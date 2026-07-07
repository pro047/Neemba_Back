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
