import type { Readable } from "node:stream";

export type StopStreaming = () => Promise<void>;

export type AudioConsumerContext = {
  sessionId?: string;
};

export interface AudioConsumerPort {
  start(
    readable: Readable,
    context?: AudioConsumerContext
  ): Promise<StopStreaming>;
}
