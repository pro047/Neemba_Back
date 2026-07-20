import type {
  PublishEvent,
  TranscriptPublisherPort,
} from "./ports/transcriptPublisher.js";

// §4-4 backpressure: bounded in-memory retry buffer in front of the real
// (JetStream) publisher, so a NATS blip no longer silently drops spans.
//
// All publishes flow through one FIFO drained by a single loop. Ordering is
// load-bearing, not cosmetic: the Python consumer keeps a per-session
// sequence watermark and drops seq<=last as duplicates, so any span that is
// overtaken by a newer one is lost permanently. A blocked head therefore
// blocks everything behind it until it is published or expires.
//
// TTL must stay under the JetStream dedup window (2 min): republishing the
// same Nats-Msg-Id inside that window is deduped by the broker, which is
// what makes retrying after a partial failure safe. Raising TTL past the
// window would break that assumption.
const DEFAULT_TTL_MS = 60_000;
const DEFAULT_RETRY_DELAY_MS = 2_000;
// ~5 spans/sec worst case * 60s TTL ≈ 300; cap bounds memory if the drain
// loop is stuck longer than expected.
const DEFAULT_MAX_BUFFERED = 500;

type BufferedSpan = {
  event: PublishEvent;
  enqueuedAt: number;
};

export class RetryingTranscriptPublisher implements TranscriptPublisherPort {
  private readonly queue: BufferedSpan[] = [];
  private draining = false;
  private stopped = false;
  private dropped = 0;

  constructor(
    private readonly inner: TranscriptPublisherPort,
    private readonly ttlMs = DEFAULT_TTL_MS,
    private readonly retryDelayMs = DEFAULT_RETRY_DELAY_MS,
    private readonly maxBuffered = DEFAULT_MAX_BUFFERED
  ) {}

  get droppedCount(): number {
    return this.dropped;
  }

  async start(): Promise<void> {
    await this.inner.start?.();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.queue.length > 0) {
      this.dropped += this.queue.length;
      console.warn(
        `publish buffer: dropped ${this.queue.length} spans on stop, total dropped=${this.dropped}`
      );
      this.queue.length = 0;
    }
    await this.inner.stop?.();
  }

  // Resolves once the span is queued, not once it reaches the broker —
  // delivery outcome is the drain loop's business (callers were already
  // fire-and-forget).
  async publish(message: PublishEvent): Promise<void> {
    if (this.stopped) {
      this.dropped += 1;
      console.warn(
        `publish buffer: span dropped after stop, total dropped=${this.dropped}`
      );
      return;
    }

    this.queue.push({ event: message, enqueuedAt: Date.now() });
    if (this.queue.length > this.maxBuffered) {
      this.queue.shift();
      this.dropped += 1;
      console.warn(
        `publish buffer: capacity exceeded, oldest span dropped, total dropped=${this.dropped}`
      );
    }

    if (!this.draining) {
      this.draining = true;
      void this.drainLoop().finally(() => {
        this.draining = false;
      });
    }
  }

  private async drainLoop(): Promise<void> {
    while (!this.stopped && this.queue.length > 0) {
      this.evictExpired();
      const head = this.queue[0];
      if (!head) return;

      try {
        await this.inner.publish(head.event);
        // shift() only after success and only if stop() has not already
        // cleared the queue underneath us.
        if (this.queue[0] === head) this.queue.shift();
      } catch (err) {
        console.warn(
          `publish buffer: publish failed, retrying in ${this.retryDelayMs}ms ` +
            `(buffered=${this.queue.length})`,
          err
        );
        await this.sleep(this.retryDelayMs);
      }
    }
  }

  private evictExpired(): void {
    const now = Date.now();
    while (
      this.queue.length > 0 &&
      now - this.queue[0]!.enqueuedAt > this.ttlMs
    ) {
      const expired = this.queue.shift()!;
      this.dropped += 1;
      console.warn(
        `publish buffer: span expired after ${this.ttlMs}ms, dropped ` +
          `(session=${expired.event.sessionId} seq=${expired.event.sequence}), ` +
          `total dropped=${this.dropped}`
      );
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
