import { v2 as speech } from "@google-cloud/speech";
import type {
  RecognizerCreateInput,
  RecognizerLocation,
  RecognizerRepository,
  RecognizerSummary,
} from "../repositories/RecongnizerRepository";

export class GoogleRecognizerRepository implements RecognizerRepository {
  createSpeechClientByParent(parent: string): speech.SpeechClient {
    const match = parent.match(/^projects\/[^/]+\/locations\/([^/]+)$/);
    if (!match) throw new Error(`Invalid parent : ${parent}`);
    const location = match[1] as RecognizerLocation;

    return location == "global"
      ? new speech.SpeechClient()
      : new speech.SpeechClient({
          apiEndpoint: `${location}-speech.googleapis.com`,
        });
  }

  toSummary(recognizer: any): RecognizerSummary {
    return {
      name: recognizer.name,
      displayName: recognizer.displayName ?? null,
      languageCodes:
        recognizer.languageCodes ??
        recognizer.defaultRecognitionConfig?.languageCodes ??
        [],
      model:
        recognizer.model ?? recognizer.defaultRecognitionConfig?.model ?? null,
      state: recognizer.state ?? null,
    };
  }

  async list(parent: string): Promise<RecognizerSummary[]> {
    const client = this.createSpeechClientByParent(parent);
    const [recognizers] = await client.listRecognizers({ parent });
    return recognizers.map(this.toSummary);
  }

  async create(input: RecognizerCreateInput): Promise<string> {
    const client = this.createSpeechClientByParent(input.parent);
    const [op] = await client.createRecognizer({
      parent: input.parent,
      recognizerId: input.recognizerId,
      recognizer: {
        displayName: input.displayName ?? "",
        defaultRecognitionConfig: {
          languageCodes: input.languageCodes,
          model: input.model,
          explicitDecodingConfig: input.sampleRateHertz
            ? {
                encoding: "LINEAR16",
                sampleRateHertz: input.sampleRateHertz,
                audioChannelCount: input.audioChannelCount ?? 1,
              }
            : null,
          features: input.enableAutomaticPunctuation
            ? { enableAutomaticPunctuation: true }
            : null,
        },
      },
    });

    const [created] = await op.promise();
    if (!created.name)
      throw new Error("Recognizer creation returned empty name");
    return created.name;
  }
  async delete(recongnizerResourceName: string): Promise<void> {
    const matched = recongnizerResourceName.match(
      /^projects\/[^/]+\/locations\/([^/]+)\/recognizers\/[^/]+$/
    );
    if (!matched)
      throw new Error(
        `Inavlid recognizer resource name: ${recongnizerResourceName}`
      );
    const location = matched[1] as RecognizerLocation;

    const client =
      location === "global"
        ? new speech.SpeechClient()
        : new speech.SpeechClient({
            apiEndpoint: `${location}-speech.googleapis.com`,
          });

    const [op] = await client.deleteRecognizer({
      name: recongnizerResourceName,
    });
    await op.promise();
  }
  async ensure(input: RecognizerCreateInput): Promise<string> {
    const existing = await this.list(input.parent);

    const found = existing.find((r) =>
      r.name.endsWith(`/recognizers/${input.recognizerId}`)
    );
    if (found) return found.name;
    try {
      console.log("ensure create", this.create(input));

      return await this.create(input);
    } catch (err: any) {
      if (err.code === 6) {
        const latest = await this.list(input.parent);
        const retry = latest.find((r) =>
          r.name.endsWith(`/recognizers/${input.recognizerId}`)
        );
        if (retry?.name) {
          return retry.name;
        }
      }
      throw err;
    }
  }
}
