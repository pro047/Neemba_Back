export type RecognizerLocation = "global" | "us-central1";

export interface RecognizerCreateInput {
  parent: string;
  recognizerId: string;
  displayName?: string;
  languageCodes: string[];
  model: "latest_long" | "latest_short" | "chirp_2" | "chirp_3";
  sampleRateHertz?: number;
  audioChannelCount?: number;
  enableAutomaticPunctuation?: boolean;
}

export interface RecognizerSummary {
  name: string;
  displayName?: string | null;
  languageCodes: string[];
  model?: string | null;
  state?: string | null;
}

export interface RecognizerRepository {
  list(parent: string): Promise<RecognizerSummary[]>;
  create(input: RecognizerCreateInput): Promise<string>;
  delete(recongnizerResourceName: string): Promise<void>;
  ensure(input: RecognizerCreateInput): Promise<string>;
}
