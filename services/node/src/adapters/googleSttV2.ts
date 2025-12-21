import { v2 as speech } from "@google-cloud/speech";
import type { SpeechToTextPort } from "../ports/sttPorts";

type TransciprtPayload = {
  isFinal: boolean;
  transcriptText: string;
  confidence?: number;
  resultEndTimeMs?: number;
};

const toMillis = (t?: { seconds?: number | string; nanos?: number }) => {
  if (!t) return 0;
  const s =
    typeof t.seconds === "string" ? parseInt(t.seconds, 10) : (t.seconds ?? 0);
  const n = t.nanos ?? 0;
  return Math.floor(s * 1000 + n / 1_000_000);
};

export class GoogleSttV2Adapter implements SpeechToTextPort {
  constructor(
    private readonly speechClient: speech.SpeechClient,
    private readonly recognizerName: string
  ) {}

  getRecognizer = async () => {
    await this.speechClient.getRecognizer({
      name: this.recognizerName,
    });
  };

  startStreaming(options: {
    languageCodes: string[];
    model: "latest_long" | "latest_short" | "chirp_2";
    onTranscript: (p: TransciprtPayload) => void;
    onError: (e: unknown) => void;
  }) {
    const stream = this.speechClient._streamingRecognize();
    let closed = false;

    const markClosed = () => {
      closed = true;
    };

    stream.on("data", (response: any) => {
      for (const result of response.results ?? []) {
        const alternative = result.alternatives?.[0];
        if (!alternative) continue;

        // Final 결과는 항상 처리
        if (result.isFinal) {
          console.dir(result, { depth: null, colors: true });
          options.onTranscript({
            isFinal: true,
            transcriptText: alternative.transcript ?? "",
            confidence: alternative.confidence,
            resultEndTimeMs: toMillis(result.resultEndTimeMs),
          });
          continue;
        }

        // Interim 결과는 stability가 충분할 때만 처리
        if (result.stability < 0.85 && result.stability > 0) {
          return;
        }

        const transcript = alternative.transcript ?? "";
        options.onTranscript({
          isFinal: false,
          transcriptText: transcript,
          confidence: alternative.confidence,
          resultEndTimeMs: toMillis(result.resultEndTimeMs),
        });
      }
    });
    stream.on("error", (err: any) => {
      markClosed();
      options.onError?.(err);
    });
    stream.on("end", () => {
      markClosed();
      console.log("stream end");
    });
    stream.on("close", () => {
      markClosed();
      console.log("stream close");
    });

    let isConfigured = false;

    const request = {
      recognizer: this.recognizerName,
      streamingConfig: {
        config: {
          languageCodes: options.languageCodes,
          model: options.model,
          explicitDecodingConfig: {
            encoding: "LINEAR16",
            sampleRateHertz: 16000,
            audioChannelCount: 1,
          },
        },
        streamingFeatures: {
          interimResults: true,
          enableVoiceActivityEvents: true,
        },
      },
    };

    const assertAudioOnlyAfterConfig = (payload: any) => {
      if (!isConfigured) {
        console.log("isConfigured = false");
      }
      if (!payload || typeof payload !== "object") {
        throw new Error("not found payload or payload type is not object");
      }
      if ("recognizer" in payload || "streamingConfig" in payload) {
        throw new Error("Config detected after initial config");
      }
      if (!("audio" in payload)) throw new Error("Missing audio field");
    };

    const configureOnce = () => {
      if (isConfigured) return;
      stream.write(request);
      isConfigured = true;
      console.log("configured");
    };

    const writeAudioChunk = async (audioChunkBuffer: Buffer) => {
      if (
        closed ||
        !Buffer.isBuffer(audioChunkBuffer) ||
        audioChunkBuffer.length === 0
      )
        return;
      const CHUNK = 8192;
      for (let i = 0; i < audioChunkBuffer.length; i += CHUNK) {
        const slice = audioChunkBuffer.subarray(i, i + CHUNK);
        const payload = { audio: slice };

        assertAudioOnlyAfterConfig(payload);
        stream.write(payload);
      }
    };

    const stop = async () => {
      if (closed) return;
      markClosed();
      try {
        stream.end();
      } finally {
        isConfigured = false;
      }
    };

    const isOpen = () => !closed;

    return { configureOnce, writeAudioChunk, stop, isOpen };
  }
}
