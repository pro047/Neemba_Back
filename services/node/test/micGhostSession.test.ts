import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachMicSocketHandlers } from "../src/micWebSocket.js";
import { createSessionRuntimeStore } from "../src/sessionRuntimeStore.js";

// P1 ghost-session suite: an app that drops the mic WS without calling
// /mic/stop must not leave the Google STT stream (and its 285s rotation
// timer) running — and billing — forever. Teardown fires after a grace
// window; reconnecting within it cancels the teardown.

class FakeSocket extends EventEmitter {
  close() {
    this.emit("close");
  }
}

describe("mic websocket — ghost session teardown", () => {
  const GRACE_MS = 1_000;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  function setup(sessionId = "session-1") {
    const runtimeStore = createSessionRuntimeStore();
    runtimeStore.set(sessionId, {
      inputWritable: new PassThrough(),
      stop: async () => {},
    });
    const stopSession = vi.fn(async (id: string) => {
      runtimeStore.delete(id);
    });
    const pendingTeardowns = new Map<string, NodeJS.Timeout>();
    const attach = (socket: FakeSocket) =>
      attachMicSocketHandlers(socket as never, `/api/mic?sessionId=${sessionId}`, {
        runtimeStore,
        stopSession,
        teardownGraceMs: GRACE_MS,
        pendingTeardowns,
      });
    return { runtimeStore, stopSession, attach };
  }

  it("tears the session down after the grace period when the socket closes", async () => {
    const { stopSession, attach } = setup();
    const socket = new FakeSocket();
    attach(socket);

    socket.close();
    expect(stopSession).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(GRACE_MS + 10);

    expect(stopSession).toHaveBeenCalledTimes(1);
    expect(stopSession).toHaveBeenCalledWith("session-1");
  });

  it("cancels the teardown when the app reconnects within the grace window", async () => {
    const { stopSession, attach } = setup();
    const first = new FakeSocket();
    attach(first);
    first.close();

    await vi.advanceTimersByTimeAsync(GRACE_MS / 2);
    const second = new FakeSocket();
    attach(second);

    await vi.advanceTimersByTimeAsync(GRACE_MS * 2);
    expect(stopSession).not.toHaveBeenCalled();
  });

  it("does nothing when the session was already stopped normally", async () => {
    const { runtimeStore, stopSession, attach } = setup();
    const socket = new FakeSocket();
    attach(socket);

    // /mic/stop already ran: runtime is gone before the socket closes.
    runtimeStore.delete("session-1");
    socket.close();
    await vi.advanceTimersByTimeAsync(GRACE_MS * 2);

    expect(stopSession).not.toHaveBeenCalled();
  });

  it("does nothing when /mic/stop lands during the grace window", async () => {
    const { runtimeStore, stopSession, attach } = setup();
    const socket = new FakeSocket();
    attach(socket);

    socket.close();
    await vi.advanceTimersByTimeAsync(GRACE_MS / 2);
    runtimeStore.delete("session-1");
    await vi.advanceTimersByTimeAsync(GRACE_MS * 2);

    expect(stopSession).not.toHaveBeenCalled();
  });
});

// A force-killed / network-dropped client never sends a TCP close, so the
// "close" event (and thus teardown) never fires on its own. A protocol-level
// heartbeat detects the dead socket and terminates it to force the close.
describe("mic websocket — 하트비트로 죽은 소켓 감지", () => {
  const HEARTBEAT_MS = 1_000;
  const GRACE_MS = 500;

  class HeartbeatFakeSocket extends EventEmitter {
    ping = vi.fn();
    terminate = vi.fn(() => this.emit("close"));
    close() {
      this.emit("close");
    }
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  function setup(sessionId = "session-1") {
    const runtimeStore = createSessionRuntimeStore();
    runtimeStore.set(sessionId, {
      inputWritable: new PassThrough(),
      stop: async () => {},
    });
    const stopSession = vi.fn(async (id: string) => {
      runtimeStore.delete(id);
    });
    const pendingTeardowns = new Map<string, NodeJS.Timeout>();
    const attach = (socket: HeartbeatFakeSocket) =>
      attachMicSocketHandlers(socket as never, `/api/mic?sessionId=${sessionId}`, {
        runtimeStore,
        stopSession,
        teardownGraceMs: GRACE_MS,
        pendingTeardowns,
        heartbeatIntervalMs: HEARTBEAT_MS,
      });
    return { runtimeStore, stopSession, attach };
  }

  it("pong 응답이 없으면 소켓을 terminate 해야 한다", async () => {
    // Arrange
    const { attach } = setup();
    const socket = new HeartbeatFakeSocket();
    attach(socket);

    // Act: two intervals with no pong (1st pings, 2nd terminates)
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 2 + 10);

    // Assert
    expect(socket.terminate).toHaveBeenCalledTimes(1);
  });

  it("pong 응답이 계속되면 소켓을 terminate 하지 않아야 한다", async () => {
    // Arrange: the client auto-replies pong to every ping
    const { attach } = setup();
    const socket = new HeartbeatFakeSocket();
    socket.ping.mockImplementation(() => socket.emit("pong"));
    attach(socket);

    // Act
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 5 + 10);

    // Assert
    expect(socket.terminate).not.toHaveBeenCalled();
  });

  it("하트비트 terminate가 유령 teardown을 유발해야 한다", async () => {
    // Arrange
    const { stopSession, attach } = setup();
    const socket = new HeartbeatFakeSocket();
    attach(socket);

    // Act: heartbeat terminates (→ close), then the grace window elapses
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 2 + 10);
    await vi.advanceTimersByTimeAsync(GRACE_MS + 10);

    // Assert
    expect(stopSession).toHaveBeenCalledWith("session-1");
  });
});
