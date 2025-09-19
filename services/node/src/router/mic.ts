import express from "express";
import { v4 as uuidv4 } from "uuid";
import { pythonHost } from "../config";

const router = express.Router();

const PY_HOST = pythonHost;

router.post("/mic/start", async (req, res) => {
  const sessionId = uuidv4;

  try {
    const response = await fetch(`${PY_HOST}/internal/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId }),
    });
    const t = (await response.json()) as {
      sessionId: string;
      websocketUrl: string;
    };
    if (!response.ok) console.error("python error:", response.status, t);
    else console.log("python ok:", t);

    return res.status(200).json({ sessionId, websocketUrl: t.websocketUrl });
  } catch (err) {
    console.error("python fetch fail:", err);
    return res.status(500).json({ error: err });
  }
});

router.post("/mic/stop", async (req, res) => {
  const sessionId = req.body?.sessionId;
  console.log("stop sessionId:", sessionId);

  if (!sessionId)
    return res.status(400).json({
      error: "sessionId required",
    });
  try {
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
