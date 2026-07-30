import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSessionLifecycle,
  type PipelineHandle,
  type SessionLifecycleDeps,
} from "../src/usecases/SessionLifecycle.js";

// nginx-rtmp fires on_publish_done when the publisher (OBS) drops. node does
// not close the session immediately: it arms a grace timer so a flapping
// publisher can come back. Everything below pins the state transitions around
// that window — they are the reason this feature is not a two-line handler.

const GRACE_MS = 120_000;

function buildLifecycle(overrides: Partial<SessionLifecycleDeps> = {}) {
  const pipeline: PipelineHandle = {
    stop: vi.fn(async () => {}),
    notifyPublisherReturned: vi.fn(),
  };
  let seq = 0;
  const deps: SessionLifecycleDeps = {
    newSessionId: () => `session-${++seq}`,
    startPipeline: vi.fn(async () => pipeline),
    startPythonSession: vi.fn(async () => ({ webSocketUrl: "ws://python" })),
    stopPythonSession: vi.fn(async () => {}),
    onSessionIdChanged: vi.fn(),
    recordStop: vi.fn(),
    graceMs: () => GRACE_MS,
    ...overrides,
  };
  return { lifecycle: createSessionLifecycle(deps), deps, pipeline };
}

const languages = { sourceLang: "ko-KR", targetLang: "en-US" };

describe("세션 자동 종료 — publisher 종료 유예", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("유예 안에 publisher 가 돌아오면 세션을 유지해야 한다", async () => {
    // Arrange
    const { lifecycle, deps, pipeline } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");

    // Act
    lifecycle.publisherDone("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS - 1000);
    lifecycle.publisherReturned("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(lifecycle.currentSession()).toBe("session-1");
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
    expect(pipeline.notifyPublisherReturned).toHaveBeenCalled();
  });

  it("유예가 지나면 파이프라인 정리와 python stop 을 호출해야 한다", async () => {
    // Arrange
    const { lifecycle, deps, pipeline } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");

    // Act
    lifecycle.publisherDone("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(pipeline.stop).toHaveBeenCalledTimes(1);
    expect(deps.stopPythonSession).toHaveBeenCalledWith("session-1");
    expect(deps.recordStop).toHaveBeenCalledWith("publisher_done");
    expect(lifecycle.currentSession()).toBeNull();
  });

  it("유예 중 새 세션이 시작되면 옛 타이머가 새 세션을 종료하지 않아야 한다", async () => {
    // Arrange
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");
    lifecycle.publisherDone("1");

    // Act
    await vi.advanceTimersByTimeAsync(GRACE_MS / 2);
    await lifecycle.start(languages);
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(lifecycle.currentSession()).toBe("session-2");
    expect(deps.stopPythonSession).toHaveBeenCalledTimes(1);
    expect(deps.stopPythonSession).toHaveBeenCalledWith("session-1");
  });

  it("수동 stop 이후 타이머가 만료돼도 아무 일도 하지 않아야 한다", async () => {
    // Arrange
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");
    lifecycle.publisherDone("1");

    // Act
    await vi.advanceTimersByTimeAsync(GRACE_MS / 2);
    const outcome = await lifecycle.stopBySessionId("session-1");
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(outcome).toBe("stopped");
    expect(deps.stopPythonSession).toHaveBeenCalledTimes(1);
    expect(deps.recordStop).toHaveBeenCalledExactlyOnceWith("manual");
  });

  it("다른 clientid 의 publish_done 은 타이머를 걸지 않아야 한다", async () => {
    // Arrange: OBS 재접속으로 새 clientid 가 publish 한 뒤, 죽은 옛 연결의
    // publish_done 이 뒤늦게 도착하는 순서 역전 상황
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("7");

    // Act
    lifecycle.publisherDone("3");
    await vi.advanceTimersByTimeAsync(GRACE_MS * 2);

    // Assert
    expect(lifecycle.currentSession()).toBe("session-1");
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
  });

  it("publish_done 이 중복 수신되면 마지막 수신 기준으로 유예를 재설정해야 한다", async () => {
    // Arrange
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");

    // Act
    lifecycle.publisherDone("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS - 1000);
    lifecycle.publisherDone("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS - 1000);

    // Assert: 첫 수신 기준이었다면 이미 종료됐을 시점
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(deps.stopPythonSession).toHaveBeenCalledTimes(1);
  });

  it("세션 시작 전에 기록된 clientid 가 그 세션의 자동 종료를 막지 않아야 한다", async () => {
    // Arrange: on_publish 는 세션과 무관하게 도착하므로 세션이 없는 동안에도
    // publisherClientId 가 남는다. start 가 이를 비우지 않으면 다음 세션의
    // publish_done 이 clientid 불일치로 버려져 다시 고아 세션이 된다 — 이
    // 기능이 없애려던 바로 그 상태다.
    const { lifecycle, deps } = buildLifecycle();
    lifecycle.publisherReturned("9");
    await lifecycle.start(languages);

    // Act
    lifecycle.publisherDone("4");
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(deps.stopPythonSession).toHaveBeenCalledWith("session-1");
    expect(lifecycle.currentSession()).toBeNull();
  });

  it("세션이 없을 때 도착한 publish_done 은 무시해야 한다", async () => {
    // Arrange
    const { lifecycle, deps } = buildLifecycle();

    // Act
    lifecycle.publisherDone("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS * 2);

    // Assert
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
    expect(deps.recordStop).not.toHaveBeenCalled();
  });

  it("파이프라인 정리가 실패해도 python stop 은 호출해야 한다", async () => {
    // Arrange: 멈추지 않는 ffmpeg 가 active_session 해제를 막으면 안 된다
    const { lifecycle, deps, pipeline } = buildLifecycle();
    vi.mocked(pipeline.stop).mockRejectedValue(new Error("ffmpeg wedged"));
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");

    // Act
    lifecycle.publisherDone("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(deps.stopPythonSession).toHaveBeenCalledWith("session-1");
  });
});
