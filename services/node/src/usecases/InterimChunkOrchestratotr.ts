import type { IInterfaceOrchestra } from "../ports/interimOrchestra.js";
import type {
  PublishEvent,
  TranscriptPublisherPort,
} from "../ports/transcriptPublisher.js";

export class InterimChunkOrchestrator implements IInterfaceOrchestra {
  private text: string | null = "";
  private prevText: string | null = "";
  private currentSegmentId: number | null = null;
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

  constructor(
    private readonly publisher: TranscriptPublisherPort,
    private readonly languages: {
      sourceLanguage: string;
      targetLanguage: string;
    } = {
      sourceLanguage: "ko-KR",
      targetLanguage: "en-US",
    }
  ) {}

  async onSttResult(result: {
    transcript: string;
    isFinal: boolean;
    resultEndTimeMs: number;
    segmentId: number;
    sessionId: string;
  }) {
    // Empty transcripts must not touch delta state: overwriting `text` with ""
    // would make the next result look like a session start and re-publish it
    // in full (duplication). Bail before mutating.
    const next = this.normalize(result.transcript);
    if (!next) return;

    const segmentId = result.segmentId;
    const sessionId = result.sessionId;

    // Segment boundary (stream rotation): texts from different streams must
    // never be diffed against each other. Ship whatever is still queued
    // under the old segment, then restart delta tracking from scratch.
    if (this.currentSegmentId !== null && segmentId !== this.currentSegmentId) {
      if (this.queue.length > 0) {
        await this.publishSpan(this.currentSegmentId, sessionId, true);
      }
      this.prevText = "";
      this.text = "";
    }
    this.currentSegmentId = segmentId;

    this.prevText = this.text;
    this.text = next;

    const slice = this.computeDelta(this.prevText, this.text);

    if (slice === "") return;

    this.queue.push(slice);
    this.lastInputAt = Date.now();

    if (result.isFinal) {
      // A final closes the utterance: Google restarts the next interim from
      // "", so ship the tail now and reset the diff baseline.
      if (this.trailingTimer) clearTimeout(this.trailingTimer);
      await this.publishSpan(segmentId, sessionId, true);
      this.prevText = "";
      this.text = "";
      return;
    }

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

  private async publishSpan(segmentId: number, sessionId: string, force = false) {
    // publishSpan is reached from fire-and-forget callers (STT callback,
    // trailing timer): any rejection escaping here becomes an unhandled
    // rejection and kills the whole Node process — every live session.
    try {
      await this.ensurePublisher();
    } catch (err) {
      // Connection failed: keep the queue intact so the next span retries.
      console.error("interim publish: publisher start failed", err);
      return;
    }

    // Boundary/final flushes must never be dropped by the rapid-publish
    // guard: a skipped flush would leave old-segment slices in the queue and
    // merge them into the next segment's publish.
    if (!force && Date.now() - this.lastSentAt < 5) return;

    const merged = this.queue.join("");
    this.queue = [];
    this.lastSentAt = Date.now();

    const chunk: PublishEvent = {
      sessionId: sessionId,
      segmentId: segmentId,
      sequence: ++this.sequence,
      sourceLanguage: this.languages.sourceLanguage,
      targetLanguage: this.languages.targetLanguage,
      sampleRateHz: 16000,
      transcriptText: merged,
      createdAt: new Date().toISOString(),
    };

    try {
      await this.publisher.publish(chunk);
    } catch (err) {
      console.error("interim publish failed, span dropped", err);
    }
  }

  async dispose() {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    await this.publisher.stop?.().catch(() => {});
    console.log("interim orchestra : dispose");
  }
}
