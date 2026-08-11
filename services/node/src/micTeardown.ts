import type { SessionRuntimeStore } from "./sessionRuntimeStore.js";

// Shared pending-teardown state for mic sessions, one map per process. Two
// producers arm timers here — /mic/start (waiting for the first audio socket)
// and the socket close handler (waiting for a reconnect) — and a (re)connect
// must be able to cancel whichever one is pending. Split maps would let a
// connect cancel the wrong side's timer search and tear down a live session.
export const micPendingTeardowns = new Map<string, NodeJS.Timeout>();

type ScheduleMicTeardownDeps = {
  runtimeStore: SessionRuntimeStore;
  stop: (sessionId: string) => Promise<void>;
  graceMs: number;
  reason: string;
  pendingTeardowns?: Map<string, NodeJS.Timeout>;
};

export function scheduleMicTeardown(
  sessionId: string,
  {
    runtimeStore,
    stop,
    graceMs,
    reason,
    pendingTeardowns = micPendingTeardowns,
  }: ScheduleMicTeardownDeps
): void {
  if (pendingTeardowns.has(sessionId)) return;

  const timer = setTimeout(() => {
    pendingTeardowns.delete(sessionId);
    // /mic/stop may have landed during the grace window.
    if (!runtimeStore.get(sessionId)) return;
    console.warn(`${reason}: ${sessionId}`);
    stop(sessionId).catch((err) =>
      console.error("mic session teardown failed", err)
    );
  }, graceMs);
  timer.unref?.();
  pendingTeardowns.set(sessionId, timer);
}

export function cancelMicTeardown(
  sessionId: string,
  pendingTeardowns: Map<string, NodeJS.Timeout> = micPendingTeardowns
): boolean {
  const pending = pendingTeardowns.get(sessionId);
  if (!pending) return false;
  clearTimeout(pending);
  pendingTeardowns.delete(sessionId);
  return true;
}
