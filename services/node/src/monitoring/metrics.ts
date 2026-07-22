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

// Absolute-value gauges (not self-incrementing counters): the publisher
// already keeps its own cumulative droppedCount, so the hook just mirrors it.
const publishBufferDropped = new Gauge({
  name: "neemba_publish_buffer_dropped_total",
  help: "Spans dropped by the publish retry buffer (expired/capacity/stop)",
  registers: [register],
});

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

export const setSttPaused = (paused: boolean): void => {
  sttPaused.set(paused ? 1 : 0);
};

export const incFfmpegStale = (): void => {
  ffmpegStale.inc();
};

export const setPublishBufferDropped = (total: number): void => {
  publishBufferDropped.set(total);
};

export const setPublishBufferSize = (size: number): void => {
  publishBufferSize.set(size);
};

export const setRtmpAuthEnabled = (enabled: boolean): void => {
  rtmpAuthEnabled.set(enabled ? 1 : 0);
};
