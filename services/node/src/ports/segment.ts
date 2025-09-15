export interface ISegmentManager {
  next(sessionId: string): number;
  current(sessionId: string): number;
}
