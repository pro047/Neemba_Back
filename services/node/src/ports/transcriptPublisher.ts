export type PublishEvent = {
  sessionId: string;
  segmentId: number;
  sequence: number;
  confidence?: number;
  transcriptText: string;
  sourceLanguage: string;
  targetLanguage: string;
  sampleRateHz: number;
  createdAt: string;
};

export interface TranscriptPublisherPort {
  start?(): Promise<void>;
  stop?(): Promise<void>;
  publish(message: PublishEvent): Promise<void>;
}
