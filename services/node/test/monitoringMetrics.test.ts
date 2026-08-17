import { beforeEach, describe, expect, it, vi } from "vitest";
import { register } from "prom-client";
import {
  setSttPaused,
  incFfmpegStale,
  incPublishBufferDropped,
  setPublishBufferSize,
  setRtmpAuthEnabled,
  incSessionStopped,
  removeSessionMetrics,
} from "../src/monitoring/metrics.js";
import { RetryingTranscriptPublisher } from "../src/retryingPublisher.js";
import type { PublishEvent } from "../src/ports/transcriptPublisher.js";

type LabelledValue = { value: number; labels?: Record<string, string> };

const metricValue = async (
  name: string,
  labels?: Record<string, string>
): Promise<number | undefined> => {
  const metrics = await register.getMetricsAsJSON();
  const metric = metrics.find((m) => m.name === name);
  const values = (metric as { values?: LabelledValue[] } | undefined)?.values;
  if (!labels) return values?.[0]?.value;
  return values?.find((v) =>
    Object.entries(labels).every(([key, value]) => v.labels?.[key] === value)
  )?.value;
};

describe("monitoring metrics module", () => {
  it("stt paused gauge follows pause/resume", async () => {
    setSttPaused("mm-a", true);
    expect(await metricValue("neemba_stt_paused", { sessionId: "mm-a" })).toBe(
      1
    );
    setSttPaused("mm-a", false);
    expect(await metricValue("neemba_stt_paused", { sessionId: "mm-a" })).toBe(
      0
    );
  });

  // Since PR #91 there are N orchestrators at once, and an unlabelled global
  // gauge was last-writer-wins: one session's failure got overwritten with 0
  // by another session.
  it("세션별 stt_paused 시리즈는 서로 덮어쓰지 않아야 한다", async () => {
    setSttPaused("mm-iso-a", true);
    setSttPaused("mm-iso-b", false);

    expect(
      await metricValue("neemba_stt_paused", { sessionId: "mm-iso-a" })
    ).toBe(1);
    expect(
      await metricValue("neemba_stt_paused", { sessionId: "mm-iso-b" })
    ).toBe(0);
  });

  it("세션별 publish buffer 시리즈는 서로 덮어쓰지 않아야 한다", async () => {
    setPublishBufferSize("mm-buf-a", 7);
    setPublishBufferSize("mm-buf-b", 2);

    expect(
      await metricValue("neemba_publish_buffer_size", { sessionId: "mm-buf-a" })
    ).toBe(7);
    expect(
      await metricValue("neemba_publish_buffer_size", { sessionId: "mm-buf-b" })
    ).toBe(2);
  });

  // Every session adds a uuid label, so a teardown that does not remove the
  // series piles up dead labels forever (cardinality leak).
  it("removeSessionMetrics는 해당 세션의 시리즈만 지워야 한다", async () => {
    setSttPaused("mm-rm-a", true);
    setSttPaused("mm-rm-b", true);
    setPublishBufferSize("mm-rm-a", 5);

    removeSessionMetrics("mm-rm-a");

    expect(
      await metricValue("neemba_stt_paused", { sessionId: "mm-rm-a" })
    ).toBeUndefined();
    expect(
      await metricValue("neemba_publish_buffer_size", { sessionId: "mm-rm-a" })
    ).toBeUndefined();
    expect(
      await metricValue("neemba_stt_paused", { sessionId: "mm-rm-b" })
    ).toBe(1);
  });

  it("ffmpeg stale counter accumulates", async () => {
    const before = (await metricValue("neemba_ffmpeg_stale_total")) ?? 0;
    incFfmpegStale();
    incFfmpegStale();
    expect(await metricValue("neemba_ffmpeg_stale_total")).toBe(before + 2);
  });

  it("publish buffer size gauge tracks the absolute queue depth", async () => {
    setPublishBufferSize("mm-depth", 7);
    expect(
      await metricValue("neemba_publish_buffer_size", { sessionId: "mm-depth" })
    ).toBe(7);
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

  // Teardown removes the session's labelled series, but a publish that
  // resolves late — after stop — would call onQueueSize again and resurrect
  // the removed series as a permanent 0 (cardinality leak).
  it("stop 이후 늦게 끝난 publish는 큐 크기를 다시 보고하지 않아야 한다", async () => {
    // Arrange: inner publish that stays in flight until we release it
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const sizes: number[] = [];
    const publisher = new RetryingTranscriptPublisher(
      { publish: () => gate },
      60_000,
      2_000,
      500,
      { onQueueSize: (n) => sizes.push(n) }
    );
    await publisher.publish(makeEvent(1));
    await vi.advanceTimersByTimeAsync(0);
    await publisher.stop();
    const reportsAfterStop = sizes.length;
    expect(sizes[reportsAfterStop - 1]).toBe(0);

    // Act: the in-flight publish resolves only after stop
    release();
    await vi.advanceTimersByTimeAsync(0);

    // Assert: stop's terminal 0 must stay the last report
    expect(sizes.length).toBe(reportsAfterStop);

    vi.useRealTimers();
  });

  // The terminal 0 report deliberately bypasses the late-report guard, so a
  // second stop would re-fire it after removeSessionMetrics and resurrect the
  // series. Today only the callers' own stop guards prevent that.
  it("stop을 두 번 호출해도 종단 큐 크기 보고는 한 번만 나가야 한다", async () => {
    // Arrange
    const sizes: number[] = [];
    const publisher = new RetryingTranscriptPublisher(
      { publish: async () => {} },
      60_000,
      2_000,
      500,
      { onQueueSize: (n) => sizes.push(n) }
    );
    await publisher.stop();
    const reportsAfterStop = sizes.length;

    // Act
    await publisher.stop();

    // Assert
    expect(sizes.length).toBe(reportsAfterStop);

    vi.useRealTimers();
  });
});
