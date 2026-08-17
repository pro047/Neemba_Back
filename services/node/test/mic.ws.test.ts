import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, describe, expect, it } from "vitest";
import {
  attachMicSocketHandlers,
  CLOSE_MIC_SESSION_NOT_FOUND,
} from "../src/micWebSocket.js";
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

  // The client cannot tell "reconnected" from "reconnected to a dead session"
  // until the server says so. Rejecting on connect means it never has to send
  // audio to find out — and a client that never sends is no longer parked on
  // an open socket forever.
  it("런타임이 없으면 오디오를 받기 전에 접속 시점에 닫아야 한다", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const socket = new FakeSocket();
    sockets.push(socket);

    attachMicSocketHandlers(socket as never, "/api/mic?sessionId=session-1", {
      runtimeStore,
    });

    expect(socket.closeCode).toBe(CLOSE_MIC_SESSION_NOT_FOUND);
    expect(socket.closeReason).toBe("No active mic session");
  });

  it("스트리밍 도중 세션이 사라지면 첫 프레임에서 같은 코드로 닫아야 한다", async () => {
    const runtimeStore = createSessionRuntimeStore();
    runtimeStore.set("session-1", {
      inputWritable: new PassThrough(),
      stop: async () => {},
    });
    const socket = new FakeSocket();
    sockets.push(socket);
    attachMicSocketHandlers(socket as never, "/api/mic?sessionId=session-1", {
      runtimeStore,
    });
    expect(socket.closeCode).toBeUndefined();

    runtimeStore.delete("session-1");
    socket.emit("message", Buffer.from("pcm-frame"), true);

    expect(socket.closeCode).toBe(CLOSE_MIC_SESSION_NOT_FOUND);
  });

  it("sessionId가 없으면 오디오를 받기 전에 접속 시점에 닫아야 한다", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const socket = new FakeSocket();
    sockets.push(socket);

    attachMicSocketHandlers(socket as never, "/api/mic", { runtimeStore });

    expect(socket.closeCode).toBe(1008);
    expect(socket.closeReason).toBe("sessionId required");
  });

  // ws emits 'error' for a peer protocol violation, and an 'error' with no
  // listener throws out of the EventEmitter and kills the process — taking
  // every live session with it (test/crashGuards.test.ts covers the same
  // class). A rejected socket can still emit one before its close handshake
  // finishes, so the listener must be armed before any early return.
  it.each([
    ["세션이 없는 경우", "/api/mic?sessionId=gone"],
    ["sessionId가 없는 경우", "/api/mic"],
  ])("거절된 소켓의 error도 삼켜야 한다 — %s", async (_label, url) => {
    const runtimeStore = createSessionRuntimeStore();
    const socket = new FakeSocket();
    sockets.push(socket);
    attachMicSocketHandlers(socket as never, url, { runtimeStore });

    expect(socket.listenerCount("error")).toBeGreaterThan(0);
    expect(() => socket.emit("error", new Error("protocol violation"))).not.toThrow();
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
