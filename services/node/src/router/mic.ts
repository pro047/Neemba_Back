import express from "express";
import { pythonHost } from "../config.js";
import { v4 as uuidv4 } from "uuid";
import { removeSessionId, setSessionId } from "../ports/sessionStore.js";
import { GoogleAuth } from "google-auth-library";
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
  stopSession(sessionId: string): Promise<void>;
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
    },
  };
}

function createMicTtsSynthesizer(): MicTtsSynthesizer {
  const auth = new GoogleAuth({
    scopes: "https://www.googleapis.com/auth/cloud-platform",
  });
  const clientPromise = auth.getClient();
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

      const client = await clientPromise;

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

async function stopExistingMicSession(
  runtimeStore: SessionRuntimeStore,
  pythonClient: PythonSessionClient
): Promise<void> {
  const activeSessionId = runtimeStore.getActiveSessionId();

  if (!activeSessionId) {
    return;
  }

  const activeRuntime = runtimeStore.get(activeSessionId);
  removeSessionId();

  if (activeRuntime) {
    await activeRuntime.stop();
  }

  runtimeStore.delete(activeSessionId);
  await pythonClient.stopSession(activeSessionId);
}

export function createStartMicSessionHandler({
  pythonClient,
  runtimeStore,
  micPipelineFactory,
  sessionIdFactory,
}: MicHandlerDependencies): RequestHandler {
  return async (req, res) => {
    const sessionId = sessionIdFactory();
    const sourceLang = req.body?.sourceLang ?? "ko-KR";
    const targetLang = req.body?.targetLang ?? "en-US";

    console.log("python host", PY_HOST);
    console.log(sessionId);
    console.log(sourceLang);
    console.log(targetLang);

    try {
      await stopExistingMicSession(runtimeStore, pythonClient).catch((error) => {
        console.error("failed to stop previous mic session", error);
      });

      const pythonSession = await pythonClient.startSession({
        sessionId,
        sourceLang,
        targetLang,
      });

      setSessionId(sessionId);

      try {
        const runtime = await micPipelineFactory(sessionId, {
          sourceLang,
          targetLang,
        });
        runtimeStore.set(sessionId, runtime);
        runtimeStore.setActiveSessionId(sessionId);
      } catch (error) {
        removeSessionId();
        await pythonClient.stopSession(sessionId).catch(() => undefined);
        throw error;
      }

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
      await stopMicSession(sessionId, { pythonClient, runtimeStore });

      return res.status(200).json({ ok: true });
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
): Promise<void> {
  const runtime = runtimeStore.get(sessionId);

  if (runtime) {
    await runtime.stop();
    runtimeStore.delete(sessionId);
  }

  removeSessionId();
  await pythonClient.stopSession(sessionId);
}

export function createMicSessionStopper(
  runtimeStore: SessionRuntimeStore = micRuntimeStore
): (sessionId: string) => Promise<void> {
  const pythonClient = createPythonSessionClient(PY_HOST);
  return (sessionId) => stopMicSession(sessionId, { pythonClient, runtimeStore });
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
}: CreateMicRouterDependencies = {}) {
  const router = express.Router();
  const startHandler = createStartMicSessionHandler({
    pythonClient,
    runtimeStore,
    ttsSynthesizer,
    micPipelineFactory,
    sessionIdFactory,
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
