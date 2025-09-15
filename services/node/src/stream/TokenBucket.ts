export class TokenBucket {
  private tokens: number;
  private lastRefillAt = Date.now();
  constructor(
    private capacity: number,
    private refillPerSecond: number
  ) {
    this.tokens = capacity;
  }

  allow(): boolean {
    const now = Date.now();
    const elapsedSeconds = (now - this.lastRefillAt) / 1000;
    this.lastRefillAt = now;
    this.tokens = Math.min(
      this.capacity,
      this.tokens + elapsedSeconds * this.refillPerSecond
    );
    if (this.tokens < 1) return false;
    this.tokens -= 1;
    return true;
  }
}
