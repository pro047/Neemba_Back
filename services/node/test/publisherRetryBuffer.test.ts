import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RetryingTranscriptPublisher } from "../src/retryingPublisher.js";
import type { PublishEvent } from "../src/ports/transcriptPublisher.js";

// §4-4 backpressure: the retry buffer must survive a NATS blip without
// losing spans, without reordering them (the consumer's per-session sequence
// watermark drops seq<=last as duplicates, so an overtaken span is lost for
// good), and without growing unbounded.

const makeEvent = (sequence: number): PublishEvent => ({
  sessionId: "s1",
  segmentId: 1,
  sequence,
  transcriptText: `text-${sequence}`,
  sourceLanguage: "ko-KR",
  targetLanguage: "en-US",
  sampleRateHz: 16000,
  createdAt: new Date().toISOString(),
});

type InnerControl = {
  published: PublishEvent[];
  fail: boolean;
};

const makeInner = (): {
  control: InnerControl;
  publish: (event: PublishEvent) => Promise<void>;
} => {
  const control: InnerControl = { published: [], fail: false };
  return {
    control,
    publish: async (event: PublishEvent) => {
      if (control.fail) throw new Error("nats down");
      control.published.push(event);
    },
  };
};

describe("RetryingTranscriptPublisher", () => {
  let inner: ReturnType<typeof makeInner>;
  let publisher: RetryingTranscriptPublisher;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-20T00:00:00Z"));
    inner = makeInner();
    publisher = new RetryingTranscriptPublisher({
      publish: inner.publish,
      start: async () => {},
      stop: async () => {},
    });
  });

  afterEach(async () => {
    await publisher.stop();
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("publishes immediately when the inner publisher is healthy", async () => {
    await publisher.publish(makeEvent(1));
    await vi.advanceTimersByTimeAsync(0);
    expect(inner.control.published.map((e) => e.sequence)).toEqual([1]);
  });

  it("retains failed spans and republishes them in order after recovery", async () => {
    inner.control.fail = true;
    await publisher.publish(makeEvent(1));
    await publisher.publish(makeEvent(2));
    await vi.advanceTimersByTimeAsync(0);
    expect(inner.control.published).toEqual([]);

    inner.control.fail = false;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(inner.control.published.map((e) => e.sequence)).toEqual([1, 2]);
  });

  it("does not let a new publish overtake spans buffered during an outage", async () => {
    inner.control.fail = true;
    await publisher.publish(makeEvent(1));
    await vi.advanceTimersByTimeAsync(0);

    inner.control.fail = false;
    // Published while the drain loop is still waiting for its retry delay:
    // it must queue behind sequence 1, not jump ahead.
    await publisher.publish(makeEvent(2));
    await vi.advanceTimersByTimeAsync(5_000);
    expect(inner.control.published.map((e) => e.sequence)).toEqual([1, 2]);
  });

  it("drops spans older than the TTL and counts them", async () => {
    inner.control.fail = true;
    await publisher.publish(makeEvent(1));
    await vi.advanceTimersByTimeAsync(0);

    // Past the 60s TTL: the span is stale subtitle text, not worth showing.
    await vi.advanceTimersByTimeAsync(61_000);
    inner.control.fail = false;
    await vi.advanceTimersByTimeAsync(5_000);

    expect(inner.control.published).toEqual([]);
    expect(publisher.droppedCount).toBe(1);
  });

  it("keeps fresh spans while dropping only the expired ones", async () => {
    inner.control.fail = true;
    await publisher.publish(makeEvent(1));
    await vi.advanceTimersByTimeAsync(30_000);
    await publisher.publish(makeEvent(2));
    // seq 1 is now 61s old (expired); seq 2 is 31s old (fresh).
    await vi.advanceTimersByTimeAsync(31_000);

    inner.control.fail = false;
    await vi.advanceTimersByTimeAsync(5_000);

    expect(inner.control.published.map((e) => e.sequence)).toEqual([2]);
    expect(publisher.droppedCount).toBe(1);
  });

  it("evicts the oldest span beyond the hard cap", async () => {
    inner.control.fail = true;
    for (let seq = 1; seq <= 501; seq++) {
      await publisher.publish(makeEvent(seq));
    }
    await vi.advanceTimersByTimeAsync(0);

    inner.control.fail = false;
    await vi.advanceTimersByTimeAsync(5_000);

    const sequences = inner.control.published.map((e) => e.sequence);
    expect(sequences).toHaveLength(500);
    expect(sequences[0]).toBe(2); // seq 1 evicted as oldest
    expect(sequences[sequences.length - 1]).toBe(501);
    expect(publisher.droppedCount).toBe(1);
  });

  it("stop() drops the remaining buffer immediately and logs the count", async () => {
    inner.control.fail = true;
    await publisher.publish(makeEvent(1));
    await publisher.publish(makeEvent(2));
    await vi.advanceTimersByTimeAsync(0);

    await publisher.stop();

    expect(publisher.droppedCount).toBe(2);
    // A publish after stop must not resurrect the drain loop.
    inner.control.fail = false;
    await publisher.publish(makeEvent(3));
    await vi.advanceTimersByTimeAsync(5_000);
    expect(inner.control.published).toEqual([]);
  });
});
