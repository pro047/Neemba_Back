export interface IInterfaceOrchestra {
  onSttResult(result: {
    transcript: string;
    isFinal: boolean;
    resultEndTimeMs: number;
    confidence?: number;
    segmentId: number;
    sessionId: string;
  }): Promise<void>;

  dispose(): Promise<void>;
}
