import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createStartMicSessionHandler,
  createStopMicSessionHandler,
  stopMicSession,
  type PythonSessionClient,
} from "../src/router/mic.js";
import { scheduleMicTeardown } from "../src/micTeardown.js";
import { attachMicSocketHandlers } from "../src/micWebSocket.js";
import {
  createSessionRuntimeStore,
  type MicRuntime,
} from "../src/sessionRuntimeStore.js";

// 마이크는 "개인 통역기"다: 청취자 1명 = 세션 1개 = STT 1스트림. 외국인
// 청취자 N명이 각자 폰으로 동시에 쓰므로, start 가 기존 세션을 죽이던
// 단일 슬롯(kill-and-replace)을 없애고 세션들이 서로 완전 독립이어야 한다.
// 상한(MIC_MAX_SESSIONS)은 사용자 제한이 아니라 무인증 start 남용 시
// STT 과금 폭주를 막는 차단선이다.

function createMockResponse() {
  return {
    statusCode: 200,
    body: undefined as unknown,
    status(code: number) {
      this.statusCode = code;
      return this;
    },
    json(payload: unknown) {
      this.body = payload;
      return this;
    },
  };
}

class FakeSocket extends EventEmitter {
  close() {
    this.emit("close");
  }
  // Heartbeat support: reply pong on next tick like a real WS stack.
  ping() {
    queueMicrotask(() => this.emit("pong"));
  }
  terminate() {
    this.emit("close");
  }
}

function createHarness(sessionIds: string[]) {
  const runtimeStore = createSessionRuntimeStore();
  const pendingTeardowns = new Map<string, NodeJS.Timeout>();
  const runtimes = new Map<string, MicRuntime & { stop: ReturnType<typeof vi.fn> }>();
  const pythonClient: PythonSessionClient = {
    startSession: vi.fn(async ({ sessionId }) => ({
      sessionId,
      webSocketUrl: `ws://localhost/api/mic?sessionId=${sessionId}`,
    })),
    stopSession: vi.fn(async () => {}),
  };
  const queue = [...sessionIds];
  const startHandler = createStartMicSessionHandler({
    pythonClient,
    runtimeStore,
    scheduleConnectTeardown: (sessionId) =>
      scheduleMicTeardown(sessionId, {
        runtimeStore,
        stop: (id) => stopMicSession(id, { pythonClient, runtimeStore }),
        graceMs: 15_000,
        reason: "no audio socket connected (test)",
        pendingTeardowns,
      }),
    micPipelineFactory: async (sessionId) => {
      const runtime = {
        inputWritable: new PassThrough(),
        stop: vi.fn(async () => {}),
      };
      runtimes.set(sessionId, runtime);
      return runtime;
    },
    sessionIdFactory: () => queue.shift() ?? "unexpected-extra-session",
  });
  const stopHandler = createStopMicSessionHandler({ pythonClient, runtimeStore });

  return {
    runtimeStore,
    runtimes,
    pythonClient,
    pendingTeardowns,
    async start() {
      const res = createMockResponse();
      await startHandler(
        { body: { sourceLang: "ko-KR", targetLang: "en-US" } } as never,
        res as never,
        () => {}
      );
      return res;
    },
    async stop(sessionId: string) {
      const res = createMockResponse();
      await stopHandler({ body: { sessionId } } as never, res as never, () => {});
      return res;
    },
  };
}

describe("마이크 다중 세션 — 완전 독립", () => {
  afterEach(() => {
    delete process.env.MIC_MAX_SESSIONS;
  });

  it("두 번째 start 가 첫 세션을 죽이지 않고 둘 다 살아 있어야 한다", async () => {
    // Arrange
    const harness = createHarness(["user-a", "user-b"]);

    // Act
    const first = await harness.start();
    const second = await harness.start();

    // Assert: kill-and-replace 시절에는 두 번째 start 가 user-a 를 stop 했다
    expect(first.statusCode).toBe(202);
    expect(second.statusCode).toBe(202);
    expect(harness.runtimeStore.get("user-a")).toBeDefined();
    expect(harness.runtimeStore.get("user-b")).toBeDefined();
    expect(harness.runtimes.get("user-a")!.stop).not.toHaveBeenCalled();
    expect(harness.pythonClient.stopSession).not.toHaveBeenCalled();
  });

  it("한 세션을 stop 해도 다른 세션은 영향받지 않아야 한다", async () => {
    // Arrange: 두 세션 동시 가동
    const harness = createHarness(["user-a", "user-b"]);
    await harness.start();
    await harness.start();

    // Act
    const res = await harness.stop("user-a");

    // Assert
    expect(res.statusCode).toBe(200);
    expect(harness.runtimes.get("user-a")!.stop).toHaveBeenCalledTimes(1);
    expect(harness.runtimeStore.get("user-a")).toBeUndefined();
    expect(harness.pythonClient.stopSession).toHaveBeenCalledExactlyOnceWith(
      "user-a"
    );
    expect(harness.runtimeStore.get("user-b")).toBeDefined();
    expect(harness.runtimes.get("user-b")!.stop).not.toHaveBeenCalled();
  });

  it("동시 세션이 상한에 도달하면 새 start 를 429 로 거절해야 한다", async () => {
    // Arrange
    process.env.MIC_MAX_SESSIONS = "2";
    const harness = createHarness(["user-a", "user-b", "user-c"]);
    await harness.start();
    await harness.start();

    // Act
    const third = await harness.start();

    // Assert: 거절은 python 세션을 만들기 전에 일어나야 한다 (기존 2건만)
    expect(third.statusCode).toBe(429);
    expect(harness.pythonClient.startSession).toHaveBeenCalledTimes(2);
    expect(harness.runtimeStore.get("user-a")).toBeDefined();
    expect(harness.runtimeStore.get("user-b")).toBeDefined();
  });

  it("상한에서 한 세션이 stop 으로 빠지면 다시 start 가 가능해야 한다", async () => {
    // Arrange
    process.env.MIC_MAX_SESSIONS = "2";
    const harness = createHarness(["user-a", "user-b", "user-c"]);
    await harness.start();
    await harness.start();
    await harness.stop("user-a");

    // Act
    const third = await harness.start();

    // Assert
    expect(third.statusCode).toBe(202);
    expect(harness.runtimeStore.get("user-c")).toBeDefined();
  });

  it("WS 가 한 번도 붙지 않은 세션은 유예 후 정리되어야 한다", async () => {
    // Arrange: start 만 치고 오디오 소켓을 영영 안 여는 경우 (curl 남용 또는
    // 앱이 WS 열기 전에 죽은 경우). 유령 teardown 은 소켓 close 에서만 arm
    // 되므로, 연결 자체가 없으면 아무도 이 세션을 청소하지 않는다 — 상한
    // 슬롯 영구 점유 + STT 과금 지속.
    vi.useFakeTimers();
    try {
      const harness = createHarness(["user-a"]);

      // Act
      await harness.start();
      await vi.advanceTimersByTimeAsync(60_000);

      // Assert
      expect(harness.runtimes.get("user-a")!.stop).toHaveBeenCalledTimes(1);
      expect(harness.runtimeStore.count()).toBe(0);
      expect(harness.pythonClient.stopSession).toHaveBeenCalledExactlyOnceWith(
        "user-a"
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("유예 안에 WS 가 붙으면 세션이 정리되지 않아야 한다", async () => {
    // Arrange
    vi.useFakeTimers();
    try {
      const harness = createHarness(["user-a"]);
      await harness.start();

      // Act: 정상 앱 흐름 — start 응답 직후 오디오 소켓 접속
      const socket = new FakeSocket();
      attachMicSocketHandlers(socket as never, "/api/mic?sessionId=user-a", {
        runtimeStore: harness.runtimeStore,
        pendingTeardowns: harness.pendingTeardowns,
      });
      await vi.advanceTimersByTimeAsync(60_000);

      // Assert
      expect(harness.runtimes.get("user-a")!.stop).not.toHaveBeenCalled();
      expect(harness.runtimeStore.get("user-a")).toBeDefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("동시 start 폭주가 상한을 뚫지 못해야 한다", async () => {
    // Arrange: count 확인과 등록 사이 await 틈을 노린 동시 요청. 상한 검사가
    // check-then-act 면 전부 count 0 을 보고 통과해 STT 가 무제한으로 뜬다.
    process.env.MIC_MAX_SESSIONS = "2";
    const harness = createHarness([
      "user-a",
      "user-b",
      "user-c",
      "user-d",
      "user-e",
    ]);

    // Act
    const responses = await Promise.all([
      harness.start(),
      harness.start(),
      harness.start(),
      harness.start(),
      harness.start(),
    ]);

    // Assert
    const codes = responses.map((r) => r.statusCode).sort();
    expect(codes).toEqual([202, 202, 429, 429, 429]);
    expect(harness.pythonClient.startSession).toHaveBeenCalledTimes(2);
    expect(harness.runtimeStore.count()).toBe(2);
  });

  it("각 세션의 WS 오디오는 자기 runtime 에만 도달해야 한다", async () => {
    // Arrange: 대조군 — 오디오 라우팅은 원래부터 sessionId 별이어야 한다
    const runtimeStore = createSessionRuntimeStore();
    const receivedA: Buffer[] = [];
    const receivedB: Buffer[] = [];
    const writableA = new PassThrough();
    const writableB = new PassThrough();
    writableA.on("data", (c) => receivedA.push(Buffer.from(c)));
    writableB.on("data", (c) => receivedB.push(Buffer.from(c)));
    runtimeStore.set("user-a", { inputWritable: writableA, stop: async () => {} });
    runtimeStore.set("user-b", { inputWritable: writableB, stop: async () => {} });
    const socketA = new FakeSocket();
    const socketB = new FakeSocket();
    attachMicSocketHandlers(socketA as never, "/api/mic?sessionId=user-a", {
      runtimeStore,
    });
    attachMicSocketHandlers(socketB as never, "/api/mic?sessionId=user-b", {
      runtimeStore,
    });

    // Act
    socketA.emit("message", Buffer.from("frame-a"), true);
    socketB.emit("message", Buffer.from("frame-b"), true);

    // Assert
    expect(receivedA).toEqual([Buffer.from("frame-a")]);
    expect(receivedB).toEqual([Buffer.from("frame-b")]);
  });
});
