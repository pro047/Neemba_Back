import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { InterimChunkOrchestrator } from "../src/usecases/InterimChunkOrchestratotr.js";
import { FfmpegTranscoder } from "../src/adapters/FfmpegTranscoder.js";

// P1 crash-guard suite: a NATS publish rejection or an ffmpeg stdin EPIPE must
// never escape as an unhandled rejection / uncaught 'error' event — either one
// takes down the whole Node process and every live session with it.

describe("InterimChunkOrchestrator — publish failures stay contained", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-07T00:00:00Z"));
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  const sttResult = (transcript: string) => ({
    transcript,
    isFinal: false,
    resultEndTimeMs: 100,
    segmentId: 1,
    sessionId: "session-1",
  });

  it("resolves even when publish rejects, including the trailing timer path", async () => {
    const orchestrator = new InterimChunkOrchestrator({
      publish: async () => {
        throw new Error("nats publish timeout");
      },
      start: async () => {},
      stop: async () => {},
    });

    await expect(
      orchestrator.onSttResult(sttResult("안녕하세요"))
    ).resolves.toBeUndefined();

    // The trailing debounce timer also calls publishSpan — advancing it must
    // not surface an unhandled rejection either (vitest fails the test if
    // one escapes).
    await vi.advanceTimersByTimeAsync(400);
  });

  it("keeps the queue and retries when the publisher start fails once", async () => {
    let startCalls = 0;
    const published: string[] = [];
    const orchestrator = new InterimChunkOrchestrator({
      publish: async (event) => {
        published.push(event.transcriptText);
      },
      start: async () => {
        startCalls += 1;
        if (startCalls === 1) throw new Error("nats connect refused");
      },
      stop: async () => {},
    });

    // First result: start() fails — must not reject, text must not be lost.
    await expect(
      orchestrator.onSttResult(sttResult("안녕하세요"))
    ).resolves.toBeUndefined();
    expect(published).toEqual([]);

    // Second result: start() succeeds and the retained text ships too.
    await vi.advanceTimersByTimeAsync(100);
    await orchestrator.onSttResult(sttResult("안녕하세요 여러분"));

    expect(published.join("")).toContain("안녕하세요");
    expect(published.join("")).toContain("여러분");
  });
});

describe("FfmpegTranscoder — stdin errors do not crash the process", () => {
  class FakeChild extends EventEmitter {
    stdin = new PassThrough();
    stdout = new PassThrough();
    stderr = new PassThrough();
    kill = vi.fn();
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("swallows an EPIPE on ffmpeg stdin instead of raising an uncaught error", () => {
    const child = new FakeChild();
    const fakeSpawn = vi.fn(() => child);
    const transcoder = new FfmpegTranscoder(fakeSpawn as never);

    const { stop } = transcoder.startTranscoder();

    // ffmpeg died mid-write: without an 'error' listener this emit throws
    // (uncaught 'error' event) and kills the whole process.
    expect(() =>
      child.stdin.emit("error", new Error("write EPIPE"))
    ).not.toThrow();

    stop();
  });
});
