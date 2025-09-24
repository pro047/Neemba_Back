import express from "express";
import { pythonHost } from "../config";
import { v4 as uuidv4 } from "uuid";
import { runPipelines } from "../runPipeLines";
import { removeSessionId, setSessionId } from "../sessions/sessionStore";

const router = express.Router();

const PY_HOST = pythonHost;

let stopPipeline: (() => Promise<void>) | null = null;

router.post("/sessions/start", async (req, res) => {
  const sessionId = uuidv4();
  const sourceLang = req.body?.sourceLang ?? "ko-KR";
  const targetLang = req.body?.targetLang ?? "en-US";

  setSessionId(sessionId);

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
    if (!r.ok) console.error("python err:", r.status, t);
    else console.log("python ok:", t);

    const { stop } = await runPipelines();
    stopPipeline = stop;

    return res.status(202).json({ sessionId, webSocketUrl: t.webSocketUrl });
  } catch (err) {
    console.error("python fetch fail", err);
    return res.status(500).json({ error: err });
  }
});

router.post("/sessions/stop", async (req, res) => {
  const sessionId = req.body?.sessionId;
  console.log("stop sessionId:", sessionId);

  if (!sessionId) return res.status(400).json({ error: "sessionId required" });

  try {
    removeSessionId();

    if (stopPipeline) await stopPipeline();

    console.log("session stopped");
    console.log(stopPipeline);

    await fetch(`${PY_HOST}/internal/sessions/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: sessionId }),
    });
    return res.status(200).json({ ok: true });
  } catch (err) {
    return res.status(500).json({ error: err });
  }
});

export default router;
