import type http from "node:http";
import { WebSocketServer, type RawData, type WebSocket } from "ws";
import {
  micRuntimeStore,
  type SessionRuntimeStore,
} from "./sessionRuntimeStore.js";
import { createMicSessionStopper } from "./router/mic.js";
import {
  cancelMicTeardown,
  micPendingTeardowns,
  scheduleMicTeardown,
} from "./micTeardown.js";

// A socket that closes without /mic/stop leaves the Google STT stream (and
// its 285s rotation timer) running — and billing — forever. After this grace
// window the session is torn down; a reconnect within it cancels teardown.
const DEFAULT_TEARDOWN_GRACE_MS = 10_000;
// A force-killed / network-dropped client leaves a half-open socket that never
// emits "close", so teardown would never fire. Ping this often; if a full
// interval passes with no pong the socket is declared dead and terminated.
const DEFAULT_HEARTBEAT_INTERVAL_MS = 15_000;

// Uplink counterpart of the hub's CLOSE_SESSION_NOT_FOUND (websocket.py): the
// session this socket names is gone, so reconnecting cannot help. One code
// across both links means the client needs a single rule — stop retrying —
// instead of branching per endpoint.
export const CLOSE_MIC_SESSION_NOT_FOUND = 4404;

type AttachMicSocketDependencies = {
  runtimeStore?: SessionRuntimeStore;
  stopSession?: (sessionId: string) => Promise<void>;
  teardownGraceMs?: number;
  pendingTeardowns?: Map<string, NodeJS.Timeout>;
  heartbeatIntervalMs?: number;
};

function toBuffer(message: RawData): Buffer {
  if (Buffer.isBuffer(message)) {
    return message;
  }

  if (Array.isArray(message)) {
    return Buffer.concat(message);
  }

  if (message instanceof ArrayBuffer) {
    return Buffer.from(message);
  }

  const view = message as ArrayBufferView;
  return Buffer.from(view.buffer, view.byteOffset, view.byteLength);
}

function resolveSessionId(
  requestUrl: string | undefined,
): string | undefined {
  const baseUrl = "http://localhost";
  return requestUrl
    ? new URL(requestUrl, baseUrl).searchParams.get("sessionId") ?? undefined
    : undefined;
}

export function attachMicSocketHandlers(
  socket: WebSocket,
  requestUrl: string | undefined,
  {
    runtimeStore = micRuntimeStore,
    stopSession,
    teardownGraceMs = DEFAULT_TEARDOWN_GRACE_MS,
    pendingTeardowns = micPendingTeardowns,
    heartbeatIntervalMs = DEFAULT_HEARTBEAT_INTERVAL_MS,
  }: AttachMicSocketDependencies = {}
): void {
  const attachedSessionId = resolveSessionId(requestUrl);

  // FIRST, before any path that can return: ws emits 'error' for a peer's
  // protocol violation (unmasked frame, bad opcode, invalid close code), and an
  // 'error' with no listener throws out of the EventEmitter and kills the
  // process — every live session with it. A rejected socket can still do that
  // between our close frame and the close handshake finishing.
  socket.on("error", (error) => {
    console.error("mic websocket error", error);
  });

  // Reject at connect time, not on the first audio frame. The old code let a
  // dead session's socket sit open until audio arrived, so a client could not
  // tell "reconnected" from "reconnected to nothing" without sending — and a
  // client that never sends would hold the socket forever.
  if (!attachedSessionId) {
    socket.close(1008, "sessionId required");
    return;
  }

  // Checked BEFORE cancelMicTeardown: a pending timer with no runtime means
  // teardown already ran, and cancelling it would only lose the cleanup.
  if (!runtimeStore.get(attachedSessionId)) {
    console.warn(`mic ws rejected, no such session: ${attachedSessionId}`);
    socket.close(CLOSE_MIC_SESSION_NOT_FOUND, "No active mic session");
    return;
  }

  // A (re)connect within a grace window keeps the session alive — this
  // cancels both the awaiting-first-socket timer armed by /mic/start and the
  // reconnect timer armed by a previous socket's close.
  if (
    attachedSessionId &&
    cancelMicTeardown(attachedSessionId, pendingTeardowns)
  ) {
    console.log(`mic ws connected, teardown cancelled: ${attachedSessionId}`);
  }

  // Protocol-level heartbeat. The client's WS stack auto-replies pong (no app
  // code needed); if a full interval elapses with no pong we terminate() to
  // force a "close" event, which drives the ghost-teardown path below.
  let isAlive = true;
  socket.on("pong", () => {
    isAlive = true;
  });
  const heartbeat = setInterval(() => {
    if (!isAlive) {
      socket.terminate();
      return;
    }
    isAlive = false;
    socket.ping();
  }, heartbeatIntervalMs);
  heartbeat.unref?.();

  socket.on("close", () => {
    clearInterval(heartbeat);
    if (!runtimeStore.get(attachedSessionId)) return;

    scheduleMicTeardown(attachedSessionId, {
      runtimeStore,
      stop: stopSession ?? createMicSessionStopper(runtimeStore),
      graceMs: teardownGraceMs,
      reason: "mic ws closed without stop — tearing down ghost session",
      pendingTeardowns,
    });
  });

  socket.on("message", (message: RawData, isBinary: boolean) => {
    if (!isBinary) {
      return;
    }

    // attachedSessionId, not a re-resolve: the connect-time guard above
    // already rejected a missing id, so there is no second chance for it to
    // be absent here.
    const runtime = runtimeStore.get(attachedSessionId);

    if (!runtime) {
      // Same code as the connect-time rejection above: the session can also
      // die mid-stream (teardown while this socket stayed open), and the
      // client's response is identical either way.
      socket.close(CLOSE_MIC_SESSION_NOT_FOUND, "No active mic session");
      return;
    }

    runtime.inputWritable.write(toBuffer(message));
  });
}

type CreateMicWebSocketServerDependencies = {
  server: http.Server;
  path?: string;
  runtimeStore?: SessionRuntimeStore;
  stopSession?: (sessionId: string) => Promise<void>;
  teardownGraceMs?: number;
};

export function createMicWebSocketServer({
  server,
  path = "/api/mic",
  runtimeStore = micRuntimeStore,
  stopSession,
  teardownGraceMs,
}: CreateMicWebSocketServerDependencies): WebSocketServer {
  const ws = new WebSocketServer({ server, path });
  // The process-wide map, NOT a per-server one: /mic/start arms its
  // awaiting-first-socket timer there, and only a connection routed through
  // this server can cancel it.
  const pendingTeardowns = micPendingTeardowns;

  ws.on("connection", (socket, request) => {
    attachMicSocketHandlers(socket, request.url, {
      runtimeStore,
      pendingTeardowns,
      ...(stopSession ? { stopSession } : {}),
      ...(teardownGraceMs != null ? { teardownGraceMs } : {}),
    });
  });

  return ws;
}
