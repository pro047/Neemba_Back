import { type Readable } from "node:stream";
import type { SpeechToTextPort } from "../ports/sttPorts";
import type {
  AudioConsumerPort,
  StopStreaming,
} from "../ports/audioConsumerPort";
import { getSessionId } from "../sessions/sessionStore";
import type { ISegmentManager } from "../ports/segment";
import type { IInterfaceOrchestra } from "../ports/interimOrchestra";
import type { StreamSwitcher } from "../stream/StreamSwitcher";
import type { StreamHandle } from "../ports/streamSwitcher";
import { createSentenceSession } from "../sessions/createSentenceSession";

function makeGoogleHandle(
  google: ReturnType<SpeechToTextPort["startStreaming"]>
): StreamHandle {
  return {
    write: (data: Buffer) => google.writeAudioChunk(data),
    close: () => google.stop(),
    isOpen: () => true,
  };
}

export class StreamOrchestrator implements AudioConsumerPort {
  private stopFlag = false;
  private restartTimer?: NodeJS.Timeout;

  constructor(
    private readonly sttPort: SpeechToTextPort,
    private readonly switcher: StreamSwitcher,
    private readonly interimOrchestra: IInterfaceOrchestra,
    private readonly segmentManager: ISegmentManager,
    private readonly restartIntervalMs = 285_000
  ) {}

  async start(pcmReadable: Readable): Promise<StopStreaming> {
    const sessionId = getSessionId();

    const sessionSegmentId = this.segmentManager.next(sessionId);

    const session = createSentenceSession(
      (
        text,
        isFinal,
        endTimeMilliseconds,
        confidence,
        segmentId,
        sessionId
      ) => {
        if (!text.trim()) return;
        this.interimOrchestra.onSttResult({
          transcript: text,
          isFinal: isFinal,
          resultEndTimeMs: endTimeMilliseconds,
          confidence,
          segmentId,
          sessionId,
        });
      }
    );

    const firstHandle = this._createSttHandle(session, sessionId);

    await this.switcher.handoff(firstHandle, sessionSegmentId);

    (async () => {
      for await (const chunk of pcmReadable as unknown as AsyncIterable<Buffer>) {
        if (this.stopFlag) return;
        await this.switcher.write(chunk);
      }
    })().catch((e) => console.error("pcm pump error", e));

    this._scheduleOverlap(sessionId, session);

    return async () => {
      this.stopFlag = true;
      clearTimeout(this.restartTimer);
      session.stop();
      this.interimOrchestra.dispose();
      this.switcher.shutdown();
      console.log("stream stopped");
    };
  }

  private _createSttHandle(
    session: ReturnType<typeof createSentenceSession>,
    sessionId: string
  ): StreamHandle {
    const stt = this.sttPort.startStreaming({
      languageCodes: ["ko-KR"],
      model: "latest_long",
      onTranscript: (p) => {
        const segmentId = this.switcher.currentSegmentId();
        session.handleInterim({
          ...p,
          segmentId: segmentId,
          sessionId: sessionId,
        });
      },
      onError: (e) => console.log("Stt error :", e),
    });
    try {
      stt.configureOnce();
    } catch (err) {
      stt.stop();
      throw err;
    }
    return makeGoogleHandle(stt);
  }

  private _scheduleOverlap(
    sessionId: string,
    session: ReturnType<typeof createSentenceSession>
  ) {
    const tick = async () => {
      if (this.stopFlag) return;
      const nextSegmentId = this.segmentManager.next(sessionId);
      const nextHandle = this._createSttHandle(session, sessionId);

      await this.switcher.handoff(nextHandle, nextSegmentId);

      this.restartTimer = setTimeout(tick, this.restartIntervalMs);
    };
    this.restartTimer = setTimeout(tick, this.restartIntervalMs);
  }
}
