import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PassThrough } from "node:stream";
import { StreamOrchestrator } from "../src/usecases/StreamOrchestrator.js";
import { StreamSwitcher } from "../src/stream/StreamSwitcher.js";
import { InterimChunkOrchestrator } from "../src/usecases/InterimChunkOrchestratotr.js";
import type { PublishEvent } from "../src/ports/transcriptPublisher.js";
import type { SpeechToTextPort } from "../src/ports/sttPorts.js";

// P0 #2 regression suite: 285s stream rotation must not corrupt or misattribute
// text. (1) late results from the old stream keep the old segmentId,
// (2) the delta baseline resets at segment boundaries and utterance finals so
// unrelated texts are never diffed against each other.

describe("StreamOrchestrator — late results keep their own segmentId", () => {
  type TranscriptCallback = (p: {
    isFinal: boolean;
    transcriptText: string;
    confidence?: number;
    resultEndTimeMs?: number;
  }) => void;

  function createFakeSttPort() {
    const streams: { onTranscript: TranscriptCallback }[] = [];
    const port = {
      getRecognizer: async () => {},
      startStreaming(options: { onTranscript: TranscriptCallback }) {
        streams.push({ onTranscript: options.onTranscript });
        return {
          configureOnce: () => {},
          writeAudioChunk: async () => {},
          stop: async () => {},
          isOpen: () => true,
        };
      },
    };
    return { port: port as unknown as SpeechToTextPort, streams };
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-07T00:00:00Z"));
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("attributes an old stream's late final to the old segment after rotation", async () => {
    const { port, streams } = createFakeSttPort();
    const received: { transcript: string; segmentId: number }[] = [];
    const orchestra = {
      onSttResult: async (result: { transcript: string; segmentId: number }) => {
        received.push({ transcript: result.transcript, segmentId: result.segmentId });
      },
      dispose: async () => {},
    };
    let counter = 0;
    const segmentManager = { next: () => ++counter };
    const switcher = new StreamSwitcher(() => {});

    const orchestrator = new StreamOrchestrator(
      port,
      switcher,
      orchestra as never,
      segmentManager as never
    );
    const stop = await orchestrator.start(new PassThrough(), {
      sessionId: "session-1",
    });
    expect(streams).toHaveLength(1);

    // 285s passes → rotation creates stream 2 / segment 2.
    await vi.advanceTimersByTimeAsync(285_000);
    expect(streams).toHaveLength(2);

    // The old stream flushes its last final AFTER the handoff.
    streams[0].onTranscript({
      isFinal: true,
      transcriptText: "늦게 도착한 최종 결과입니다",
      confidence: 0.9,
      resultEndTimeMs: 1000,
    });

    expect(received).toEqual([
      { transcript: "늦게 도착한 최종 결과입니다", segmentId: 1 },
    ]);

    await stop();
  });
});

describe("InterimChunkOrchestrator — boundary resets", () => {
  let published: PublishEvent[];
  let orchestrator: InterimChunkOrchestrator;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-07T00:00:00Z"));
    published = [];
    orchestrator = new InterimChunkOrchestrator({
      publish: async (event: PublishEvent) => {
        published.push(event);
      },
      start: async () => {},
      stop: async () => {},
    });
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  const sttResult = (
    transcript: string,
    segmentId: number,
    isFinal = false
  ) => ({
    transcript,
    isFinal,
    resultEndTimeMs: 100,
    segmentId,
    sessionId: "session-1",
  });

  const nonEmpty = () => published.filter((e) => e.transcriptText !== "");
  const texts = () => nonEmpty().map((e) => e.transcriptText);
  const segmentIds = () => nonEmpty().map((e) => e.segmentId);

  it("does not diff texts across a segment boundary", async () => {
    await orchestrator.onSttResult(sttResult("안녕하세요 여러분", 1));
    await vi.advanceTimersByTimeAsync(100);

    // New segment: shares the prefix "안녕하" with the old text — a
    // cross-segment diff would corrupt it to "신가요".
    await orchestrator.onSttResult(sttResult("안녕하신가요", 2));

    expect(texts()).toEqual(["안녕하세요 여러분", "안녕하신가요"]);
    expect(segmentIds()).toEqual([1, 2]);
  });

  it("flushes deltas still queued under the old segment at the boundary", async () => {
    await orchestrator.onSttResult(sttResult("안녕하세요", 1));
    // Within the 50ms throttle window: this delta stays in the queue.
    await vi.advanceTimersByTimeAsync(10);
    await orchestrator.onSttResult(sttResult("안녕하세요 여러분", 1));

    // Boundary: the queued " 여러분" must go out under segment 1.
    await vi.advanceTimersByTimeAsync(10);
    await orchestrator.onSttResult(sttResult("다음 세그먼트입니다", 2));
    await vi.advanceTimersByTimeAsync(100);
    await orchestrator.onSttResult(sttResult("다음 세그먼트입니다 여러분", 2));

    expect(texts()).toEqual([
      "안녕하세요",
      " 여러분",
      "다음 세그먼트입니다 여러분",
    ]);
    expect(segmentIds()).toEqual([1, 1, 2]);
  });

  it("resets the delta baseline after a final so the next utterance is not diffed", async () => {
    await orchestrator.onSttResult(sttResult("안녕하세요", 1));
    await vi.advanceTimersByTimeAsync(100);
    await orchestrator.onSttResult(sttResult("안녕하세요 여러분", 1, true));
    await vi.advanceTimersByTimeAsync(100);

    // Next utterance shares the "안녕하" prefix: without the reset the diff
    // would publish a corrupted "신가요".
    await orchestrator.onSttResult(sttResult("안녕하신가요", 1));

    expect(texts()).toEqual(["안녕하세요", " 여러분", "안녕하신가요"]);
  });
});
