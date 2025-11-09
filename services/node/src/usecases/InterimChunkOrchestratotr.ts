import type { IInterfaceOrchestra } from "../ports/interimOrchestra";
import type {
  PublishEvent,
  TranscriptPublisherPort,
} from "../ports/transcriptPublisher";

export class InterimChunkOrchestrator implements IInterfaceOrchestra {
  private text: string | null = "";
  private prevText: string | null = "";
  private sequence = 0;
  private silenceTimer?: NodeJS.Timeout;
  private trailingTimer: NodeJS.Timeout | null = null;
  private publisherStarted = false;
  private throttleMs: number = 50;
  private debounceMs: number = 200;
  private hardLatencMs: number = 300;
  private maxQueue: number = 300;
  private lastSentAt: number = 0;
  private lastInputAt: number = 0;
  private queue: string[] = [];

  constructor(private readonly publisher: TranscriptPublisherPort) {}

  async onSttResult(result: {
    transcript: string;
    isFinal: boolean;
    resultEndTimeMs: number;
    segmentId: number;
    sessionId: string;
  }) {
    this.prevText = this.text;
    this.text = this.normalize(result.transcript);
    if (!this.prevText || !this.text) return;

    const segmentId = result.segmentId;
    const sessionId = result.sessionId;

    const slice = this.computeDelta(this.prevText, this.text);

    if (slice === "") return;

    console.log("interim - slice :", slice);
    console.log("--------------------------");

    this.queue.push(slice);
    this.lastInputAt = Date.now();

    if (this.lastInputAt - this.lastSentAt >= this.throttleMs) {
      await this.publishSpan(segmentId, sessionId);
    }

    if (this.trailingTimer) clearTimeout(this.trailingTimer);
    this.trailingTimer = setTimeout(async () => {
      if (Date.now() - this.lastSentAt >= this.hardLatencMs) {
        await this.publishSpan(segmentId, sessionId);
      }
    }, this.debounceMs);
  }

  private normalize(text: string) {
    return text
      .trim()
      .replace(/[\s]+/g, " ")
      .replace(/[.,!?]/g, "");
  }

  private computeDelta(prevText: string | null, currText: string | null) {
    if (!currText) return "";
    if (!prevText) return currText;
    if (prevText === currText) return "";

    let prefixIdx = 0;
    const minLength = Math.min(prevText.length, currText.length);
    while (
      prefixIdx < minLength &&
      prevText.charCodeAt(prefixIdx) === currText.charCodeAt(prefixIdx)
    ) {
      prefixIdx++;
    }

    let prevSuffix = prevText.length - 1;
    let currSuffix = currText.length - 1;
    while (
      prevSuffix >= prefixIdx &&
      currSuffix >= prefixIdx &&
      prevText.charCodeAt(prevSuffix) === currText.charCodeAt(currSuffix)
    ) {
      prevSuffix--;
      currSuffix--;
    }

    return currText.slice(prefixIdx, currSuffix + 1);
  }

  private async ensurePublisher() {
    if (!this.publisherStarted) {
      await this.publisher.start?.();
      this.publisherStarted = true;
    }
  }

  private async publishSpan(segmentId: number, sessionId: string) {
    await this.ensurePublisher();

    if (Date.now() - this.lastSentAt < 5) return;

    const merged = this.queue.join("");
    this.queue = [];
    this.lastSentAt = Date.now();

    const chunk: PublishEvent = {
      sessionId: sessionId,
      segmentId: segmentId,
      sequence: ++this.sequence,
      sourceLanguage: "ko-KR",
      targetLanguage: "en-US",
      sampleRateHz: 16000,
      transcriptText: merged,
      createdAt: new Date().toISOString(),
    };

    await this.publisher.publish(chunk);
  }

  async dispose() {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    await this.publisher.stop?.().catch(() => {});
    console.log("interim orchestra : dispose");
  }
}
