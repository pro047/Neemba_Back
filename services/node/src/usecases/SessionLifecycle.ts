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
};

export type StopOutcome = "stopped" | "mismatch";

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

  const cancelAutoStop = (): void => {
    if (!graceTimer) return;
    clearTimeout(graceTimer);
    graceTimer = null;
  };

  const teardown = async (reason: SessionStopReason): Promise<void> => {
    const sessionId = currentSessionId;
    if (!sessionId) return;

    const handle = pipeline;
    // Release the identity BEFORE the first await. A manual stop landing while
    // the grace timer is already tearing down must find nothing left to close,
    // otherwise both run the sequence and python gets two stop calls.
    currentSessionId = null;
    pipeline = null;
    publisherClientId = null;
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

  return {
    async start(languages: SessionLanguages): Promise<SessionStartResult> {
      if (currentSessionId) {
        try {
          await teardown("superseded");
        } catch (err) {
          // Starting the new session matters more than reporting that the old
          // one resisted; python's own startup sweep closes the leftover row.
          console.error("previous session teardown failed", err);
        }
      }

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
        pipeline = await deps.startPipeline({
          sourceLanguage: languages.sourceLang,
          targetLanguage: languages.targetLang,
        });
        return { sessionId, webSocketUrl };
      } catch (err) {
        currentSessionId = null;
        pipeline = null;
        publisherClientId = null;
        deps.onSessionIdChanged(null);
        throw err;
      }
    },

    async stopBySessionId(sessionId: string): Promise<StopOutcome> {
      if (!currentSessionId || sessionId !== currentSessionId) return "mismatch";
      await teardown("manual");
      return "stopped";
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
