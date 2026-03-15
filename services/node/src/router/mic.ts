import express from "express";
import { pythonHost } from "../config.js";
import { v4 as uuidv4 } from "uuid";
import { removeSessionId, setSessionId } from "../ports/sessionStore.js";
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

export interface PythonSessionClient {
  startSession(args: {
    sessionId: string;
    sourceLang: string;
    targetLang: string;
  }): Promise<PythonStartResponse>;
  stopSession(sessionId: string): Promise<void>;
}

type CreateMicRouterDependencies = {
  pythonClient?: PythonSessionClient;
  runtimeStore?: SessionRuntimeStore;
  micPipelineFactory?: (sessionId: string) => Promise<MicRuntime>;
  sessionIdFactory?: () => string;
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

type MicHandlerDependencies = {
  pythonClient: PythonSessionClient;
  runtimeStore: SessionRuntimeStore;
  micPipelineFactory: (sessionId: string) => Promise<MicRuntime>;
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
        const runtime = await micPipelineFactory(sessionId);
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

export function createStopMicSessionHandler({
  pythonClient,
  runtimeStore,
}: Omit<MicHandlerDependencies, "micPipelineFactory" | "sessionIdFactory">): RequestHandler {
  return async (req, res) => {
    const sessionId = req.body?.sessionId as string | undefined;
    console.log("stop sessionId:", sessionId);

    if (!sessionId) {
      return res.status(400).json({ error: "sessionId required" });
    }

    try {
      const runtime = runtimeStore.get(sessionId);

      if (runtime) {
        await runtime.stop();
        runtimeStore.delete(sessionId);
      }

      removeSessionId();
      await pythonClient.stopSession(sessionId);

      return res.status(200).json({ ok: true });
    } catch (err) {
      return res.status(500).json({ error: "Failed to stop mic session" });
    }
  };
}

export function createMicRouter({
  pythonClient = createPythonSessionClient(PY_HOST),
  runtimeStore = micRuntimeStore,
  micPipelineFactory = (sessionId: string) => runDefaultMicPipeline(sessionId),
  sessionIdFactory = uuidv4,
}: CreateMicRouterDependencies = {}) {
  const router = express.Router();
  const startHandler = createStartMicSessionHandler({
    pythonClient,
    runtimeStore,
    micPipelineFactory,
    sessionIdFactory,
  });
  const stopHandler = createStopMicSessionHandler({
    pythonClient,
    runtimeStore,
  });

  router.post("/mic/start", startHandler);
  router.post("/mic/stop", stopHandler);

  return router;
}

export default createMicRouter();
