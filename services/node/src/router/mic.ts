import express from "express";
import { PassThrough } from "node:stream";
import { pythonHost } from "../config.js";
import { v4 as uuidv4 } from "uuid";
import { GoogleAuth } from "google-auth-library";
import { scheduleMicTeardown } from "../micTeardown.js";
import {
  micRuntimeStore,
  type MicRuntime,
  type SessionRuntimeStore,
} from "../sessionRuntimeStore.js";
import { runDefaultMicPipeline } from "../runMicPipeline.js";
import type { RequestHandler } from "express";

const PY_HOST = pythonHost;

type PythonStartResponse = {
  sessionId: string;
  webSocketUrl: string;
};

// python's stop is idempotent: `ended` is true only for the call that actually
// transitioned the session. Passing it through lets a client tell "I just
// ended it" from "it was already gone" — an offline Stop that lands late gets
// 200 either way, so the status code alone cannot say.
type PythonStopResponse = {
  ok: boolean;
  ended: boolean;
  translationCount: number;
};

type TtsSynthesisResult = {
  audioContent: string;
  audioMimeType: string;
};

const MIC_TTS_CACHE_LIMIT = 100;

export interface PythonSessionClient {
  startSession(args: {
    sessionId: string;
    sourceLang: string;
    targetLang: string;
  }): Promise<PythonStartResponse>;
  stopSession(sessionId: string): Promise<PythonStopResponse>;
}

export interface MicTtsSynthesizer {
  synthesize(args: { text: string; languageCode: string }): Promise<TtsSynthesisResult>;
}

type CreateMicRouterDependencies = {
  pythonClient?: PythonSessionClient;
  runtimeStore?: SessionRuntimeStore;
  ttsSynthesizer?: MicTtsSynthesizer;
  micPipelineFactory?: (
    sessionId: string,
    languages?: {
      sourceLang: string;
      targetLang: string;
    }
  ) => Promise<MicRuntime>;
  sessionIdFactory?: () => string;
  scheduleConnectTeardown?: (sessionId: string) => void;
};

type MicTtsRequest = {
  text?: string;
  language?: string;
  fallbackLanguage?: string;
};

function createPythonSessionClient(host: string): PythonSessionClient {
  return {
    async startSession({ sessionId, sourceLang, targetLang }) {
      const response = await fetch(`${host}/internal/sessions/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId, sourceLang, targetLang }),
      });
      const body = (await response.json()) as PythonStartResponse;

      if (!response.ok) {
        throw new Error(`Failed to start python session: ${response.status}`);
      }

      return body;
    },
    async stopSession(sessionId: string) {
      const response = await fetch(`${host}/internal/sessions/stop`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId }),
      });

      if (!response.ok) {
        throw new Error(`Failed to stop python session: ${response.status}`);
      }

      return (await response.json()) as PythonStopResponse;
    },
  };
}

function createMicTtsSynthesizer(): MicTtsSynthesizer {
  const auth = new GoogleAuth({
    scopes: "https://www.googleapis.com/auth/cloud-platform",
  });
  // Lazily obtain the auth client on first synthesize. Calling getClient()
  // eagerly here leaves a floating promise that becomes an unhandled rejection
  // when credentials are absent (e.g. CI) and no synthesize follows.
  let clientPromise: ReturnType<typeof auth.getClient> | undefined;
  const responseCache = new Map<string, TtsSynthesisResult>();

  return {
    async synthesize({ text, languageCode }) {
      const cacheKey = `${languageCode}\n${text}`;
      const cached = responseCache.get(cacheKey);
      if (cached) {
        responseCache.delete(cacheKey);
        responseCache.set(cacheKey, cached);
        return cached;
      }

      const client = await (clientPromise ??= auth.getClient());

      try {
        const response = await client.request<{
          audioContent?: string;
          error?: { message?: string };
        }>({
          url: "https://texttospeech.googleapis.com/v1/text:synthesize",
          method: "POST",
          data: {
            input: { text },
            voice: { languageCode },
            audioConfig: { audioEncoding: "MP3" },
          },
          headers: {
            "Content-Type": "application/json",
          },
        });

        const body = response.data;
        if (!body?.audioContent) {
          throw new Error("TTS response missing audio content");
        }

        const synthesized = {
          audioContent: body.audioContent,
          audioMimeType: "audio/mpeg",
        } satisfies TtsSynthesisResult;

        responseCache.delete(cacheKey);
        responseCache.set(cacheKey, synthesized);
        if (responseCache.size > MIC_TTS_CACHE_LIMIT) {
          const oldestKey = responseCache.keys().next().value;
          if (oldestKey) {
            responseCache.delete(oldestKey);
          }
        }

        return synthesized;
      } catch (error) {
        const responseMessage =
          typeof error === "object" &&
          error != null &&
          "response" in error &&
          typeof error.response === "object" &&
          error.response != null &&
          "data" in error.response &&
          typeof error.response.data === "object" &&
          error.response.data != null &&
          "error" in error.response.data &&
          typeof error.response.data.error === "object" &&
          error.response.data.error != null &&
          "message" in error.response.data.error &&
          typeof error.response.data.error.message === "string"
            ? error.response.data.error.message
            : undefined;

        throw new Error(
          responseMessage ?? (error instanceof Error ? error.message : String(error))
        );
      }
    },
  };
}

type MicHandlerDependencies = {
  pythonClient: PythonSessionClient;
  runtimeStore: SessionRuntimeStore;
  ttsSynthesizer: MicTtsSynthesizer;
  micPipelineFactory: (
    sessionId: string,
    languages?: {
      sourceLang: string;
      targetLang: string;
    }
  ) => Promise<MicRuntime>;
  sessionIdFactory: () => string;
};

const DEFAULT_MIC_MAX_SESSIONS = 5;

// Not a user quota — the expected concurrent listeners are ~2. This is the
// cost-abuse ceiling: /api/mic/start is unauthenticated and each session is
// a billed Google STT stream, so without a cap one script could spin up
// streams without bound. Read per call so tests and container env both apply
// (same reason RTMP_PUBLISH_KEY is).
function micMaxSessions(): number {
  const raw = Number(process.env.MIC_MAX_SESSIONS);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_MIC_MAX_SESSIONS;
}

// How long a started session may wait for its first audio socket. The ghost
// teardown in micWebSocket.ts only arms on socket close — a session whose
// socket NEVER connects (curl abuse, app died before opening the WS) has no
// close event, so without this deadline it would hold a cap slot and a billed
// STT stream until the process restarts.
const MIC_CONNECT_GRACE_MS = 15_000;

type StartMicSessionDependencies = Pick<
  MicHandlerDependencies,
  "pythonClient" | "runtimeStore" | "micPipelineFactory" | "sessionIdFactory"
> & {
  // Required, not optional: a wiring that forgets it silently recreates the
  // never-connected-socket leak (same rule as the orchestrator's mandatory
  // AudioConsumerContext).
  scheduleConnectTeardown: (sessionId: string) => void;
};

export function createStartMicSessionHandler({
  pythonClient,
  runtimeStore,
  micPipelineFactory,
  sessionIdFactory,
  scheduleConnectTeardown,
}: StartMicSessionDependencies): RequestHandler {
  return async (req, res) => {
    const sessionId = sessionIdFactory();
    const sourceLang = req.body?.sourceLang ?? "ko-KR";
    const targetLang = req.body?.targetLang ?? "en-US";

    console.log("python host", PY_HOST);
    console.log(sessionId);
    console.log(sourceLang);
    console.log(targetLang);

    // Reject before touching python: a denied start must leave zero traces.
    if (runtimeStore.count() >= micMaxSessions()) {
      console.warn(
        `mic start rejected: ${runtimeStore.count()} sessions already running (max ${micMaxSessions()})`
      );
      return res.status(429).json({ error: "Too many active mic sessions" });
    }

    // Reserve the slot in the same tick as the check above. Concurrent starts
    // interleave only at awaits, so without this placeholder N simultaneous
    // requests all read the same count and blow past the cap together. The
    // error path below releases the reservation via delete().
    runtimeStore.set(sessionId, {
      inputWritable: new PassThrough(),
      stop: async () => {},
    });

    try {
      const pythonSession = await pythonClient.startSession({
        sessionId,
        sourceLang,
        targetLang,
      });

      try {
        const runtime = await micPipelineFactory(sessionId, {
          sourceLang,
          targetLang,
        });
        runtimeStore.set(sessionId, runtime);
      } catch (error) {
        await pythonClient.stopSession(sessionId).catch(() => undefined);
        throw error;
      }

      // Armed BEFORE the 202 goes out: the app cannot open the socket until
      // it has the response, so the connect that cancels this timer can never
      // race ahead of the arming.
      scheduleConnectTeardown(sessionId);

      return res.status(202).json({
        sessionId,
        webSocketUrl: pythonSession.webSocketUrl,
      });
    } catch (err) {
      console.error("python fetch fail", err);
      runtimeStore.delete(sessionId);
      return res.status(500).json({ error: "Failed to start mic session" });
    }
  };
}

const DEFAULT_TTS_FALLBACK_LANGUAGE = "en-US";

export function createMicTtsHandler({
  ttsSynthesizer,
}: Pick<MicHandlerDependencies, "ttsSynthesizer">): RequestHandler {
  return async (req, res) => {
    const { text, language, fallbackLanguage } = (req.body ?? {}) as MicTtsRequest;
    const trimmedText = text?.trim();

    if (!trimmedText) {
      return res.status(400).json({ error: "text required" });
    }

    const requestedLanguage = language?.trim() || DEFAULT_TTS_FALLBACK_LANGUAGE;
    const fallback = fallbackLanguage?.trim() || DEFAULT_TTS_FALLBACK_LANGUAGE;
    const candidates = Array.from(
      new Set([requestedLanguage, fallback].filter(Boolean))
    );

    let lastError: unknown;
    for (const candidate of candidates) {
      try {
        const synthesized = await ttsSynthesizer.synthesize({
          text: trimmedText,
          languageCode: candidate,
        });

        return res.status(200).json({
          audioContent: synthesized.audioContent,
          audioMimeType: synthesized.audioMimeType,
          requestedLanguage,
          resolvedLanguage: candidate,
          usedFallback: candidate !== requestedLanguage,
        });
      } catch (error) {
        lastError = error;
        console.error(
          "mic tts synth failed",
          JSON.stringify({
            requestedLanguage,
            candidate,
            message: error instanceof Error ? error.message : String(error),
          })
        );
      }
    }

    return res.status(502).json({
      error: "Failed to synthesize mic speech",
      requestedLanguage,
      fallbackLanguage: fallback,
      message: lastError instanceof Error ? lastError.message : String(lastError),
    });
  };
}

export function createStopMicSessionHandler({
  pythonClient,
  runtimeStore,
}: Omit<
  MicHandlerDependencies,
  "micPipelineFactory" | "sessionIdFactory" | "ttsSynthesizer"
>): RequestHandler {
  return async (req, res) => {
    const sessionId = req.body?.sessionId as string | undefined;
    console.log("stop sessionId:", sessionId);

    if (!sessionId) {
      return res.status(400).json({ error: "sessionId required" });
    }

    try {
      const stopped = await stopMicSession(sessionId, {
        pythonClient,
        runtimeStore,
      });

      // `ended: false` with a 200 means the session was already gone — a late
      // offline Stop. The client needs that apart from a stop it actually
      // performed, and the status code is 200 in both cases.
      return res.status(200).json({
        ok: true,
        ended: stopped.ended,
        translationCount: stopped.translationCount,
      });
    } catch (err) {
      return res.status(500).json({ error: "Failed to stop mic session" });
    }
  };
}

/** Full session teardown shared by /mic/stop and the ghost-session sweeper. */
export async function stopMicSession(
  sessionId: string,
  {
    pythonClient,
    runtimeStore,
  }: {
    pythonClient: PythonSessionClient;
    runtimeStore: SessionRuntimeStore;
  }
): Promise<PythonStopResponse> {
  const runtime = runtimeStore.get(sessionId);

  if (runtime) {
    await runtime.stop();
    runtimeStore.delete(sessionId);
  }

  return pythonClient.stopSession(sessionId);
}

export function createMicSessionStopper(
  runtimeStore: SessionRuntimeStore = micRuntimeStore
): (sessionId: string) => Promise<void> {
  const pythonClient = createPythonSessionClient(PY_HOST);
  // Teardown callers have no client to report `ended` to — drop it here rather
  // than widening their signature.
  return async (sessionId) => {
    await stopMicSession(sessionId, { pythonClient, runtimeStore });
  };
}

export function createMicRouter({
  pythonClient = createPythonSessionClient(PY_HOST),
  runtimeStore = micRuntimeStore,
  ttsSynthesizer = createMicTtsSynthesizer(),
  micPipelineFactory = (
    sessionId: string,
    languages?: { sourceLang: string; targetLang: string }
  ) => {
    const streamLanguages =
      languages == null
        ? undefined
        : {
            ...(languages.sourceLang == null
              ? {}
              : { sourceLanguage: languages.sourceLang }),
            ...(languages.targetLang == null
              ? {}
              : { targetLanguage: languages.targetLang }),
          };
    return runDefaultMicPipeline(sessionId, streamLanguages);
  },
  sessionIdFactory = uuidv4,
  scheduleConnectTeardown,
}: CreateMicRouterDependencies = {}) {
  const router = express.Router();
  const connectTeardown =
    scheduleConnectTeardown ??
    ((sessionId: string) =>
      scheduleMicTeardown(sessionId, {
        runtimeStore,
        stop: async (id) => {
          await stopMicSession(id, { pythonClient, runtimeStore });
        },
        graceMs: MIC_CONNECT_GRACE_MS,
        reason:
          "mic session started but no audio socket connected — tearing down",
      }));
  const startHandler = createStartMicSessionHandler({
    pythonClient,
    runtimeStore,
    micPipelineFactory,
    sessionIdFactory,
    scheduleConnectTeardown: connectTeardown,
  });
  const stopHandler = createStopMicSessionHandler({
    pythonClient,
    runtimeStore,
  });
  const ttsHandler = createMicTtsHandler({ ttsSynthesizer });

  router.post("/mic/start", startHandler);
  router.post("/mic/stop", stopHandler);
  router.post("/mic/tts", ttsHandler);

  return router;
}

export default createMicRouter();
