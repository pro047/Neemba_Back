import { TokenBucket } from "../stream/TokenBucket";

type RateGateOptions = {
  maximumEmitsPerSecond?: number;
  minimumGrowthCharacters?: number;
  punctuationRegex?: RegExp;
};

export function createSentenceSession(
  onSentence: (
    text: string,
    isFinal: boolean,
    endTimeMilliseconds: number,
    confidence: number,
    segmentId: number,
    sessionId: string
  ) => void,
  {
    maximumEmitsPerSecond = 3,
    minimumGrowthCharacters = 50,
    punctuationRegex = /[.!?](\s+|["')\]]*)$/,
  }: RateGateOptions = {}
) {
  const bucket = new TokenBucket(maximumEmitsPerSecond, maximumEmitsPerSecond);
  const lastTextByKey = new Map<string, string>();

  return {
    handleInterim(p: {
      segmentId: number | undefined;
      sessionId: string;
      transcriptText: string;
      resultEndTimeMs?: number;
      isFinal: boolean;
      confidence?: number;
    }) {
      const segmentId = p.segmentId ?? 0;
      const sessionId = p.sessionId;
      const endTimeMilliseconds = p.resultEndTimeMs ?? 0;
      const confidence = p.confidence ?? 0;
      const isFinal = p.isFinal;
      const text = (p.transcriptText ?? "").trimEnd();
      if (!text) return;

      const key = `${sessionId} : ${segmentId} : ${endTimeMilliseconds}`;
      if (lastTextByKey.get(key) === text) return;
      lastTextByKey.set(key, text);

      const hasPunctuation = punctuationRegex.test(text);
      const growthEnough = text.length >= minimumGrowthCharacters;

      if ((hasPunctuation || growthEnough) && bucket.allow()) {
        onSentence(
          text,
          isFinal,
          endTimeMilliseconds,
          confidence,
          segmentId,
          sessionId
        );
      }
    },
    stop(sessionId?: string) {
      if (sessionId == null) lastTextByKey.clear();
      else {
        for (const k of [...lastTextByKey.keys()])
          if (k.startsWith(`${sessionId}`)) lastTextByKey.delete(k);
      }
    },
  };
}
