import type http from "node:http";
import { WebSocketServer, type RawData, type WebSocket } from "ws";
import {
  micRuntimeStore,
  type SessionRuntimeStore,
} from "./sessionRuntimeStore.js";

type AttachMicSocketDependencies = {
  runtimeStore?: SessionRuntimeStore;
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
  { runtimeStore = micRuntimeStore }: AttachMicSocketDependencies = {}
): void {
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
};

export function createMicWebSocketServer({
  server,
  path = "/api/mic",
  runtimeStore = micRuntimeStore,
}: CreateMicWebSocketServerDependencies): WebSocketServer {
  const ws = new WebSocketServer({ server, path });

  ws.on("connection", (socket, request) => {
    attachMicSocketHandlers(socket, request.url, { runtimeStore });
  });

  return ws;
}
