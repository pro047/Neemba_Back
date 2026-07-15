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
