import { type Readable } from "node:stream";
import type { SpeechToTextPort } from "../ports/sttPorts.js";
import type {
  AudioConsumerContext,
  AudioConsumerPort,
  StopStreaming,
} from "../ports/audioConsumerPort.js";
import {
  removeSessionMetrics,
  setPublishBufferSize,
  setSttPaused,
} from "../monitoring/metrics.js";
import type { ISegmentManager } from "../ports/segment.js";
import type { IInterfaceOrchestra } from "../ports/interimOrchestra.js";
import type { StreamSwitcher } from "../stream/StreamSwitcher.js";
import type { StreamHandle } from "../ports/streamSwitcher.js";
import { createSentenceSession } from "../sessions/createSentenceSession.js";

type RotationReason = "scheduled" | "error" | "resume";

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
  // §4-4-2: paused halts rotation (stops billing) after too many no-audio
  // errors, but unlike stopFlag it is recoverable — the pcm pump revives STT
  // when audio flows again. The old permanent give-up left the session a
  // zombie while ffmpeg kept retrying upstream (2026-07-19 incident).
  private paused = false;
  private restartTimer: NodeJS.Timeout | undefined;
  private rotationInFlight: Promise<void> | null = null;
  // Consecutive STT errors with no transcript in between. A disconnected
  // client stops sending audio → Google times out and errors ~every 5s;
  // rotating on each would recreate (and bill) streams forever. Reset to 0
  // whenever a transcript arrives (proof the client is still streaming).
  private consecutiveErrorRotations = 0;
  // Wall-clock of the last pcm chunk. Read by SessionLifecycle's no-publisher
  // timer: ffmpeg only emits here once it has actually pulled the RTMP url, so
  // a chunk is proof a publisher exists even when on_publish never fired for
  // this process (2026-08-24 incident).
  private lastAudioAtMs: number | null = null;

  constructor(
    private readonly sttPort: SpeechToTextPort,
    private readonly switcher: StreamSwitcher,
    private readonly interimOrchestra: IInterfaceOrchestra,
    private readonly segmentManager: ISegmentManager,
    private readonly restartIntervalMs = 285_000,
    private readonly restartRetryIntervalMs = 5_000,
    private readonly maxConsecutiveErrorRotations = 3
  ) {}

  async start(
    pcmReadable: Readable,
    context?: AudioConsumerContext
  ): Promise<StopStreaming> {
    // No fallback on purpose: the shared session slot let mic start/stop
    // silently redirect RTMP captions (docs/mic-rtmp-session-slot-plan.md §3).
    // Every caller must own its sessionId and pass it explicitly.
    const sessionId = context?.sessionId;

    if (!sessionId) {
      throw new Error("sessionId required: pass AudioConsumerContext.sessionId");
    }

    // Seed the session's series at 0 so consumers can tell "session running,
    // not paused" apart from "no such session". Both gauges are seeded here,
    // inside the try/catch that removes them: seeding anywhere outside a
    // teardown-covered scope leaks a series no path can delete.
    setSttPaused(sessionId, false);
    setPublishBufferSize(sessionId, 0);

    try {
      return await this._startWithSession(pcmReadable, sessionId);
    } catch (err) {
      // start() threw before handing back its stop closure, so no teardown
      // path will ever run — do it here. stopFlag first: a gRPC stream opened
      // before the throw still delivers its error callback, which would
      // otherwise rotate (and bill) a replacement stream and repaint the
      // gauges we are about to remove. dispose() before remove for the same
      // ordering reason as the stop closure below.
      this.stopFlag = true;
      this._clearRestartTimer();
      this.interimOrchestra.dispose();
      removeSessionMetrics(sessionId);
      throw err;
    }
  }

  lastAudioAt(): number | null {
    return this.lastAudioAtMs;
  }

  private async _startWithSession(
    pcmReadable: Readable,
    sessionId: string
  ): Promise<StopStreaming> {
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
        this.lastAudioAtMs = Date.now();
        if (this.paused) {
          // Audio is flowing again — arriving chunks are the same liveness
          // proof as a transcript, so reset the counter and revive STT.
          this.paused = false;
          this.consecutiveErrorRotations = 0;
          setSttPaused(sessionId, false);
          console.log(`Stt resuming: audio returned (session ${sessionId})`);
          await this._rotateStream(sessionId, session, "resume").catch(
            () => undefined
          );
        }
        await this.switcher.write(chunk);
      }
    })().catch((e) => console.error("pcm pump error", e));

    return async () => {
      this.stopFlag = true;
      this.paused = false;
      this._clearRestartTimer();
      await this.rotationInFlight?.catch(() => undefined);
      session.stop(sessionId);
      // dispose() stops the retry buffer, whose terminal onQueueSize(0)
      // fires synchronously inside this call — so removing the series AFTER
      // it cannot be undone by that report (late async reports are guarded
      // in RetryingTranscriptPublisher.notify).
      this.interimOrchestra.dispose();
      // Remove, don't zero: a stopped session must vanish from /metrics
      // entirely, or dead uuid labels pile up per session (cardinality
      // leak). Placed before shutdown() so a throw there can't skip it.
      removeSessionMetrics(sessionId);
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
        // A transcript proves audio is flowing → the client is alive.
        this.consecutiveErrorRotations = 0;
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
        if (this.stopFlag || this.paused) return;
        this.consecutiveErrorRotations += 1;
        if (this.consecutiveErrorRotations > this.maxConsecutiveErrorRotations) {
          // Client audio is gone: pause rotation so we stop recreating (and
          // billing) streams. NOT a permanent give-up — the pcm pump resumes
          // STT as soon as audio flows again (ffmpeg keeps retrying upstream,
          // so audio can return without a new session).
          console.warn(
            `Stt paused: ${this.consecutiveErrorRotations} consecutive errors with no audio — pausing rotation until audio returns (session ${sessionId})`
          );
          this.paused = true;
          setSttPaused(sessionId, true);
          this._clearRestartTimer();
          return;
        }
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
    // paused guard: a rotation that was already in flight when the pause hit
    // must not re-arm the timer and recreate streams behind the pause.
    if (this.stopFlag || this.paused) return;
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
