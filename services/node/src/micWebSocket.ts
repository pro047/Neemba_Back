import type http from "node:http";
import { WebSocketServer, type RawData, type WebSocket } from "ws";
import {
  micRuntimeStore,
  type SessionRuntimeStore,
} from "./sessionRuntimeStore.js";
import { createMicSessionStopper } from "./router/mic.js";

// A socket that closes without /mic/stop leaves the Google STT stream (and
// its 285s rotation timer) running — and billing — forever. After this grace
// window the session is torn down; a reconnect within it cancels teardown.
const DEFAULT_TEARDOWN_GRACE_MS = 10_000;
const defaultPendingTeardowns = new Map<string, NodeJS.Timeout>();

type AttachMicSocketDependencies = {
  runtimeStore?: SessionRuntimeStore;
  stopSession?: (sessionId: string) => Promise<void>;
  teardownGraceMs?: number;
  pendingTeardowns?: Map<string, NodeJS.Timeout>;
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
    pendingTeardowns = defaultPendingTeardowns,
  }: AttachMicSocketDependencies = {}
): void {
  const attachedSessionId = resolveSessionId(requestUrl);

  // Reconnect within the grace window: keep the session alive.
  if (attachedSessionId) {
    const pending = pendingTeardowns.get(attachedSessionId);
    if (pending) {
      clearTimeout(pending);
      pendingTeardowns.delete(attachedSessionId);
      console.log(
        `mic ws reconnected, teardown cancelled: ${attachedSessionId}`
      );
    }
  }

  socket.on("close", () => {
    if (!attachedSessionId) return;
    if (!runtimeStore.get(attachedSessionId)) return;
    if (pendingTeardowns.has(attachedSessionId)) return;

    const stop = stopSession ?? createMicSessionStopper(runtimeStore);
    const timer = setTimeout(() => {
      pendingTeardowns.delete(attachedSessionId);
      // /mic/stop may have landed during the grace window.
      if (!runtimeStore.get(attachedSessionId)) return;
      console.warn(
        `mic ws closed without stop — tearing down ghost session: ${attachedSessionId}`
      );
      stop(attachedSessionId).catch((err) =>
        console.error("ghost session teardown failed", err)
      );
    }, teardownGraceMs);
    timer.unref?.();
    pendingTeardowns.set(attachedSessionId, timer);
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
  // One map per server so every connection of a session shares the same
  // pending-teardown state (a reconnect must find the timer to cancel it).
  const pendingTeardowns = new Map<string, NodeJS.Timeout>();

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
