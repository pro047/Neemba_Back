import { describe, expect, it, vi } from "vitest";
import { PassThrough } from "node:stream";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { StreamOrchestrator } from "../src/usecases/StreamOrchestrator.js";
import { StreamlinkToConsumerService } from "../src/usecases/StreamlinkToConsumerService.js";
import { StreamSwitcher } from "../src/stream/StreamSwitcher.js";
import { createSessionLifecycle } from "../src/usecases/SessionLifecycle.js";
import {
  createStartMicSessionHandler,
  createStopMicSessionHandler,
  type PythonSessionClient,
} from "../src/router/mic.js";
import { createSessionRuntimeStore } from "../src/sessionRuntimeStore.js";
import type { SpeechToTextPort } from "../src/ports/sttPorts.js";
import type { AudioTranscoder } from "../src/ports/ports.js";

// 세션 슬롯 분리 (docs/mic-rtmp-session-slot-plan.md): RTMP 와 마이크가
// ports/sessionStore.ts 의 문자열 1개짜리 모듈 전역을 공유해서, RTMP 시작 구간
// (startPythonSession await 동안)에 마이크 [시작]/[정지]가 끼어들면 RTMP
// 오케스트레이터가 마이크의 sessionId(또는 빈 문자열)를 캡처했다 — 자막이
// 남의 세션으로 발행되거나 시작 자체가 throw 했다. 각 경로가 자기 sessionId 를
// 명시적으로 들고 다니면 두 경로 사이에 공유 상태가 0이어야 한다.

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

function createFakeFfmpeg(): AudioTranscoder {
  return {
    startTranscoder: () => ({
      inputWritable: new PassThrough(),
      pcmReadable: new PassThrough(),
      stop: () => {},
    }),
  } as unknown as AudioTranscoder;
}

function createMockResponse() {
  return {
    statusCode: 200,
    body: undefined as unknown,
    status(code: number) {
      this.statusCode = code;
      return this;
    },
    json(payload: unknown) {
      this.body = payload;
      return this;
    },
  };
}

// 실제 마이크 라우터 핸들러를 그대로 쓴다: 결함의 원인이 "마이크 시작/종료가
// 공유 상태에 쓰기를 한다"였으므로, 대조군은 마이크 경로의 실제 코드를 태워야
// 의미가 있다 (스텁으로 흉내 내면 수정 전에도 통과해 버린다).
function createMicHarness(sessionId: string) {
  const runtimeStore = createSessionRuntimeStore();
  const pythonClient: PythonSessionClient = {
    startSession: vi.fn(async () => ({
      sessionId,
      webSocketUrl: "ws://localhost/api/mic",
    })),
    stopSession: vi.fn(async () => STOPPED_RESPONSE),
  };
  const start = createStartMicSessionHandler({
    pythonClient,
    runtimeStore,
    scheduleConnectTeardown: () => {},
    micPipelineFactory: async () => ({
      inputWritable: new PassThrough(),
      stop: async () => {},
    }),
    sessionIdFactory: () => sessionId,
  });
  const stop = createStopMicSessionHandler({ pythonClient, runtimeStore });

  return {
    async startMic() {
      const res = createMockResponse();
      await start(
        { body: { sourceLang: "ko-KR", targetLang: "en-US" } } as never,
        res as never,
        () => {}
      );
      expect(res.statusCode).toBe(202);
    },
    async stopMic() {
      const res = createMockResponse();
      await stop(
        { body: { sessionId } } as never,
        res as never,
        () => {}
      );
      expect(res.statusCode).toBe(200);
    },
  };
}

// RTMP 하네스: SessionLifecycle → StreamlinkToConsumerService →
// StreamOrchestrator 는 실물이고, runPipelines 만 인라인으로 대체한다
// (실물은 FfmpegTranscoder·Google STT·NATS 를 하드 생성해서 단위로 못 태운다
// — 그 파일의 sessionId 전달은 tsc 의 필수 인자 검사가 지킨다).
// duringPythonStart 가 §3 의 재현 창 — sessionId 를 만든 뒤 파이프라인이 뜨기
// 전, 마이크 조작이 끼어드는 바로 그 지점이다.
function createRtmpHarness(duringPythonStart: () => Promise<void>) {
  const published: string[] = [];
  const { port, streams } = createFakeSttPort();
  const stops: (() => Promise<void>)[] = [];
  let counter = 0;

  const deps = {
    newSessionId: () => "rtmp-A",
    startPythonSession: async () => {
      await duringPythonStart();
      return { webSocketUrl: "ws://localhost/api/rtmp" };
    },
    startPipeline: async ({ sessionId }: { sessionId: string }) => {
      const orchestrator = new StreamOrchestrator(
        port,
        new StreamSwitcher(() => {}),
        {
          onSttResult: async (p: { sessionId: string }) => {
            published.push(p.sessionId);
          },
          dispose: () => {},
        } as never,
        { next: () => ++counter } as never
      );
      const service = new StreamlinkToConsumerService(
        createFakeFfmpeg(),
        orchestrator,
        sessionId
      );
      const stop = await service.run();
      stops.push(stop);
      return {
        stop,
        notifyPublisherReturned: () => {},
        lastAudioAt: () => orchestrator.lastAudioAt(),
      };
    },
    stopPythonSession: async () => {},
    recordStop: () => {},
    graceMs: () => 60_000,
    noPublisherGraceMs: () => 600_000,
  };
  const lifecycle = createSessionLifecycle(deps);

  return {
    lifecycle,
    published,
    async cleanup() {
      for (const stop of stops) await stop();
    },
    emitFinalTranscript(text: string) {
      streams[streams.length - 1].onTranscript({
        isFinal: true,
        transcriptText: text,
        resultEndTimeMs: 10,
        confidence: 0.9,
      });
    },
  };
}

// python /internal/sessions/stop 의 응답 모양. `ended` 는 이 호출이 세션을
// 실제로 끝냈는지를 나타내며 /mic/stop 이 그대로 통과시킨다.
const STOPPED_RESPONSE = { ok: true, ended: true, translationCount: 0 };

describe("마이크·RTMP 세션 슬롯 분리", () => {
  it("RTMP 시작 중 마이크 start 가 끼어들어도 RTMP 자막은 자기 sessionId 로 발행되어야 한다", async () => {
    // Arrange: RTMP 시작 구간(startPythonSession await)에 마이크 [시작]이 낀다
    const mic = createMicHarness("mic-B");
    const rtmp = createRtmpHarness(() => mic.startMic());

    // Act
    const result = await rtmp.lifecycle.start({
      sourceLang: "ko-KR",
      targetLang: "en-US",
    });
    rtmp.emitFinalTranscript("자막 테스트입니다");

    // Assert: 전역 공유 시절에는 마이크가 덮어쓴 "mic-B" 로 발행됐다
    expect(result.sessionId).toBe("rtmp-A");
    expect(rtmp.published).toEqual(["rtmp-A"]);
    await rtmp.cleanup();
  });

  it("마이크 stop 직후에 RTMP 세션을 시작해도 자막 발행이 throw 하지 않아야 한다", async () => {
    // Arrange: 같은 창에서 마이크가 start → stop 까지 마친다 (전역 시절엔
    // removeSessionId 로 슬롯이 "" 가 되어 RTMP 쪽이 throw 했다)
    const mic = createMicHarness("mic-C");
    const rtmp = createRtmpHarness(async () => {
      await mic.startMic();
      await mic.stopMic();
    });

    // Act & Assert
    await expect(
      rtmp.lifecycle.start({ sourceLang: "ko-KR", targetLang: "en-US" })
    ).resolves.toMatchObject({ sessionId: "rtmp-A" });
    rtmp.emitFinalTranscript("정지 후에도 삽니다");
    expect(rtmp.published).toEqual(["rtmp-A"]);
    await rtmp.cleanup();
  });

  it("공유 세션 슬롯(sessionStore) 참조가 src 에서 사라져야 한다", () => {
    // Arrange: src 아래 모든 .ts 를 훑는다
    const srcRoot = join(__dirname, "..", "src");
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (entry.name.endsWith(".ts")) files.push(full);
      }
    };
    walk(srcRoot);

    // Act: import 구문만 잡는다 — 심볼 이름으로 넓게 걸면 주석·산문의 단어에도
    // 걸리고, 정작 다른 이름으로 부활한 전역은 못 잡는다. 행동은 위 두 테스트가
    // 지키고, 이 테스트는 삭제된 모듈이 다시 배선되는 것만 막는다.
    const offenders = files.filter((file) =>
      /from\s+["'][^"']*ports\/sessionStore/.test(readFileSync(file, "utf8"))
    );

    // Assert: 폴백이 되살아나면 (docs 계획 §4 D1-A 함정) 여기서 잡힌다
    expect(offenders).toEqual([]);
  });
});
