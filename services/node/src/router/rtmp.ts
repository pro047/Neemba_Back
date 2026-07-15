import express from "express";
import { pythonHost } from "../config.js";
import { v4 as uuidv4 } from "uuid";
import { runPipelines } from "../runPipeLines.js";
import { removeSessionId, setSessionId } from "../ports/sessionStore.js";

const router = express.Router();

const PY_HOST = pythonHost;

let stopPipeline: (() => Promise<void>) | null = null;
let currentSessionId: string | null = null;

// nginx-rtmp on_publish hook: called before a publisher (OBS) is accepted.
// The stream key rides in the stream-name args (`translation?key=SECRET`)
// which nginx-rtmp forwards as urlencoded form fields; 2xx allows, non-2xx
// denies. With RTMP_PUBLISH_KEY unset the hook allows everything (auth off)
// so this code can ship before the church OBS / ENV_PROD are updated.
router.post(
  "/rtmp/on-publish",
  express.urlencoded({ extended: false }),
  (req, res) => {
    const configuredKey = process.env.RTMP_PUBLISH_KEY;
    if (!configuredKey) {
      console.warn(
        "rtmp on_publish: RTMP_PUBLISH_KEY not set — allowing publish (auth disabled)"
      );
      return res.status(200).end();
    }
    if (req.body?.key === configuredKey) {
      return res.status(200).end();
    }
    console.warn(
      `rtmp on_publish: denied publish from ${req.body?.addr ?? "unknown"}`
    );
    return res.status(403).end();
  }
);

router.post("/sessions/start", async (req, res) => {
  const sessionId = uuidv4();
  const sourceLang = req.body?.sourceLang ?? "ko-KR";
  const targetLang = req.body?.targetLang ?? "en-US";

  // 이전 세션이 있으면 정리
  if (stopPipeline && currentSessionId) {
    console.log(`Stopping previous session: ${currentSessionId}`);
    try {
      await stopPipeline();
      await fetch(`${PY_HOST}/internal/sessions/stop`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: currentSessionId }),
      });
    } catch (err) {
      console.error("Error stopping previous session:", err);
    }
    stopPipeline = null;
    currentSessionId = null;
  }

  setSessionId(sessionId);
  currentSessionId = sessionId;

  console.log("python host", PY_HOST);
  console.log(sessionId);
  console.log(sourceLang);
  console.log(targetLang);

  try {
    const r = await fetch(`${PY_HOST}/internal/sessions/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, sourceLang, targetLang }),
    });
    const t = (await r.json()) as {
      sessionId: string;
      webSocketUrl: string;
    };
    if (!r.ok) {
      console.error("python err:", r.status, t);
      currentSessionId = null;
      return res.status(500).json({ error: "Failed to start session" });
    }
    console.log("python ok:", t);

    const { stop } = await runPipelines({
      sourceLanguage: sourceLang,
      targetLanguage: targetLang,
    });
    stopPipeline = stop;

    return res.status(202).json({ sessionId, webSocketUrl: t.webSocketUrl });
  } catch (err) {
    console.error("python fetch fail", err);
    currentSessionId = null;
    stopPipeline = null;
    return res.status(500).json({ error: err });
  }
});

router.post("/sessions/stop", async (req, res) => {
  const sessionId = req.body?.sessionId;
  console.log("stop sessionId:", sessionId);

  if (!sessionId) return res.status(400).json({ error: "sessionId required" });

  // 세션 ID가 일치하는지 확인
  if (sessionId !== currentSessionId) {
    console.warn(`Session ID mismatch: requested ${sessionId}, current ${currentSessionId}`);
    return res.status(400).json({ error: "Session ID mismatch" });
  }

  try {
    removeSessionId();

    if (stopPipeline) {
      await stopPipeline();
      stopPipeline = null;
    }

    currentSessionId = null;
    console.log("session stopped");

    await fetch(`${PY_HOST}/internal/sessions/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: sessionId }),
    });
    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error("Error stopping session:", err);
    // 에러 발생 시에도 상태 정리
    stopPipeline = null;
    currentSessionId = null;
    return res.status(500).json({ error: err });
  }
});

export default router;
