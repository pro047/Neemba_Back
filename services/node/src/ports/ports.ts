import type { Readable, Writable } from "node:stream";

//쿠키 헤더 제공 인터페이스
export interface CookieHeaderProvider {
  getCookieHeader(): Promise<string>;
}

export interface LiveMediaExtracter {
  getStreamUrl(youtubeUrl: string): Promise<string>;
}

//서비스 포트 인터페이스
export interface LiveMediaReader {
  start(): { mediaReadable: Readable };
  stop(): void;
}

// 오디오 트랜스코더 인터페이스(to google speech-to-text)
export interface AudioTranscoder {
  startTranscoder(): {
    inputWritable: Writable;
    pcmReadable: Readable;
    stop: () => void;
  };
}
