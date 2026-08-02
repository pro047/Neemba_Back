import { beforeEach, describe, expect, it, vi } from "vitest";
import { register } from "prom-client";
import {
  setSttPaused,
  incFfmpegStale,
  incPublishBufferDropped,
  setPublishBufferSize,
  setRtmpAuthEnabled,
  incSessionStopped,
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

  it("publish buffer size gauge tracks the absolute queue depth", async () => {
    setPublishBufferSize(7);
    expect(await metricValue("neemba_publish_buffer_size")).toBe(7);
  });

  it("publish buffer dropped counter accumulates instead of being overwritten", async () => {
    const before = (await metricValue("neemba_publish_buffer_dropped_total")) ?? 0;
    incPublishBufferDropped(2);
    incPublishBufferDropped(1);
    expect(await metricValue("neemba_publish_buffer_dropped_total")).toBe(
      before + 3
    );
  });

  it("rtmp auth gauge reflects key presence", async () => {
    setRtmpAuthEnabled(false);
    expect(await metricValue("neemba_rtmp_auth_enabled")).toBe(0);
    setRtmpAuthEnabled(true);
    expect(await metricValue("neemba_rtmp_auth_enabled")).toBe(1);
  });

  // The monitor sidecar keys metrics by the whole exposition line, so its rule
  // string embeds the rendered label. Pin the exact text here: renaming the
  // metric or the label would otherwise silence the alert with no test failing.
  it("session stop counter exposes every reason label from boot", async () => {
    const exposition = await register.metrics();

    expect(exposition).toContain('neemba_session_stopped_total{reason="manual"} 0');
    expect(exposition).toContain(
      'neemba_session_stopped_total{reason="publisher_done"} 0'
    );
    expect(exposition).toContain(
      'neemba_session_stopped_total{reason="superseded"} 0'
    );

    incSessionStopped("publisher_done");
    expect(await register.metrics()).toContain(
      'neemba_session_stopped_total{reason="publisher_done"} 1'
    );
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

  const makeStuckPublisher = (hooks: {
    onQueueSize?: (n: number) => void;
    onDropped?: (n: number) => void;
  }) =>
    new RetryingTranscriptPublisher(
      { publish: async () => { throw new Error("down"); } },
      60_000,
      2_000,
      500,
      hooks
    );

  it("reports queue size on enqueue and drain, drop increments as they happen", async () => {
    const sizes: number[] = [];
    const dropped: number[] = [];
    const publisher = makeStuckPublisher({
      onQueueSize: (n) => sizes.push(n),
      onDropped: (n) => dropped.push(n),
    });

    await publisher.publish(makeEvent(1));
    await publisher.publish(makeEvent(2));
    await vi.advanceTimersByTimeAsync(0);
    expect(sizes).toContain(2);
    // Nothing dropped yet: a publish that only queues must not touch the
    // counter (a cumulative-total hook fired on every publish instead).
    expect(dropped).toEqual([]);

    await publisher.stop();
    expect(dropped).toEqual([2]);
    expect(sizes[sizes.length - 1]).toBe(0);

    vi.useRealTimers();
  });

  // §11 F-2: one publisher is built per session, so a session swap used to
  // reset the exported total to 0 and hide the losses from the sidecar's
  // delta rule (2026-08-02: 5 spans lost, metric read 0).
  it("keeps counting drops across a publisher instance swap", async () => {
    const before = (await metricValue("neemba_publish_buffer_dropped_total")) ?? 0;

    const first = makeStuckPublisher({ onDropped: incPublishBufferDropped });
    await first.publish(makeEvent(1));
    await vi.advanceTimersByTimeAsync(0);
    await first.stop();

    const second = makeStuckPublisher({ onDropped: incPublishBufferDropped });
    await second.publish(makeEvent(2));
    await vi.advanceTimersByTimeAsync(0);
    await second.stop();

    expect(await metricValue("neemba_publish_buffer_dropped_total")).toBe(
      before + 2
    );
    vi.useRealTimers();
  });
});
