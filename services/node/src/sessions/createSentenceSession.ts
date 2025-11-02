import { TokenBucket } from "../stream/TokenBucket";

type RateGateOptions = {
  maximumEmitsPerSecond?: number;
  minimumGrowthCharacters?: number;
  punctuationRegex?: RegExp;
};

// 한국어 종결어미 패턴
const KOREAN_ENDING_PATTERNS = [
  /[다요죠네]\s*$/,      // 단순 종결어미
  /어요\s*$/,
  /아요\s*$/,
  /는데요\s*$/,
  /은데요\s*$/,
  /습니다\s*$/,
  /습니까\s*$/,
  /지요\s*$/,
  /게요\s*$/,
  /을게요\s*$/,
  /을까요\s*$/,
  /으니까요\s*$/,
  /네요\s*$/,
  /인데요\s*$/,
  /래요\s*$/,
  /거예요\s*$/,
  /니다\s*$/,
];

function hasKoreanSentenceEnding(text: string): boolean {
  for (const pattern of KOREAN_ENDING_PATTERNS) {
    if (pattern.test(text)) return true;
  }
  return false;
}

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
    punctuationRegex = /[.!?…](\s+|["')\]]*)$/,
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
      const hasKoreanEnding = hasKoreanSentenceEnding(text);
      const growthEnough = text.length >= minimumGrowthCharacters;

      if ((hasPunctuation || hasKoreanEnding || growthEnough) && bucket.allow()) {
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
