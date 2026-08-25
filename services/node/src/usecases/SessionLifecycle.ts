import type { SessionStopReason } from "../monitoring/metrics.js";

// Owns the single live session: who is publishing, which pipeline is running,
// and every path that closes it. Before this existed the teardown sequence was
// duplicated across /sessions/start and /sessions/stop; the grace timer would
// have made it three copies, each free to drift.
//
// The auto-stop path exists because nothing else closes a session when the
// broadcast ends — the only teardown trigger was a manual HTTP call, so a
// forgotten stop left python's active_session pinned at 1 (and the alerting
// with it) until the container restarted.

export type PipelineHandle = {
  stop: () => Promise<void>;
  notifyPublisherReturned: () => void;
  // Wall-clock ms of the last pcm chunk the transcoder produced, or null if
  // none ever arrived. This is the second proof that a publisher is alive:
  // on_publish fires once at publish start, so a broadcast that was already
  // running when this process booted never announces itself.
  lastAudioAt: () => number | null;
};

export type SessionLanguages = {
  sourceLang: string;
  targetLang: string;
};

export type SessionStartResult = {
  sessionId: string;
  webSocketUrl: string;
  // True when the caller joined a broadcast that was already running instead of
  // starting one (P1 D1). The Flutter app ignores unknown response fields
  // (mvp/lib/type.dart), so this ships without an app release.
  joined: boolean;
  // The live session's actual languages, which are NOT necessarily the ones the
  // caller asked for — a joiner gets what the broadcast is already producing.
  sourceLang: string;
  targetLang: string;
};

// P1 D4: no outcome tears a session down any more. "ignored" means the request
// named the live session and was deliberately not acted on.
export type StopOutcome = "ignored" | "mismatch";

export type SessionLifecycleDeps = {
  newSessionId: () => string;
  // sessionId rides along so the pipeline can pass it down as explicit
  // AudioConsumerContext — the orchestrator has no other way to learn it now
  // that the shared session slot is gone (mic-rtmp-session-slot-plan D1-A).
  startPipeline: (args: {
    sessionId: string;
    sourceLanguage: string;
    targetLanguage: string;
  }) => Promise<PipelineHandle>;
  startPythonSession: (
    params: SessionLanguages & { sessionId: string }
  ) => Promise<{ webSocketUrl: string }>;
  stopPythonSession: (sessionId: string) => Promise<void>;
  recordStop: (reason: SessionStopReason) => void;
  graceMs: () => number;
  // Separate knob from graceMs: that one covers a publisher that dropped
  // mid-broadcast (network flap), this one covers "the app was opened before
  // OBS". Different orders of magnitude, and this one is safe to make generous
  // because arriving audio re-arms it.
  noPublisherGraceMs: () => number;
};

export type SessionLifecycle = ReturnType<typeof createSessionLifecycle>;

export function createSessionLifecycle(deps: SessionLifecycleDeps) {
  let currentSessionId: string | null = null;
  let pipeline: PipelineHandle | null = null;
  let publisherClientId: string | null = null;
  let graceTimer: NodeJS.Timeout | null = null;
  // What a joiner is handed (P1 D1). Written only once the session is fully up,
  // and cleared everywhere currentSessionId is cleared — a url that outlives its
  // session would hand the next listener a socket to a dead broadcast.
  let currentWebSocketUrl: string | null = null;
  let currentLanguages: SessionLanguages | null = null;
  // Two devices pressing [시작] within the same second is the ordinary P1 case,
  // not a rare race: without this the second call finds currentSessionId set but
  // no url yet, falls through, and starts a second pipeline that orphans the
  // first. Joiners wait on the in-flight start instead.
  let startInFlight: Promise<SessionStartResult> | null = null;

  const forgetSession = (): void => {
    currentSessionId = null;
    pipeline = null;
    publisherClientId = null;
    currentWebSocketUrl = null;
    currentLanguages = null;
  };

  const cancelAutoStop = (): void => {
    if (!graceTimer) return;
    clearTimeout(graceTimer);
    graceTimer = null;
  };

  const teardown = async (reason: SessionStopReason): Promise<void> => {
    const sessionId = currentSessionId;
    if (!sessionId) return;

    const handle = pipeline;
    // Release the identity BEFORE the first await. A second teardown landing
    // while this one is in flight must find nothing left to close, otherwise
    // both run the sequence and python gets two stop calls. The join cache goes
    // with it — a joiner must never receive a url for a session being torn down.
    forgetSession();
    cancelAutoStop();
    deps.recordStop(reason);

    try {
      await handle?.stop();
    } catch (err) {
      // A wedged ffmpeg must not block the python stop below — that call is
      // the one that clears active_session and silences the alerting.
      console.error(`session ${sessionId}: pipeline stop failed`, err);
    }
    await deps.stopPythonSession(sessionId);
  };

  // "publisher_done": the publisher we knew about left. "no_publisher": the
  // session was opened while the slot was empty and we are waiting to find out
  // whether a broadcast shows up at all (2026-08-24 incident).
  const scheduleAutoStop = (kind: "publisher_done" | "no_publisher"): void => {
    if (!currentSessionId) return;
    cancelAutoStop();
    const armedFor = currentSessionId;
    const graceMs =
      kind === "no_publisher" ? deps.noPublisherGraceMs() : deps.graceMs();
    const armedAt = Date.now();
    const timer = setTimeout(() => {
      graceTimer = null;
      // The session that armed this timer may be gone already — stopped by
      // hand, or superseded by a new session started inside the grace window.
      if (currentSessionId !== armedFor) return;

      // Audio is the other liveness proof, and the only one available when
      // on_publish already fired before this process existed. Re-arm rather
      // than cancel outright: when that broadcast does end there is no
      // publish_done either, so a cancelled timer would strand the session
      // exactly like the incident it is meant to fix.
      // Compared against the arming instant rather than a duration: "did any
      // audio arrive during THIS window" is the question, and the timestamp
      // form has no boundary case to argue about.
      //
      // Applied to BOTH kinds. publisher_done needs it for the same reason:
      // the "first writer wins" guard only holds while the slot is full, so
      // after a node restart a rejected duplicate OBS can arm this timer for a
      // broadcast that never stopped. The cost on the normal path is bounded —
      // ffmpeg may flush a little buffered pcm right after the publisher
      // leaves, which costs one extra window, and the next one finds silence.
      const lastAudio = pipeline?.lastAudioAt() ?? null;
      if (lastAudio !== null && lastAudio >= armedAt) {
        scheduleAutoStop(kind);
        return;
      }

      console.log(
        kind === "no_publisher"
          ? `session ${armedFor}: no publisher ever connected — auto-stopping`
          : `session ${armedFor}: publisher gone — auto-stopping`
      );
      void teardown(kind).catch((err) =>
        console.error(`session ${armedFor}: auto-stop failed`, err)
      );
    }, graceMs);
    // Never hold the event loop open for a timer whose whole job is cleanup.
    timer.unref?.();
    graceTimer = timer;
  };

  const beginSession = async (
    languages: SessionLanguages
  ): Promise<SessionStartResult> => {
    const sessionId = deps.newSessionId();
    currentSessionId = sessionId;
    // publisherClientId is deliberately NOT cleared here. The normal order of
    // events is OBS first, app [시작] second, so clearing would forget a
    // publisher that is live right now — and hand the free slot to whatever
    // publishes next, whose publish_done then ends the running broadcast.
    // The slot is emptied by publisherDone, which is the only event that
    // actually says a publisher left; teardown clears it via forgetSession.

    let pythonStarted = false;
    try {
      const { webSocketUrl } = await deps.startPythonSession({
        sessionId,
        ...languages,
      });
      pythonStarted = true;
      const handle = await deps.startPipeline({
        sessionId,
        sourceLanguage: languages.sourceLang,
        targetLanguage: languages.targetLang,
      });
      // A teardown can land anywhere in the two awaits above — on_publish_done
      // arrives before the start finishes and the grace timer fires. teardown
      // has no way to cancel this call (forgetSession cannot unmake a promise),
      // so the check belongs here, against the id this call owns. Assigning
      // `pipeline` past that point would leave a live ffmpeg behind a null
      // currentSessionId: nothing can reach it to stop it, and P1 D4 removed
      // the manual stop that used to be the way out.
      if (currentSessionId !== sessionId) {
        await handle.stop();
        throw new Error(`session ${sessionId} was torn down while starting`);
      }
      pipeline = handle;
      // Nothing else will ever close this session if OBS does not show up:
      // auto-stop arms on publisher_done, and [정지] is a P1 D4 no-op. Armed
      // only when the slot is empty, so the normal order (OBS first) is
      // untouched. Placed after `pipeline` is assigned — the timer reads
      // lastAudioAt off it. (2026-08-24 incident)
      if (!publisherClientId) scheduleAutoStop("no_publisher");
      // Publish the join cache only now — everything a joiner needs exists.
      currentWebSocketUrl = webSocketUrl;
      currentLanguages = languages;
      return { sessionId, webSocketUrl, joined: false, ...languages };
    } catch (err) {
      // stillOwned distinguishes "the pipeline failed" from "a teardown claimed
      // this session mid-start": teardown already ran stopPythonSession, and a
      // second stop for the same id is a non-2xx error log.
      const stillOwned = currentSessionId === sessionId;
      forgetSession();
      if (pythonStarted && stillOwned) {
        // Without this, a pipeline failure after a successful python start
        // leaves an orphan python session nothing can reach — active_session
        // sticks at 1 and the alerting fires until a manual /internal stop.
        await deps.stopPythonSession(sessionId).catch((stopErr) =>
          console.error(
            `session ${sessionId}: python stop after failed start failed`,
            stopErr
          )
        );
      }
      throw err;
    }
  };

  return {
    async start(languages: SessionLanguages): Promise<SessionStartResult> {
      // P1 D1: 멱등 join. 라이브 세션이 있으면 teardown 없이 그 세션을 돌려준다.
      // 청취자 앱의 [시작] 이 유일한 진입점이라(mvp/lib/rtmp_translation_tab.dart)
      // 예전에는 두 번째 청취자가 teardown("superseded") 로 ffmpeg·STT 까지
      // 재시작시켰다 — 방송 자체가 끊겼다는 뜻이다.
      if (currentSessionId && currentWebSocketUrl && currentLanguages) {
        console.log(
          `session ${currentSessionId}: join (requested ${languages.sourceLang}->${languages.targetLang}, live ${currentLanguages.sourceLang}->${currentLanguages.targetLang})`
        );
        return {
          sessionId: currentSessionId,
          webSocketUrl: currentWebSocketUrl,
          joined: true,
          ...currentLanguages,
        };
      }

      if (startInFlight) {
        const result = await startInFlight;
        return { ...result, joined: true };
      }

      startInFlight = beginSession(languages);
      try {
        return await startInFlight;
      } finally {
        startInFlight = null;
      }
    },

    // P1 D4: 세션을 닫지 않는다. join 한 청취자가 [정지]
    // (mvp/lib/rtmp_translation_tab.dart:286)를 누르면 남의 방송이 죽기
    // 때문이다. 종료는 on_publish_done + grace 단일 경로가 맡는다. 수동 회수가
    // 필요하면 python 의 POST /internal/sessions/stop 을 직접 친다.
    async stopBySessionId(sessionId: string): Promise<StopOutcome> {
      if (!currentSessionId || sessionId !== currentSessionId) return "mismatch";
      console.log(
        `session ${sessionId}: stop ignored (P1 D4 — teardown is on_publish_done only)`
      );
      return "ignored";
    },

    // on_publish, but only once the stream-name and key checks passed: an
    // unauthenticated publish attempt must not be able to extend a session it
    // cannot start.
    publisherReturned(clientId: string | null): void {
      // First writer wins. A second OBS on the same stream name is rejected by
      // nginx ("Already publishing"), but the hook fires BEFORE the rejection
      // and that clientid's on_publish_done follows ~1ms later (measured on the
      // dev stack, 2026-08-09). Overwriting the slot would let that pair arm the
      // grace timer and auto-stop a broadcast that never stopped — reproduced,
      // active_session went 1 → 0 with the real publisher still sending.
      if (!publisherClientId) publisherClientId = clientId;
      cancelAutoStop();
      pipeline?.notifyPublisherReturned();
    },

    publisherDone(clientId: string | null): void {
      // Someone else's exit says nothing about this broadcast: it is either the
      // rejected duplicate above, or the late publish_done of a connection that
      // already died and was replaced. Neither may arm the timer.
      if (publisherClientId && clientId && clientId !== publisherClientId) {
        return;
      }
      // Empty the slot even with no session running. A broadcast that starts
      // and stops before the app is opened (a pre-service OBS test) would
      // otherwise leave its id behind, and the real broadcast's publish_done
      // would then be discarded as a mismatch — a session nothing can close,
      // which is the failure P1 D4 left without a manual way out.
      publisherClientId = null;
      scheduleAutoStop("publisher_done");
    },

    currentSession(): string | null {
      return currentSessionId;
    },
  };
}
