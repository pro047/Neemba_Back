import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PassThrough } from "node:stream";
import { register } from "prom-client";
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

  const gaugeValue = async (
    name: string,
    labels?: Record<string, string>
  ): Promise<number | undefined> => {
    const metric = (await register.getMetricsAsJSON()).find(
      (m) => m.name === name
    );
    const values = (
      metric as
        | { values?: { value: number; labels?: Record<string, string> }[] }
        | undefined
    )?.values;
    if (!labels) return values?.[0]?.value;
    return values?.find((v) =>
      Object.entries(labels).every(([key, value]) => v.labels?.[key] === value)
    )?.value;
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

  // §4-4-2 (2026-07-19 incident): the old bound was a PERMANENT give-up
  // (stopFlag=true) while ffmpeg keeps retrying forever — once audio came
  // back the session stayed a zombie until a manual stop/start. The bound
  // must instead PAUSE rotation (billing still stops) and revive STT when
  // audio flows again.

  it("일시정지 후 오디오가 다시 유입되면 STT가 재개되어야 한다", async () => {
    // Arrange: exceed the error bound → rotation paused at 4 streams
    const { port, streams } = createFakeSttPort();
    const orchestrator = makeOrchestrator(port, 3);
    const pcm = new PassThrough();
    await orchestrator.start(pcm, { sessionId: "s1" });
    for (let i = 0; i < 4; i++) {
      streams[streams.length - 1].onError(new Error("Stream timed out"));
      await flush();
    }

    // Act: audio returns (ffmpeg reconnected upstream)
    pcm.write(Buffer.alloc(3200));
    await flush();
    await flush();

    // Assert: a fresh STT stream was created (4 paused + 1 resumed)
    expect(streams).toHaveLength(5);
  });

  it("정지(stop) 후에는 오디오가 유입돼도 STT를 재개하지 않아야 한다", async () => {
    // Arrange: a started session that is then stopped for real
    const { port, streams } = createFakeSttPort();
    const orchestrator = makeOrchestrator(port, 3);
    const pcm = new PassThrough();
    const stop = await orchestrator.start(pcm, { sessionId: "s1" });
    await stop();

    // Act: late audio after the session ended
    pcm.write(Buffer.alloc(3200));
    await flush();
    await flush();

    // Assert: no new stream beyond the initial one
    expect(streams).toHaveLength(1);
  });

  // 2026-07-30: prod 세션을 stop 한 뒤에도 neemba_stt_paused 가 1로 남아 있었다.
  // 라벨화 이후에는 0으로 되돌리는 대신 세션 시리즈 자체를 지운다 — 죽은
  // 세션의 uuid 라벨이 쌓이는 cardinality 누수까지 함께 막는다.
  it("일시정지 상태에서 세션을 정지하면 해당 세션의 stt_paused 시리즈가 제거되어야 한다", async () => {
    // Arrange: 에러 임계값을 넘겨 pause 상태로 만든다
    const { port, streams } = createFakeSttPort();
    const orchestrator = makeOrchestrator(port, 3);
    const pcm = new PassThrough();
    const stop = await orchestrator.start(pcm, { sessionId: "stop-remove" });
    for (let i = 0; i < 4; i++) {
      streams[streams.length - 1].onError(new Error("Stream timed out"));
      await flush();
    }
    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "stop-remove" })
    ).toBe(1);

    // Act
    await stop();

    // Assert
    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "stop-remove" })
    ).toBeUndefined();
  });

  // 세션 시작 시 0 시딩: 이전 프로세스 상태와 무관하게 "이 세션은 살아있고
  // 일시정지 아님"이 시리즈로 노출돼야 소비자가 세션 존재를 알 수 있다
  // (watch-service 스킬의 stale 1 오탐이 이 시딩+remove 로 함께 사라진다).
  it("세션을 시작하면 해당 세션의 stt_paused 시리즈가 0으로 노출되어야 한다", async () => {
    const { port } = createFakeSttPort();
    const orchestrator = makeOrchestrator(port, 3);

    await orchestrator.start(new PassThrough(), { sessionId: "seed-zero" });

    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "seed-zero" })
    ).toBe(0);
  });

  it("세션을 시작하면 해당 세션의 publish_buffer_size 시리즈가 0으로 노출되어야 한다", async () => {
    const { port } = createFakeSttPort();
    const orchestrator = makeOrchestrator(port, 3);

    await orchestrator.start(new PassThrough(), { sessionId: "seed-buf" });

    expect(
      await gaugeValue("neemba_publish_buffer_size", { sessionId: "seed-buf" })
    ).toBe(0);
  });

  // A port whose stream opens but fails to configure: start() throws after
  // the gRPC error callback is already registered.
  const createFailingSttPort = () => {
    const streams: { onError: ErrorCallback }[] = [];
    const port = {
      getRecognizer: async () => {},
      startStreaming(options: { onError: ErrorCallback }) {
        streams.push({ onError: options.onError });
        return {
          configureOnce: () => {
            throw new Error("configure failed");
          },
          writeAudioChunk: async () => {},
          stop: async () => {},
          isOpen: () => true,
        };
      },
    };
    return { port: port as unknown as SpeechToTextPort, streams };
  };

  // Seeding outside the scope that removes the series (assembly time) left a
  // failed start leaking a dead uuid label forever — the very cardinality
  // leak this labelling was meant to prevent.
  it("start가 실패하면 시딩된 두 시리즈가 모두 제거되어야 한다", async () => {
    const { port } = createFailingSttPort();
    const orchestrator = makeOrchestrator(port, 3);

    await expect(
      orchestrator.start(new PassThrough(), { sessionId: "fail-start" })
    ).rejects.toThrow("configure failed");

    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "fail-start" })
    ).toBeUndefined();
    expect(
      await gaugeValue("neemba_publish_buffer_size", { sessionId: "fail-start" })
    ).toBeUndefined();
  });

  // The stream opened before the throw still delivers errors. Without a
  // stopFlag in the failure path those errors rotate (and bill) replacement
  // streams and repaint the gauge that was just removed.
  it("start 실패 후 도착한 STT 에러는 스트림을 다시 만들거나 시리즈를 되살리지 않아야 한다", async () => {
    // Arrange
    const { port, streams } = createFailingSttPort();
    const orchestrator = makeOrchestrator(port, 3);
    await expect(
      orchestrator.start(new PassThrough(), { sessionId: "fail-late" })
    ).rejects.toThrow("configure failed");

    // Act: late gRPC errors from the stream opened before the failure
    for (let i = 0; i < 5; i++) {
      streams[0].onError(new Error("Stream timed out"));
      await flush();
    }

    // Assert
    expect(streams).toHaveLength(1);
    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "fail-late" })
    ).toBeUndefined();
  });

  // 결함 재현 대조군 (계획 §1): 라벨 없는 전역 게이지에서는 B 의 재개가
  // A 의 1 을 0 으로 덮어 "STT 동작"으로 위장됐다.
  it("한 세션이 일시정지 중일 때 다른 세션이 재개해도 일시정지 시리즈는 1로 유지되어야 한다", async () => {
    // Arrange: 세션 A 가 에러 임계 초과로 pause
    const a = createFakeSttPort();
    const orchestratorA = makeOrchestrator(a.port, 3);
    await orchestratorA.start(new PassThrough(), { sessionId: "label-a" });
    for (let i = 0; i < 4; i++) {
      a.streams[a.streams.length - 1].onError(new Error("Stream timed out"));
      await flush();
    }
    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "label-a" })
    ).toBe(1);

    // Act: 세션 B 가 pause 후 오디오 복귀로 재개
    const b = createFakeSttPort();
    const orchestratorB = makeOrchestrator(b.port, 3);
    const pcmB = new PassThrough();
    await orchestratorB.start(pcmB, { sessionId: "label-b" });
    for (let i = 0; i < 4; i++) {
      b.streams[b.streams.length - 1].onError(new Error("Stream timed out"));
      await flush();
    }
    pcmB.write(Buffer.alloc(3200));
    await flush();
    await flush();

    // Assert: B 는 0 으로 돌아오고 A 는 여전히 1
    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "label-b" })
    ).toBe(0);
    expect(
      await gaugeValue("neemba_stt_paused", { sessionId: "label-a" })
    ).toBe(1);
  });
});
