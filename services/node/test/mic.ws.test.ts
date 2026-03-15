import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, describe, expect, it } from "vitest";
import { attachMicSocketHandlers } from "../src/micWebSocket.js";
import { createSessionRuntimeStore } from "../src/sessionRuntimeStore.js";

class FakeSocket extends EventEmitter {
  public closeCode: number | undefined;
  public closeReason: string | undefined;

  close(code?: number, reason?: string) {
    this.closeCode = code;
    this.closeReason = reason;
    this.emit("close", code, reason);
  }
}

describe("mic websocket", () => {
  const sockets: FakeSocket[] = [];

  afterEach(() => {
    sockets.length = 0;
  });

  it("writes binary websocket messages to the matching runtime input", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const inputWritable = new PassThrough();
    const received: Buffer[] = [];
    inputWritable.on("data", (chunk) => {
      received.push(Buffer.from(chunk));
    });

    runtimeStore.set("session-1", {
      inputWritable,
      stop: async () => {},
    });
    const socket = new FakeSocket();
    sockets.push(socket);
    attachMicSocketHandlers(socket as never, "/api/mic?sessionId=session-1", {
      runtimeStore,
    });

    socket.emit("message", Buffer.from("pcm-frame"), true);

    expect(received).toEqual([Buffer.from("pcm-frame")]);
  });

  it("ignores text websocket messages", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const inputWritable = new PassThrough();
    const received: Buffer[] = [];
    inputWritable.on("data", (chunk) => {
      received.push(Buffer.from(chunk));
    });

    runtimeStore.set("session-1", {
      inputWritable,
      stop: async () => {},
    });
    const socket = new FakeSocket();
    sockets.push(socket);
    attachMicSocketHandlers(socket as never, "/api/mic?sessionId=session-1", {
      runtimeStore,
    });

    socket.emit("message", Buffer.from("hello"), false);

    expect(received).toEqual([]);
  });

  it("closes the websocket when no active runtime exists", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const socket = new FakeSocket();
    sockets.push(socket);
    attachMicSocketHandlers(socket as never, "/api/mic?sessionId=session-1", {
      runtimeStore,
    });

    socket.emit("message", Buffer.from("pcm-frame"), true);

    expect(socket.closeCode).toBe(1011);
  });

  it("closes the websocket when sessionId is missing", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const socket = new FakeSocket();
    sockets.push(socket);
    attachMicSocketHandlers(socket as never, "/api/mic", { runtimeStore });

    socket.emit("message", Buffer.from("pcm-frame"), true);

    expect(socket.closeCode).toBe(1008);
    expect(socket.closeReason).toBe("sessionId required");
  });

  it("connection cleanup does not crash the process", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const inputWritable = new PassThrough();
    runtimeStore.set("session-1", {
      inputWritable,
      stop: async () => {},
    });
    const socket = new FakeSocket();
    sockets.push(socket);
    attachMicSocketHandlers(socket as never, "/api/mic?sessionId=session-1", {
      runtimeStore,
    });

    socket.close();

    expect(true).toBe(true);
  });
});
