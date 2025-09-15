export type StreamHandle = {
  write(data: Buffer): Promise<void>;
  close(): Promise<void>;
  isOpen(): boolean;
};

export interface IStreamSwitcher {
  handoff(next: StreamHandle, nextSegmentId: number): Promise<void>;
  write(payload: Buffer): Promise<void>;
  shutdown(): Promise<void>;
}
