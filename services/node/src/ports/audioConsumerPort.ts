import type { Readable } from "node:stream";

export type StopStreaming = () => Promise<void>;

export interface AudioConsumerPort {
  start(readable: Readable): Promise<StopStreaming>;
}
