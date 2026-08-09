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
  startPipeline: (languages: {
    sourceLanguage: string;
    targetLanguage: string;
  }) => Promise<PipelineHandle>;
  startPythonSession: (
    params: SessionLanguages & { sessionId: string }
  ) => Promise<{ webSocketUrl: string }>;
  stopPythonSession: (sessionId: string) => Promise<void>;
  onSessionIdChanged: (sessionId: string | null) => void;
  recordStop: (reason: SessionStopReason) => void;
  graceMs: () => number;
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
    deps.onSessionIdChanged(null);
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

  const scheduleAutoStop = (clientId: string | null): void => {
    if (!currentSessionId) return;
    // A reconnecting OBS opens its new socket (and fires on_publish) before
    // nginx notices the old one died, so the stale publish_done can arrive
    // AFTER the publisher is already back. Re-arming on it would auto-stop a
    // live broadcast one grace window later.
    if (publisherClientId && clientId && clientId !== publisherClientId) return;

    cancelAutoStop();
    const armedFor = currentSessionId;
    const timer = setTimeout(() => {
      graceTimer = null;
      // The session that armed this timer may be gone already — stopped by
      // hand, or superseded by a new session started inside the grace window.
      if (currentSessionId !== armedFor) return;
      console.log(`session ${armedFor}: publisher gone — auto-stopping`);
      void teardown("publisher_done").catch((err) =>
        console.error(`session ${armedFor}: auto-stop failed`, err)
      );
    }, deps.graceMs());
    // Never hold the event loop open for a timer whose whole job is cleanup.
    timer.unref?.();
    graceTimer = timer;
  };

  const beginSession = async (
    languages: SessionLanguages
  ): Promise<SessionStartResult> => {
    const sessionId = deps.newSessionId();
    currentSessionId = sessionId;
    // Forget the previous broadcast's publisher. A stale id here would make
    // the ordering guard below reject the new session's publish_done, and
    // the failure mode of that is the exact bug this feature fixes — a
    // session nobody ever closes. When in doubt the guard must let the
    // teardown through, so an unknown publisher is null, never a leftover.
    publisherClientId = null;
    deps.onSessionIdChanged(sessionId);

    try {
      const { webSocketUrl } = await deps.startPythonSession({
        sessionId,
        ...languages,
      });
      const handle = await deps.startPipeline({
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
      // Publish the join cache only now — everything a joiner needs exists.
      currentWebSocketUrl = webSocketUrl;
      currentLanguages = languages;
      return { sessionId, webSocketUrl, joined: false, ...languages };
    } catch (err) {
      // Same clear as teardown, minus the stop calls: nothing was started, so
      // leaving a url behind would publish a session that does not exist.
      forgetSession();
      deps.onSessionIdChanged(null);
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

    // on_publish, but only once the key check passed: an unauthenticated
    // publish attempt must not be able to extend a session it cannot start.
    publisherReturned(clientId: string | null): void {
      publisherClientId = clientId;
      cancelAutoStop();
      pipeline?.notifyPublisherReturned();
    },

    publisherDone(clientId: string | null): void {
      scheduleAutoStop(clientId);
    },

    currentSession(): string | null {
      return currentSessionId;
    },
  };
}
