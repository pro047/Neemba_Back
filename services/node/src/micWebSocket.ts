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
    if (!attachedSessionId) return;
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

    const sessionId = resolveSessionId(requestUrl);

    if (!sessionId) {
      socket.close(1008, "sessionId required");
      return;
    }

    const runtime = runtimeStore.get(sessionId);

    if (!runtime) {
      socket.close(1011, "No active mic runtime");
      return;
    }

    runtime.inputWritable.write(toBuffer(message));
  });

  socket.on("error", (error) => {
    console.error("mic websocket error", error);
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
