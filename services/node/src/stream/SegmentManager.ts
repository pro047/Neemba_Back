import type { ISegmentManager } from "../ports/segment";

export class SegmentManager implements ISegmentManager {
  private perSession = new Map<string, number>();

  next(sessionId: string) {
    const next = (this.perSession.get(sessionId) ?? -1) + 1;
    this.perSession.set(sessionId, next);
    return next;
  }

  current(sessionId: string) {
    return this.perSession.get(sessionId) ?? 0;
  }
}
