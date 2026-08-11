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

  it("유예 중 start 가 와도 새 세션을 만들지 않고 자동 종료를 미루지 않아야 한다", async () => {
    // Arrange: P1 D1 이후 유예 창 안의 [시작] 은 join 이다. 그런데 join 이
    // 유예를 취소하면, OBS 가 이미 떠난 방송을 청취자 앱 하나가 영원히 열어두게
    // 된다 — auto-stop 이 없애려던 고아 세션 그대로다. 청취자가 붙는 것은
    // publisher 가 돌아온 것이 아니다.
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");
    lifecycle.publisherDone("1");

    // Act
    await vi.advanceTimersByTimeAsync(GRACE_MS / 2);
    const joined = await lifecycle.start(languages);
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(joined.sessionId).toBe("session-1");
    expect(joined.joined).toBe(true);
    // 새 세션이 생겼다면 startPythonSession 이 두 번 불렸을 것이다
    expect(deps.startPythonSession).toHaveBeenCalledTimes(1);
    expect(deps.stopPythonSession).toHaveBeenCalledTimes(1);
    expect(deps.stopPythonSession).toHaveBeenCalledWith("session-1");
    expect(deps.recordStop).toHaveBeenCalledExactlyOnceWith("publisher_done");
    expect(lifecycle.currentSession()).toBeNull();
  });

  it("stop 요청은 세션을 닫지 않고 유예 타이머만 종료를 수행해야 한다", async () => {
    // Arrange: P1 D4. join 한 청취자가 [정지] 를 누르면 남의 방송이 죽으므로
    // stop 을 무력화했다. 그 대가로 종료 경로가 on_publish_done 하나뿐이라,
    // 이 테스트가 '그 하나가 실제로 닫는다' 를 지키는 유일한 가드다.
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("1");
    lifecycle.publisherDone("1");

    // Act
    await vi.advanceTimersByTimeAsync(GRACE_MS / 2);
    const outcome = await lifecycle.stopBySessionId("session-1");

    // Assert: stop 시점에는 아무것도 닫히지 않았다
    expect(outcome).toBe("ignored");
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
    expect(lifecycle.currentSession()).toBe("session-1");

    // 유예가 지나면 그때 닫힌다 — manual 이 아니라 publisher_done 으로
    await vi.advanceTimersByTimeAsync(GRACE_MS);
    expect(deps.stopPythonSession).toHaveBeenCalledExactlyOnceWith("session-1");
    expect(deps.recordStop).toHaveBeenCalledExactlyOnceWith("publisher_done");
    expect(lifecycle.currentSession()).toBeNull();
  });

  it("라이브 세션이 없을 때의 stop 은 mismatch 로 무시해야 한다", async () => {
    // Arrange
    const { lifecycle, deps } = buildLifecycle();

    // Act
    const outcome = await lifecycle.stopBySessionId("session-1");

    // Assert
    expect(outcome).toBe("mismatch");
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
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

  it("세션 시작 전에 끝난 방송의 clientid 가 다음 세션의 자동 종료를 막지 않아야 한다", async () => {
    // Arrange: on_publish 는 세션과 무관하게 도착하므로 예배 전 OBS 테스트의
    // clientid 가 세션 없이 슬롯에 남는다. 그 방송이 끝났는데도 슬롯이 안
    // 비면, 진짜 방송의 publish_done 이 불일치로 버려져 아무도 못 닫는 세션이
    // 된다 — P1 D4 로 수동 stop 이 사라진 지금 회수 수단이 없는 상태다.
    //
    // 슬롯을 비우는 책임은 publisherDone 에 있다. start 가 비우게 두면 살아
    // 있는 publisher 를 잊어버려 반대쪽 사고(아래 테스트)가 난다.
    const { lifecycle, deps } = buildLifecycle();
    lifecycle.publisherReturned("9");
    lifecycle.publisherDone("9"); // 세션이 열리기 전에 끝난 방송
    await lifecycle.start(languages);
    lifecycle.publisherReturned("4");

    // Act
    lifecycle.publisherDone("4");
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(deps.stopPythonSession).toHaveBeenCalledWith("session-1");
    expect(lifecycle.currentSession()).toBeNull();
  });

  it("슬롯이 비어 있으면 모르는 clientid 의 publish_done 도 세션을 닫아야 한다", async () => {
    // Arrange: node 가 방송 도중 재시작하면 on_publish 를 못 본 채 세션만
    // 다시 열린다. 이때도 종료 경로는 살아 있어야 한다 — 판정에 쓸 근거가
    // 없으면 teardown 을 통과시킨다는 것이 이 가드의 방향이다.
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);

    // Act
    lifecycle.publisherDone("unseen");
    await vi.advanceTimersByTimeAsync(GRACE_MS);

    // Assert
    expect(deps.stopPythonSession).toHaveBeenCalledWith("session-1");
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

  it("두 번째 start 는 방송을 재시작하지 않고 같은 세션에 합류해야 한다", async () => {
    // Arrange: P1 의 대조군. 청취자 앱의 [시작] 이 유일한 진입점이라 두 번째
    // 청취자도 이 경로로 들어온다. 예전에는 teardown("superseded") 가
    // ffmpeg·STT 를 재시작시켜 첫 청취자의 방송 자체가 끊겼다.
    const { lifecycle, deps, pipeline } = buildLifecycle();

    // Act
    const first = await lifecycle.start(languages);
    const second = await lifecycle.start(languages);

    // Assert
    expect(second.sessionId).toBe(first.sessionId);
    expect(second.webSocketUrl).toBe(first.webSocketUrl);
    expect(first.joined).toBe(false);
    expect(second.joined).toBe(true);
    // 방송이 끊기지 않았다는 것의 정의: 파이프라인도 python 세션도 그대로다
    expect(pipeline.stop).not.toHaveBeenCalled();
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
    expect(deps.startPipeline).toHaveBeenCalledTimes(1);
    expect(deps.startPythonSession).toHaveBeenCalledTimes(1);
    expect(deps.recordStop).not.toHaveBeenCalled();
  });

  it("join 응답은 요청한 언어가 아니라 라이브 세션의 언어를 실어야 한다", async () => {
    // Arrange: 방송은 이미 한 언어로 돌고 있다. 요청 언어를 그대로 되돌려주면
    // 앱이 '내 언어로 듣는 중' 이라고 표시하는데 실제로는 아니다 (P2 전까지는
    // 언어를 못 고른다).
    const { lifecycle } = buildLifecycle();
    await lifecycle.start({ sourceLang: "ko-KR", targetLang: "en-US" });

    // Act
    const joined = await lifecycle.start({
      sourceLang: "ko-KR",
      targetLang: "ja",
    });

    // Assert
    expect(joined.joined).toBe(true);
    expect(joined.targetLang).toBe("en-US");
  });

  it("동시에 도착한 start 두 건이 파이프라인을 둘 만들지 않아야 한다", async () => {
    // Arrange: 기기 2대가 같은 순간에 [시작] 을 누르는 것이 P1 의 기본
    // 시나리오다. 두 번째가 '진행 중' 상태를 못 보고 지나가면 파이프라인이 둘
    // 생기고 첫 번째는 아무도 닫지 않는 고아가 된다.
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { lifecycle, deps } = buildLifecycle({
      startPythonSession: vi.fn(async () => {
        await gate;
        return { webSocketUrl: "ws://python" };
      }),
    });

    // Act
    const first = lifecycle.start(languages);
    const second = lifecycle.start(languages);
    release!();
    const [a, b] = await Promise.all([first, second]);

    // Assert
    expect(a.sessionId).toBe(b.sessionId);
    expect(deps.startPythonSession).toHaveBeenCalledTimes(1);
    expect(deps.startPipeline).toHaveBeenCalledTimes(1);
  });

  it("시작 도중 세션이 종료되면 뒤늦게 뜬 파이프라인을 고아로 남기지 않아야 한다", async () => {
    // Arrange: OBS 가 start 요청 직후 떨어지면 on_publish_done 이 start 완료
    // 전에 도착한다. teardown 은 진행 중인 beginSession 을 취소할 방법이 없고
    // (startInFlight 는 promise 라 되돌릴 수 없다), 그대로 두면 start 가
    // currentSessionId=null 인 상태에서 pipeline 을 채운다 — 아무도 stop 을
    // 부를 수 없는 ffmpeg 다. P1 D4 로 수동 stop 백업까지 사라져 회수 수단이
    // 없다.
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const orphan: PipelineHandle = {
      stop: vi.fn(async () => {}),
      notifyPublisherReturned: vi.fn(),
    };
    const { lifecycle, deps } = buildLifecycle({
      startPythonSession: vi.fn(async () => {
        await gate;
        return { webSocketUrl: "ws://python/dead" };
      }),
      startPipeline: vi.fn(async () => orphan),
    });

    // Act
    const starting = lifecycle.start(languages);
    lifecycle.publisherDone("1");
    await vi.advanceTimersByTimeAsync(GRACE_MS); // 시작 도중 teardown
    release!();

    // Assert
    await expect(starting).rejects.toThrow("torn down while starting");
    expect(orphan.stop).toHaveBeenCalledTimes(1);
    expect(lifecycle.currentSession()).toBeNull();
    // 캐시도 안 남는다: 다음 [시작] 은 join 이 아니라 새 세션이어야 한다
    const next = await lifecycle.start(languages);
    expect(next.joined).toBe(false);
    expect(next.sessionId).toBe("session-2");
    expect(next.webSocketUrl).toBe("ws://python/dead");
    expect(deps.stopPythonSession).toHaveBeenCalledExactlyOnceWith("session-1");
  });

  it("start 가 실패하면 join 캐시가 남아 죽은 세션을 나눠주지 않아야 한다", async () => {
    // Arrange: 실패 경로에서 캐시를 안 비우면, 다음 청취자가 존재하지도 않는
    // 세션의 webSocketUrl 을 받아 영원히 붙지 못한다.
    const pipeline: PipelineHandle = {
      stop: vi.fn(async () => {}),
      notifyPublisherReturned: vi.fn(),
    };
    let attempts = 0;
    const { lifecycle, deps } = buildLifecycle({
      startPipeline: vi.fn(async () => {
        if (++attempts === 1) throw new Error("ffmpeg failed to spawn");
        return pipeline;
      }),
    });

    // Act
    await expect(lifecycle.start(languages)).rejects.toThrow(
      "ffmpeg failed to spawn"
    );

    // Assert
    expect(lifecycle.currentSession()).toBeNull();
    const retry = await lifecycle.start(languages);
    expect(retry.joined).toBe(false);
    expect(retry.sessionId).toBe("session-2");
    expect(deps.startPythonSession).toHaveBeenCalledTimes(2);
  });

  it("거절당한 두 번째 OBS 의 publish_done 이 살아 있는 방송을 끝내지 않아야 한다", async () => {
    // Arrange: 2026-08-09 dev 스택 실측을 그대로 옮긴 것. 같은 스트림 이름으로
    // 두 번째 OBS 가 붙으면 nginx 는 'Already publishing' 으로 거절하는데,
    // 거절보다 on_publish 가 먼저 나가고 1ms 뒤 같은 clientid 의
    // publish_done 이 따라온다. 예전 코드는 슬롯을 덮어써서 그 쌍이 유예를
    // 걸었고, 진짜 방송이 계속 송출 중인데도 active_session 이 1→0 으로
    // 떨어졌다. 이름 검사로는 못 막는다 — 두 번째 OBS 도 이름은 맞기 때문이다.
    const { lifecycle, deps } = buildLifecycle();
    await lifecycle.start(languages);
    lifecycle.publisherReturned("A"); // 진짜 방송

    // Act
    lifecycle.publisherReturned("B"); // 두 번째 OBS — nginx 가 곧 거절한다
    lifecycle.publisherDone("B");
    await vi.advanceTimersByTimeAsync(GRACE_MS * 2);

    // Assert: 방송도 세션도 그대로다
    expect(lifecycle.currentSession()).toBe("session-1");
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
    expect(deps.recordStop).not.toHaveBeenCalled();

    // 그리고 진짜 방송이 끝나면 여전히 닫힌다 — 방어가 종료 경로를 막지 않았다
    lifecycle.publisherDone("A");
    await vi.advanceTimersByTimeAsync(GRACE_MS);
    expect(deps.stopPythonSession).toHaveBeenCalledExactlyOnceWith("session-1");
  });

  it("방송이 앱의 시작보다 먼저 켜졌어도 두 번째 OBS 에게 슬롯을 내주지 않아야 한다", async () => {
    // Arrange: 교회의 실제 순서다 — OBS 를 먼저 켜고, 몇 분 뒤 앱에서 [시작].
    // beginSession 이 publisherClientId 를 비우면 그 사이에 살아 있는
    // publisher 를 잊고, 다음에 붙는 아무 publisher 나 슬롯을 차지한다.
    const { lifecycle, deps } = buildLifecycle();
    lifecycle.publisherReturned("A"); // 세션보다 먼저 켜진 방송

    // Act
    await lifecycle.start(languages);
    lifecycle.publisherReturned("B");
    lifecycle.publisherDone("B");
    await vi.advanceTimersByTimeAsync(GRACE_MS * 2);

    // Assert
    expect(lifecycle.currentSession()).toBe("session-1");
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
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

  it("파이프라인 시작이 실패하면 이미 시작된 python 세션을 닫아야 한다", async () => {
    // Arrange: python start 는 성공했는데 ffmpeg/STT 쪽이 실패하는 경우.
    // forgetSession 만 하고 나가면 python 의 active_session 이 1로 고착된
    // 고아 세션이 남는다 — 닫을 id 를 아는 유일한 시점이 여기다.
    const { lifecycle, deps } = buildLifecycle({
      startPipeline: vi.fn(async () => {
        throw new Error("ffmpeg spawn failed");
      }),
    });

    // Act & Assert
    await expect(lifecycle.start(languages)).rejects.toThrow(
      "ffmpeg spawn failed"
    );
    expect(deps.stopPythonSession).toHaveBeenCalledExactlyOnceWith("session-1");
  });

  it("python 세션 시작 자체가 실패하면 python stop 을 부르지 않아야 한다", async () => {
    // Arrange: 시작된 게 없으니 닫을 것도 없다 — 존재하지 않는 세션에 stop 을
    // 보내면 non-2xx 로 에러 로그만 쌓인다.
    const { lifecycle, deps } = buildLifecycle({
      startPythonSession: vi.fn(async () => {
        throw new Error("python unreachable");
      }),
    });

    // Act & Assert
    await expect(lifecycle.start(languages)).rejects.toThrow(
      "python unreachable"
    );
    expect(deps.stopPythonSession).not.toHaveBeenCalled();
  });
});
