import type { IStreamSwitcher, StreamHandle } from "../ports/streamSwitcher.js";

export class StreamSwitcher implements IStreamSwitcher {
  private active?:
    | {
        handle: StreamHandle;
        generation: number;
        segmentId: number;
      }
    | undefined;
  private generation = 0;
  private buffer = Buffer.alloc(0);

  constructor(
    private readonly onSegmentRotation: (segmentId: number) => void
  ) {}

  async handoff(next: StreamHandle, nextSegmentId: number) {
    const nextGen = ++this.generation;
    const prev = this.active;

    this.active = {
      handle: next,
      generation: nextGen,
      segmentId: nextSegmentId,
    };
    this.onSegmentRotation(nextSegmentId);

    if (prev) {
      queueMicrotask(async () => {
        try {
          await prev.handle.close();
        } catch {}
      });
    }
  }

  async write(payload: Buffer) {
    const snapshot = this.active;
    if (!snapshot) return;

    const { handle, generation } = snapshot;

    if (generation !== this.generation) return;
    if (!handle.isOpen()) return;
    if (!Buffer.isBuffer(payload) || payload.length === 0) return;

    this.buffer = Buffer.concat([this.buffer, payload]);
    const frameBytes = 3200;
    while (this.buffer.length >= frameBytes) {
      const frame = this.buffer.subarray(0, frameBytes);
      this.buffer = this.buffer.subarray(frameBytes);
      if (generation !== this.generation || !handle.isOpen()) {
        this.buffer = Buffer.concat([frame, this.buffer]);
        return;
      }
      try {
        await handle.write(frame);
      } catch (err) {
        console.error("stream switcher write failed", err);
        this.buffer = Buffer.concat([frame, this.buffer]);
        return;
      }
    }
  }

  currentSegmentId(): number | undefined {
    if (!this.active) return undefined;
    return this.active.segmentId;
  }

  async shutdown() {
    const snapshot = this.active;
    this.active = undefined;
    if (snapshot) await snapshot.handle.close();
  }
}
