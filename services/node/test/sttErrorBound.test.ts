import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PassThrough } from "node:stream";
import { StreamOrchestrator } from "../src/usecases/StreamOrchestrator.js";
import { StreamSwitcher } from "../src/stream/StreamSwitcher.js";
import type { SpeechToTextPort } from "../src/ports/sttPorts.js";

// Regression: a force-killed / disconnected client stops sending audio, so
// Google STT times out (~5s) with "no client requests" and fires onError.
// The old code rotated the stream on every such error with no bound → an
// infinite STT-recreation loop that keeps billing. After N consecutive errors
// with no intervening transcript (proof the client is gone) rotation must stop.

describe("StreamOrchestrator — STT 에러 회전 경계", () => {
  type TranscriptCallback = (p: {
    isFinal: boolean;
    transcriptText: string;
    confidence?: number;
    resultEndTimeMs?: number;
  }) => void;
  type ErrorCallback = (e: unknown) => void;

  function createFakeSttPort() {
    const streams: { onTranscript: TranscriptCallback; onError: ErrorCallback }[] = [];
    const port = {
      getRecognizer: async () => {},
      startStreaming(options: { onTranscript: TranscriptCallback; onError: ErrorCallback }) {
        streams.push({ onTranscript: options.onTranscript, onError: options.onError });
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

  const makeOrchestrator = (port: SpeechToTextPort, maxErrors = 3) => {
    const orchestra = { onSttResult: async () => {}, dispose: async () => {} };
    let counter = 0;
    const segmentManager = { next: () => ++counter };
    const switcher = new StreamSwitcher(() => {});
    return new StreamOrchestrator(
      port,
      switcher,
      orchestra as never,
      segmentManager as never,
      285_000,
      5_000,
      maxErrors
    );
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-07T00:00:00Z"));
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  // onError rotates immediately (no timer); a microtask flush lets the
  // in-flight rotation settle before the next error is fired.
  const flush = async () => {
    await vi.advanceTimersByTimeAsync(0);
  };

  it("연속 STT 에러가 임계값을 초과하면 스트림 회전을 멈춰야 한다", async () => {
    // Arrange: max 3, initial stream created by start()
    const { port, streams } = createFakeSttPort();
    const orchestrator = makeOrchestrator(port, 3);
    await orchestrator.start(new PassThrough(), { sessionId: "s1" });

    // Act: 4 consecutive no-audio errors (the 4th exceeds max=3)
    for (let i = 0; i < 4; i++) {
      streams[streams.length - 1].onError(new Error("Stream timed out"));
      await flush();
    }

    // Assert: 1 initial + 3 rotations, the 4th error creates no new stream
    expect(streams).toHaveLength(4);
  });

  it("에러 사이에 정상 transcript가 오면 카운터가 리셋되어 회전을 계속해야 한다", async () => {
    // Arrange
    const { port, streams } = createFakeSttPort();
    const orchestrator = makeOrchestrator(port, 3);
    await orchestrator.start(new PassThrough(), { sessionId: "s1" });

    // Act: 3 errors, then a transcript (client alive → reset), then 3 more
    for (let i = 0; i < 3; i++) {
      streams[streams.length - 1].onError(new Error("Stream timed out"));
      await flush();
    }
    streams[streams.length - 1].onTranscript({
      isFinal: false,
      transcriptText: "살아있음",
      resultEndTimeMs: 1,
    });
    for (let i = 0; i < 3; i++) {
      streams[streams.length - 1].onError(new Error("Stream timed out"));
      await flush();
    }

    // Assert: without the reset it would have capped at 4 streams
    expect(streams.length).toBeGreaterThan(4);
  });
});
