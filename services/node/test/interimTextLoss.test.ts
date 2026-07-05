import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import { InterimChunkOrchestrator } from "../src/usecases/InterimChunkOrchestratotr.js";
import { GoogleSttV2Adapter } from "../src/adapters/googleSttV2.js";
import { createSentenceSession } from "../src/sessions/createSentenceSession.js";
import type { PublishEvent } from "../src/ports/transcriptPublisher.js";

// P0 text-loss regression suite: each describe block reproduces one loss bug.
// (1) first utterance never published, (2) low-stability interim skipping a
// final in the same response, (3) final results dropped by the heuristic gate.

describe("InterimChunkOrchestrator — first utterance", () => {
  let published: PublishEvent[];
  let orchestrator: InterimChunkOrchestrator;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-05T00:00:00Z"));
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

  const sttResult = (transcript: string) => ({
    transcript,
    isFinal: false,
    resultEndTimeMs: 100,
    segmentId: 1,
    sessionId: "session-1",
  });

  const publishedTexts = () =>
    published.map((event) => event.transcriptText).filter((text) => text !== "");

  it("publishes the very first STT result as a full-text delta", async () => {
    await orchestrator.onSttResult(sttResult("안녕하세요 여러분"));

    expect(publishedTexts()).toEqual(["안녕하세요 여러분"]);
  });

  it("publishes only the delta for subsequent grown results", async () => {
    await orchestrator.onSttResult(sttResult("안녕하세요 여러분"));
    await vi.advanceTimersByTimeAsync(100);
    await orchestrator.onSttResult(sttResult("안녕하세요 여러분 반갑습니다"));

    expect(publishedTexts()).toEqual(["안녕하세요 여러분", " 반갑습니다"]);
  });

  it("ignores empty transcripts without corrupting delta state", async () => {
    await orchestrator.onSttResult(sttResult("안녕하세요"));
    await vi.advanceTimersByTimeAsync(100);
    await orchestrator.onSttResult(sttResult("   "));
    await vi.advanceTimersByTimeAsync(100);
    await orchestrator.onSttResult(sttResult("안녕하세요 반가워요"));

    // The empty interim must not reset state: no duplicated "안녕하세요".
    expect(publishedTexts()).toEqual(["안녕하세요", " 반가워요"]);
  });
});

describe("GoogleSttV2Adapter — final after low-stability interim", () => {
  type FakeStream = EventEmitter & {
    write: ReturnType<typeof vi.fn>;
    end: ReturnType<typeof vi.fn>;
  };

  const startWithFakeStream = () => {
    const stream = new EventEmitter() as FakeStream;
    stream.write = vi.fn();
    stream.end = vi.fn();
    const speechClient = {
      _streamingRecognize: () => stream,
    } as never;
    const adapter = new GoogleSttV2Adapter(speechClient, "recognizer");
    const onTranscript = vi.fn();
    adapter.startStreaming({
      languageCodes: ["ko-KR"],
      model: "latest_long",
      onTranscript,
      onError: vi.fn(),
    });
    return { stream, onTranscript };
  };

  it("still delivers a final result that follows a low-stability interim", () => {
    const { stream, onTranscript } = startWithFakeStream();

    stream.emit("data", {
      results: [
        { stability: 0.3, alternatives: [{ transcript: "불안정한 중간" }] },
        {
          isFinal: true,
          alternatives: [{ transcript: "최종 문장입니다", confidence: 0.9 }],
          resultEndTimeMs: { seconds: 1 },
        },
      ],
    });

    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({ isFinal: true, transcriptText: "최종 문장입니다" })
    );
  });

  it("still skips a lone low-stability interim", () => {
    const { stream, onTranscript } = startWithFakeStream();

    stream.emit("data", {
      results: [{ stability: 0.3, alternatives: [{ transcript: "불안정" }] }],
    });

    expect(onTranscript).not.toHaveBeenCalled();
  });
});

describe("createSentenceSession — final results bypass the heuristic gate", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-05T00:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const interim = (overrides: Partial<Parameters<ReturnType<typeof createSentenceSession>["handleInterim"]>[0]>) => ({
    segmentId: 1,
    sessionId: "session-1",
    transcriptText: "",
    resultEndTimeMs: 0,
    isFinal: false,
    confidence: 0.9,
    ...overrides,
  });

  it("emits a short final without punctuation or a Korean ending", () => {
    const onSentence = vi.fn();
    const session = createSentenceSession(onSentence);

    session.handleInterim(
      interim({ transcriptText: "아멘", resultEndTimeMs: 10, isFinal: true })
    );

    expect(onSentence).toHaveBeenCalledWith("아멘", true, 10, 0.9, 1, "session-1");
  });

  it("emits a final even when the rate limit is exhausted", () => {
    const onSentence = vi.fn();
    const session = createSentenceSession(onSentence);

    // Drain the 3-token bucket with gate-passing interims (same fake instant,
    // so no refill happens in between).
    for (let i = 1; i <= 3; i++) {
      session.handleInterim(
        interim({ transcriptText: `중간 결과 ${i}번입니다`, resultEndTimeMs: i })
      );
    }
    expect(onSentence).toHaveBeenCalledTimes(3);

    session.handleInterim(
      interim({ transcriptText: "마지막 최종 문장입니다", resultEndTimeMs: 99, isFinal: true })
    );

    expect(onSentence).toHaveBeenCalledTimes(4);
  });

  it("still gates a short non-final interim", () => {
    const onSentence = vi.fn();
    const session = createSentenceSession(onSentence);

    session.handleInterim(interim({ transcriptText: "짧은 중간", resultEndTimeMs: 5 }));

    expect(onSentence).not.toHaveBeenCalled();
  });
});
