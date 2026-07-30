import express from "express";
import { z } from "zod";
import { pythonHost } from "../config.js";
import { v4 as uuidv4 } from "uuid";
import { runPipelines } from "../runPipeLines.js";
import { removeSessionId, setSessionId } from "../ports/sessionStore.js";
import { incSessionStopped, setRtmpAuthEnabled } from "../monitoring/metrics.js";
import {
  createSessionLifecycle,
  type SessionLifecycle,
} from "../usecases/SessionLifecycle.js";

const PY_HOST = pythonHost;

const DEFAULT_PUBLISH_DONE_GRACE_SEC = 120;

// Read per call rather than at import: the grace window is an operational
// knob (church network quality), and reading it live keeps it overridable in
// tests — same reason RTMP_PUBLISH_KEY is read inside the handler.
function publishDoneGraceMs(): number {
  const raw = Number(process.env.RTMP_PUBLISH_DONE_GRACE_SEC);
  const seconds =
    Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_PUBLISH_DONE_GRACE_SEC;
  return seconds * 1000;
}

// nginx-rtmp posts its notify hooks as urlencoded form fields. Only the three
// fields we act on are declared; nginx sends a dozen more and passthrough is
// fine because nothing downstream reads the rest.
const hookBodySchema = z.object({
  key: z.string().optional(),
  clientid: z.string().optional(),
  addr: z.string().optional(),
});

const stopBodySchema = z.object({
  sessionId: z.string().min(1),
});

const startBodySchema = z.object({
  sourceLang: z.string().min(1).default("ko-KR"),
  targetLang: z.string().min(1).default("en-US"),
});

export function createRtmpRouter(lifecycle: SessionLifecycle): express.Router {
  const router = express.Router();

  // nginx-rtmp on_publish hook: called before a publisher (OBS) is accepted.
  // The stream key rides in the stream-name args (`translation?key=SECRET`)
  // which nginx-rtmp forwards as urlencoded form fields; 2xx allows, non-2xx
  // denies. With RTMP_PUBLISH_KEY unset the hook allows everything (auth off)
  // so this code can ship before the church OBS / ENV_PROD are updated.
  router.post(
    "/rtmp/on-publish",
    express.urlencoded({ extended: false }),
    (req, res) => {
      const body = hookBodySchema.safeParse(req.body ?? {});
      const hook = body.success ? body.data : {};
      const configuredKey = process.env.RTMP_PUBLISH_KEY;
      setRtmpAuthEnabled(Boolean(configuredKey));

      if (!configuredKey) {
        console.warn(
          "rtmp on_publish: RTMP_PUBLISH_KEY not set — allowing publish (auth disabled)"
        );
        lifecycle.publisherReturned(hook.clientid ?? null);
        return res.status(200).end();
      }
      if (hook.key === configuredKey) {
        lifecycle.publisherReturned(hook.clientid ?? null);
        return res.status(200).end();
      }
      console.warn(
        `rtmp on_publish: denied publish from ${hook.addr ?? "unknown"}`
      );
      return res.status(403).end();
    }
  );

  // nginx-rtmp on_publish_done: fires when the PUBLISHER disconnects. Verified
  // against nginx-rtmp-module 1.2.2 that a player detaching fires on_play_done
  // only, so node's own ffmpeg pull retrying never reaches this route — which
  // is why on_done (fires for both) must not be used here.
  //
  // The route is reachable from the internet (nginx does not gate /api/), so
  // it repeats the on_publish key check: without it a single unauthenticated
  // POST would end the broadcast one grace window later.
  router.post(
    "/rtmp/on-publish-done",
    express.urlencoded({ extended: false }),
    (req, res) => {
      const body = hookBodySchema.safeParse(req.body ?? {});
      const hook = body.success ? body.data : {};
      const configuredKey = process.env.RTMP_PUBLISH_KEY;

      // Deliberately mirrors on_publish above, positive match and all: both
      // hooks guard the same trust boundary, and writing this one as
      // "reject if key set and mismatched" leaves an opening the moment
      // someone edits the condition. Only two branches reach publisherDone.
      if (!configuredKey) {
        console.warn(
          "rtmp on_publish_done: RTMP_PUBLISH_KEY not set — accepting hook (auth disabled)"
        );
        lifecycle.publisherDone(hook.clientid ?? null);
        return res.status(200).end();
      }
      if (hook.key === configuredKey) {
        lifecycle.publisherDone(hook.clientid ?? null);
        return res.status(200).end();
      }
      console.warn(
        `rtmp on_publish_done: rejected from ${hook.addr ?? "unknown"}`
      );
      return res.status(403).end();
    }
  );

  router.post("/sessions/start", async (req, res) => {
    const body = startBodySchema.safeParse(req.body ?? {});
    if (!body.success) {
      return res.status(400).json({ error: "Invalid language selection" });
    }

    try {
      const result = await lifecycle.start(body.data);
      return res.status(202).json(result);
    } catch (err) {
      console.error("session start failed", err);
      return res.status(500).json({ error: "Failed to start session" });
    }
  });

  router.post("/sessions/stop", async (req, res) => {
    const body = stopBodySchema.safeParse(req.body ?? {});
    if (!body.success) {
      return res.status(400).json({ error: "sessionId required" });
    }

    try {
      const outcome = await lifecycle.stopBySessionId(body.data.sessionId);
      if (outcome === "mismatch") {
        console.warn(`Session ID mismatch: requested ${body.data.sessionId}`);
        return res.status(400).json({ error: "Session ID mismatch" });
      }
      return res.status(200).json({ ok: true });
    } catch (err) {
      console.error("Error stopping session:", err);
      return res.status(500).json({ error: "Failed to stop session" });
    }
  });

  return router;
}

const defaultLifecycle = createSessionLifecycle({
  newSessionId: () => uuidv4(),
  startPipeline: runPipelines,
  startPythonSession: async ({ sessionId, sourceLang, targetLang }) => {
    const r = await fetch(`${PY_HOST}/internal/sessions/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, sourceLang, targetLang }),
    });
    const payload = (await r.json()) as { webSocketUrl: string };
    if (!r.ok) throw new Error(`python session start failed: ${r.status}`);
    return payload;
  },
  // Unlike before, a non-2xx here is surfaced instead of swallowed: reporting
  // a stop that python never applied is what left sessions open in the first
  // place.
  stopPythonSession: async (sessionId) => {
    const r = await fetch(`${PY_HOST}/internal/sessions/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId }),
    });
    if (!r.ok) throw new Error(`python session stop failed: ${r.status}`);
  },
  onSessionIdChanged: (sessionId) =>
    sessionId ? setSessionId(sessionId) : removeSessionId(),
  recordStop: incSessionStopped,
  graceMs: publishDoneGraceMs,
});

export default createRtmpRouter(defaultLifecycle);
