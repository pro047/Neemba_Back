import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import { FfmpegTranscoder } from "../src/adapters/FfmpegTranscoder.js";

// No RTMP publisher (e.g. OBS not started yet) means ffmpeg gets no data and
// exits/stalls forever. Waiting is a LEGITIMATE state — the operator may open
// the session before going live — so restarts must never give up; they back
// off exponentially instead (10s → 20s → ... → cap) and reset to the base
// delay as soon as real progress arrives.

class FakeChild extends EventEmitter {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  stdin = Object.assign(new EventEmitter(), {
    write: vi.fn(),
    end: vi.fn(),
    destroyed: false,
  });
  kill = vi.fn();
}

function setup(options?: {
  restartBaseDelayMs?: number;
  restartMaxDelayMs?: number;
}) {
  const children: FakeChild[] = [];
  const spawnMock = vi.fn(() => {
    const child = new FakeChild();
    children.push(child);
    return child;
  });
  const transcoder = new FfmpegTranscoder(
    spawnMock as never,
    options ?? { restartBaseDelayMs: 100, restartMaxDelayMs: 400 }
  );
  return { children, spawnMock, transcoder };
}

describe("FfmpegTranscoder — on_publish 즉시 재시작 (§4-4)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("대기 중인 백오프 재시작은 restartNow로 즉시 실행되어야 한다", async () => {
    // Arrange: child 사망 → 백오프 100ms 재시작 예약 상태
    const { children, spawnMock, transcoder } = setup();
    transcoder.startTranscoder();
    children[0]!.emit("close", 1, null);

    // Act: 타이머를 진행하지 않고 on_publish 신호만 준다
    transcoder.restartNow();
    await vi.advanceTimersByTimeAsync(0);

    // Assert: 백오프(100ms)를 기다리지 않고 즉시 새 ffmpeg spawn
    expect(spawnMock).toHaveBeenCalledTimes(2);
  });

  it("정지(stop) 후에는 restartNow가 무시되어야 한다", async () => {
    // Arrange: 재시작이 예약된 채로 세션이 정상 종료됨
    const { children, spawnMock, transcoder } = setup();
    const { stop } = transcoder.startTranscoder();
    children[0]!.emit("close", 1, null);
    stop();

    // Act: 늦게 도착한 on_publish 신호
    transcoder.restartNow();
    await vi.advanceTimersByTimeAsync(1000);

    // Assert: 새 spawn 없음
    expect(spawnMock).toHaveBeenCalledTimes(1);
  });
});

describe("FfmpegTranscoder — ffmpeg 인자 (§4-2)", () => {
  it("analyzeduration은 0이 아닌 명시적 상한이어야 한다", () => {
    // Arrange & Act: ffmpeg 인자는 spawn 시점에 확정된다
    const { spawnMock, transcoder } = setup();
    transcoder.startTranscoder();

    // Assert: 0은 "분석 생략"이 아니라 "포맷 기본값(FLV 라이브는 사실상
    // probesize 종료 조건)"이라, 저비트레이트 음성에서 32KB가 찰 때까지
    // 첫 출력이 지연된다(로컬 nginx-rtmp 실측 ~2s, 비트레이트에 비례).
    // 0.5s(500000us)로 못박아 프로브를 즉시 끝낸다.
    const args = spawnMock.mock.calls[0]?.[1] as unknown as string[];
    const value = args[args.indexOf("-analyzeduration") + 1];
    expect(value).toBe("500000");
  });
});

describe("FfmpegTranscoder — 재시작 지수 백오프", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("첫 재시작은 기본 지연 후에 실행되어야 한다", async () => {
    // Arrange
    const { children, spawnMock, transcoder } = setup();
    transcoder.startTranscoder();

    // Act: child dies, advance just past the base delay
    children[0]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(100);

    // Assert
    expect(spawnMock).toHaveBeenCalledTimes(2);
  });

  it("기본 지연 전에는 재시작하지 않아야 한다", async () => {
    // Arrange
    const { spawnMock, children, transcoder } = setup();
    transcoder.startTranscoder();

    // Act: child dies, advance to just BEFORE the base delay
    children[0]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(99);

    // Assert
    expect(spawnMock).toHaveBeenCalledTimes(1);
  });

  it("진행 없이 연속 실패하면 재시작 지연이 2배로 늘어나야 한다", async () => {
    // Arrange: first restart consumed (delay 100)
    const { children, spawnMock, transcoder } = setup();
    transcoder.startTranscoder();
    children[0]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(100);

    // Act: second failure — 100ms is no longer enough, 200ms is
    children[1]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(100);
    const spawnsAfterBaseDelay = spawnMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(100);

    // Assert: nothing at +100, third spawn only at +200
    expect([spawnsAfterBaseDelay, spawnMock.mock.calls.length]).toEqual([
      2, 3,
    ]);
  });

  it("재시작 지연은 상한을 넘지 않아야 한다", async () => {
    // Arrange: exhaust delays past the cap (100 → 200 → 400 → cap 400)
    const { children, spawnMock, transcoder } = setup();
    transcoder.startTranscoder();
    children[0]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(100);
    children[1]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(200);
    children[2]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(400);

    // Act: next delay would be 800 without the cap — cap keeps it at 400
    children[3]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(400);

    // Assert
    expect(spawnMock).toHaveBeenCalledTimes(5);
  });

  it("진행이 재개되면 재시작 지연이 기본값으로 돌아가야 한다", async () => {
    // Arrange: two failures push the delay to 200
    const { children, spawnMock, transcoder } = setup();
    transcoder.startTranscoder();
    children[0]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(100);
    children[1]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(200);

    // Act: real progress arrives, then the child dies again
    children[2]!.stderr.emit("data", Buffer.from("out_time_ms=1000"));
    children[2]!.emit("close", 1, null);
    await vi.advanceTimersByTimeAsync(100);

    // Assert: restart happened at the BASE delay again, not 400
    expect(spawnMock).toHaveBeenCalledTimes(4);
  });

  it("stop 이후에는 예약된 재시작이 실행되지 않아야 한다", async () => {
    // Arrange: a restart is pending
    const { children, spawnMock, transcoder } = setup();
    const { stop } = transcoder.startTranscoder();
    children[0]!.emit("close", 1, null);

    // Act
    stop();
    await vi.advanceTimersByTimeAsync(1000);

    // Assert
    expect(spawnMock).toHaveBeenCalledTimes(1);
  });
});

describe("FfmpegTranscoder — RTMP pull URL 설정", () => {
  afterEach(() => {
    delete process.env.RTMP_PULL_URL;
  });

  it("RTMP_PULL_URL 환경변수가 있으면 ffmpeg 입력 URL로 사용해야 한다", () => {
    // Arrange
    process.env.RTMP_PULL_URL = "rtmp://rtmp:1935/live/translation";
    const { spawnMock, transcoder } = setup();

    // Act
    transcoder.startTranscoder().stop();

    // Assert
    const args = spawnMock.mock.calls[0]?.[1] as unknown as string[];
    expect(args).toContain("rtmp://rtmp:1935/live/translation");
  });

  it("RTMP_PULL_URL 환경변수가 없으면 기존 공개 URL을 사용해야 한다", () => {
    // Arrange
    delete process.env.RTMP_PULL_URL;
    const { spawnMock, transcoder } = setup();

    // Act
    transcoder.startTranscoder().stop();

    // Assert
    const args = spawnMock.mock.calls[0]?.[1] as unknown as string[];
    expect(args).toContain("rtmp://neemba.app:1935/live/translation");
  });
});
