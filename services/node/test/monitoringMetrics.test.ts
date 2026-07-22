import { beforeEach, describe, expect, it, vi } from "vitest";
import { register } from "prom-client";
import {
  setSttPaused,
  incFfmpegStale,
  setPublishBufferDropped,
  setPublishBufferSize,
  setRtmpAuthEnabled,
} from "../src/monitoring/metrics.js";
import { RetryingTranscriptPublisher } from "../src/retryingPublisher.js";
import type { PublishEvent } from "../src/ports/transcriptPublisher.js";

const metricValue = async (name: string): Promise<number | undefined> => {
  const metrics = await register.getMetricsAsJSON();
  const metric = metrics.find((m) => m.name === name);
  const values = (metric as { values?: { value: number }[] } | undefined)
    ?.values;
  return values?.[0]?.value;
};

describe("monitoring metrics module", () => {
  it("stt paused gauge follows pause/resume", async () => {
    setSttPaused(true);
    expect(await metricValue("neemba_stt_paused")).toBe(1);
    setSttPaused(false);
    expect(await metricValue("neemba_stt_paused")).toBe(0);
  });

  it("ffmpeg stale counter accumulates", async () => {
    const before = (await metricValue("neemba_ffmpeg_stale_total")) ?? 0;
    incFfmpegStale();
    incFfmpegStale();
    expect(await metricValue("neemba_ffmpeg_stale_total")).toBe(before + 2);
  });

  it("publish buffer gauges track absolute values", async () => {
    setPublishBufferDropped(3);
    setPublishBufferSize(7);
    expect(await metricValue("neemba_publish_buffer_dropped_total")).toBe(3);
    expect(await metricValue("neemba_publish_buffer_size")).toBe(7);
  });

  it("rtmp auth gauge reflects key presence", async () => {
    setRtmpAuthEnabled(false);
    expect(await metricValue("neemba_rtmp_auth_enabled")).toBe(0);
    setRtmpAuthEnabled(true);
    expect(await metricValue("neemba_rtmp_auth_enabled")).toBe(1);
  });
});

describe("RetryingTranscriptPublisher hooks", () => {
  const makeEvent = (sequence: number): PublishEvent => ({
    sessionId: "s1",
    segmentId: 1,
    sequence,
    transcriptText: `t${sequence}`,
    sourceLanguage: "ko-KR",
    targetLanguage: "en-US",
    sampleRateHz: 16000,
    createdAt: new Date().toISOString(),
  });

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-22T00:00:00Z"));
  });

  it("reports queue size on enqueue and drain, dropped total on stop", async () => {
    const sizes: number[] = [];
    const dropped: number[] = [];
    let fail = true;
    const publisher = new RetryingTranscriptPublisher(
      { publish: async () => { if (fail) throw new Error("down"); } },
      60_000,
      2_000,
      500,
      { onQueueSize: (n) => sizes.push(n), onDropped: (n) => dropped.push(n) }
    );

    await publisher.publish(makeEvent(1));
    await publisher.publish(makeEvent(2));
    await vi.advanceTimersByTimeAsync(0);
    expect(sizes).toContain(2);

    await publisher.stop();
    // stop() drops both buffered spans; the hook gets the cumulative total.
    expect(dropped[dropped.length - 1]).toBe(2);
    expect(sizes[sizes.length - 1]).toBe(0);

    fail = false;
    vi.useRealTimers();
  });
});
