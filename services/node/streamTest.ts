import { v2 as speech } from "@google-cloud/speech";
import fs from "fs";

async function run() {
  const projectId = "neemba-1755584539";
  const recognizerName =
    "projects/1036734944133/locations/us-central1/recognizers/neemba-recognizer";

  const client = new speech.SpeechClient({
    apiEndpoint: "us-central1-speech.googleapis.com",
    keyFilename: process.env.GOOGLE_APPLICATION_CREDENTIALS,
  });

  const stream = client._streamingRecognize();

  stream.on("data", (response: any) => {
    for (const result of response.results ?? []) {
      const alternative = result.alternatives?.[0];
      if (!alternative) continue;
      const transcript = alternative.transcript ?? "";
      const isFinal = alternative.isFinal ?? false;
      const confidence = alternative.confidence;
      console.log(
        isFinal ? "[Final]" : "[Interim]",
        transcript,
        confidence ?? ""
      );
    }
  });

  stream.on("error", (err: any) => {
    console.error("streaming error name:", err?.name);
    console.error("streaming error code:", err?.code);
    console.error("streaming error details:", err?.details || err?.message);
    console.error("streaming error metadata:", err?.metadata);
  });
  stream.on("end", () => {
    console.log("stream ended");
  });

  stream.write({
    recognizer: recognizerName,
    streamingConfig: {
      config: {
        languageCodes: ["ko-KR"],
        explicitDecodingConfig: {
          encoding: "LINEAR16",
          sampleRateHertz: 16000,
          audioChannelCount: 1,
        },
      },
      singleUtterance: false,
      interimResults: true,
    },
  });

  process.stdin.on("data", (chunk: Buffer) => {
    stream.write({ audio: chunk });
  });
  process.stdin.on("end", () => {
    stream.end();
  });

  process.on("SIGINT", () => {
    try {
      stream.end();
    } catch {}
    process.exit(0);
  });
}

run().catch((e) => {
  console.error;
  process.exit(1);
});
