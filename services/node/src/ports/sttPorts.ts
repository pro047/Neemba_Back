export interface SpeechToTextPort {
  startStreaming(options: {
    languageCodes: string[];
    model: "latest_long" | "latest_short" | "chirp_2";
    onTranscript: (payload: {
      transcriptText: string;
      isFinal: boolean;
      confidence?: number;
    }) => void;
    onError: (error: unknown) => void;
  }): {
    configureOnce: () => void;
    writeAudioChunk: (audioChunkBuffer: Buffer) => Promise<void>;
    stop: () => Promise<void>;
    isOpen: () => boolean;
  };
}
