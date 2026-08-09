import { Counter, Gauge, register } from "prom-client";

// Domain metrics for the monitoring sidecar. Registered on prom-client's
// default register; app.ts merges it with its local registry when serving
// /metrics, so instrumented modules never need the app instance.

const sttPaused = new Gauge({
  name: "neemba_stt_paused",
  help: "1 while STT rotation is paused waiting for audio to return",
  registers: [register],
});

const ffmpegStale = new Counter({
  name: "neemba_ffmpeg_stale_total",
  help: "Times the ffmpeg process went 10s without progress",
  registers: [register],
});

// A real process-lifetime counter, not a mirror of the buffer's own tally:
// createStreamOrchestrator builds a fresh RetryingTranscriptPublisher per
// session, so setting an instance-local total reset the series to 0 on every
// session swap. The sidecar reads this as a counter (delta between scrapes)
// and ignores negative deltas, which hid five real subtitle losses on
// 2026-08-02 (§11 F-2). Hooks therefore report increments, not totals.
const publishBufferDropped = new Counter({
  name: "neemba_publish_buffer_dropped_total",
  help: "Spans dropped by the publish retry buffer (expired/capacity/stop)",
  registers: [register],
});

// Instance-scoped gauge: with one live session at a time the last writer is
// the current publisher. A late stop() from a replaced instance can still
// zero it for one scrape — acceptable for a queue-depth gauge, unlike the
// loss counter above.
const publishBufferSize = new Gauge({
  name: "neemba_publish_buffer_size",
  help: "Spans currently waiting in the publish retry buffer",
  registers: [register],
});

const rtmpAuthEnabled = new Gauge({
  name: "neemba_rtmp_auth_enabled",
  help: "1 when RTMP_PUBLISH_KEY auth is enforced",
  registers: [register],
});

// "manual" and "superseded" no longer have a producer as of 멀티 청취자 P1:
// D4 demoted POST /sessions/stop to a no-op (a listener pressing 정지 must not
// kill someone else's broadcast) and D1 turned start into an idempotent join,
// so nothing supersedes a live session. Both labels stay so the existing time
// series keeps its shape — dropping a label makes prom-client omit the line
// entirely, which the sidecar cannot tell apart from a scrape failure.
// Every teardown now arrives as "publisher_done". If "manual" or "superseded"
// ever increments again, a teardown path came back that P1 was meant to remove.
export type SessionStopReason = "manual" | "publisher_done" | "superseded";

const SESSION_STOP_REASONS: SessionStopReason[] = [
  "manual",
  "publisher_done",
  "superseded",
];

// One labelled counter instead of three names: the point is the ratio between
// the reasons ("how often does the operator forget to press stop?"), which is
// only readable if they share a series.
const sessionStopped = new Counter({
  name: "neemba_session_stopped_total",
  help: "Sessions torn down, labelled by what triggered the teardown",
  labelNames: ["reason"] as const,
  registers: [register],
});

// Seed every label at 0. prom-client omits a label combination until its first
// inc(), and the monitor sidecar keys off the exact exposition line — an absent
// series and a zero one would be indistinguishable on its first scrape.
for (const reason of SESSION_STOP_REASONS) {
  sessionStopped.inc({ reason }, 0);
}

export const setSttPaused = (paused: boolean): void => {
  sttPaused.set(paused ? 1 : 0);
};

export const incFfmpegStale = (): void => {
  ffmpegStale.inc();
};

export const incPublishBufferDropped = (dropped: number): void => {
  publishBufferDropped.inc(dropped);
};

export const setPublishBufferSize = (size: number): void => {
  publishBufferSize.set(size);
};

export const setRtmpAuthEnabled = (enabled: boolean): void => {
  rtmpAuthEnabled.set(enabled ? 1 : 0);
};

export const incSessionStopped = (reason: SessionStopReason): void => {
  sessionStopped.inc({ reason });
};
