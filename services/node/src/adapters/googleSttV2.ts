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
    const recognizer = await this.speechClient.getRecognizer({
      name: this.recognizerName,
    });
    console.log("recognizer", recognizer);
  };

  startStreaming(options: {
    languageCodes: string[];
    model: "latest_long" | "latest_short" | "chirp_2";
    onTranscript: (p: TransciprtPayload) => void;
    onError: (e: unknown) => void;
  }) {
    const stream = this.speechClient._streamingRecognize();

    stream.on("data", (response: any) => {
      for (const result of response.results ?? []) {
        if (
          (result.stability < 0.85 && result.stability > 0) ||
          result.isFinal
        ) {
          return;
        }

        const alternative = result.alternatives?.[0];
        const transcript = alternative?.transcript ?? "";

        if (!alternative) continue;
        options.onTranscript({
          isFinal: false,
          transcriptText: transcript ?? "",
          confidence: alternative.confidence,
          resultEndTimeMs: toMillis(result.resultEndTimeMs),
        });
      }
    });
    stream.on("error", (err: any) => options.onError?.(err));
    stream.on("end", () => {
      console.log("stream end");
    });
    stream.on("close", () => {
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
      if (!Buffer.isBuffer(audioChunkBuffer) || audioChunkBuffer.length === 0)
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
      try {
        stream.end();
      } finally {
        isConfigured = false;
      }
    };

    return { configureOnce, writeAudioChunk, stop };
  }
}
