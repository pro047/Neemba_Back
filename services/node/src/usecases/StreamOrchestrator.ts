import { type Readable } from "node:stream";
import type { SpeechToTextPort } from "../ports/sttPorts.js";
import type {
  AudioConsumerContext,
  AudioConsumerPort,
  StopStreaming,
} from "../ports/audioConsumerPort.js";
import { getSessionId } from "../ports/sessionStore.js";
import type { ISegmentManager } from "../ports/segment.js";
import type { IInterfaceOrchestra } from "../ports/interimOrchestra.js";
import type { StreamSwitcher } from "../stream/StreamSwitcher.js";
import type { StreamHandle } from "../ports/streamSwitcher.js";
import { createSentenceSession } from "../sessions/createSentenceSession.js";

type RotationReason = "scheduled" | "error";

function makeGoogleHandle(
  google: ReturnType<SpeechToTextPort["startStreaming"]>
): StreamHandle {
  return {
    write: (data: Buffer) => google.writeAudioChunk(data),
    close: () => google.stop(),
    isOpen: () => google.isOpen(),
  };
}

export class StreamOrchestrator implements AudioConsumerPort {
  private stopFlag = false;
  private restartTimer: NodeJS.Timeout | undefined;
  private rotationInFlight: Promise<void> | null = null;

  constructor(
    private readonly sttPort: SpeechToTextPort,
    private readonly switcher: StreamSwitcher,
    private readonly interimOrchestra: IInterfaceOrchestra,
    private readonly segmentManager: ISegmentManager,
    private readonly restartIntervalMs = 285_000,
    private readonly restartRetryIntervalMs = 5_000
  ) {}

  async start(
    pcmReadable: Readable,
    context?: AudioConsumerContext
  ): Promise<StopStreaming> {
    const sessionId = context?.sessionId ?? getSessionId();

    if (!sessionId) {
      throw new Error("sessionId required for mic streaming");
    }

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

        // Fire-and-forget: without the catch, a rejection here is unhandled
        // and crashes the process (defense in depth — publishSpan also
        // contains its own failures).
        this.interimOrchestra
          .onSttResult({
            transcript: text,
            isFinal: isFinal,
            resultEndTimeMs: endTimeMilliseconds,
            confidence,
            segmentId,
            sessionId,
          })
          .catch((err) => console.error("interim onSttResult error", err));
      }
    );

    const firstHandle = this._createSttHandle(
      session,
      sessionId,
      sessionSegmentId
    );

    await this.switcher.handoff(firstHandle, sessionSegmentId);
    this._scheduleNextRestart(sessionId, session);

    (async () => {
      for await (const chunk of pcmReadable as unknown as AsyncIterable<Buffer>) {
        if (this.stopFlag) return;
        await this.switcher.write(chunk);
      }
    })().catch((e) => console.error("pcm pump error", e));

    return async () => {
      this.stopFlag = true;
      this._clearRestartTimer();
      await this.rotationInFlight?.catch(() => undefined);
      session.stop(sessionId);
      this.interimOrchestra.dispose();
      await this.switcher.shutdown();
      console.log("stream stopped");
    };
  }

  private _createSttHandle(
    session: ReturnType<typeof createSentenceSession>,
    sessionId: string,
    segmentId: number
  ): StreamHandle {
    const stt = this.sttPort.startStreaming({
      languageCodes: ["ko-KR"],
      model: "latest_long",
      onTranscript: (p) => {
        // Attribute results to the stream that produced them: after a
        // rotation the old stream still flushes its last results, and those
        // must keep the OLD segmentId instead of adopting the switcher's
        // current one.
        session.handleInterim({
          ...p,
          segmentId: segmentId,
          sessionId: sessionId,
        });
      },
      onError: (e) => {
        console.log("Stt error :", e);
        this._rotateStream(sessionId, session, "error").catch(
          () => undefined
        );
      },
    });
    try {
      stt.configureOnce();
    } catch (err) {
      stt.stop();
      throw err;
    }
    return makeGoogleHandle(stt);
  }

  private _scheduleNextRestart(
    sessionId: string,
    session: ReturnType<typeof createSentenceSession>,
    delayMs = this.restartIntervalMs,
    reason: RotationReason = "scheduled"
  ) {
    if (this.stopFlag) return;
    this._clearRestartTimer();
    this.restartTimer = setTimeout(() => {
      this._rotateStream(sessionId, session, reason).catch(() => undefined);
    }, delayMs);
  }

  private _clearRestartTimer() {
    if (!this.restartTimer) return;
    clearTimeout(this.restartTimer);
    this.restartTimer = undefined;
  }

  private _rotateStream(
    sessionId: string,
    session: ReturnType<typeof createSentenceSession>,
    reason: RotationReason
  ): Promise<void> {
    if (this.rotationInFlight) return this.rotationInFlight;

    const work = (async () => {
      if (this.stopFlag) return;
      this._clearRestartTimer();
      try {
        const nextSegmentId = this.segmentManager.next(sessionId);
        const nextHandle = this._createSttHandle(
          session,
          sessionId,
          nextSegmentId
        );
        await this.switcher.handoff(nextHandle, nextSegmentId);
        this._scheduleNextRestart(sessionId, session);
      } catch (err) {
        console.error(`stream rotation failed (${reason})`, err);
        this._scheduleNextRestart(
          sessionId,
          session,
          this.restartRetryIntervalMs,
          "error"
        );
      }
    })();

    this.rotationInFlight = work.finally(() => {
      this.rotationInFlight = null;
    });

    return this.rotationInFlight;
  }
}
