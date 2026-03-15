import { GoogleAuth } from "google-auth-library";
import { v2 as speech } from "@google-cloud/speech";
import { GoogleRecognizerRepository } from "./adapters/GoogleRecognizerRepository.js";
import { GoogleSttV2Adapter } from "./adapters/googleSttV2.js";
import { natsUrl } from "./config.js";
import { JetStreamTranscriptPublisher } from "./js_pub.js";
import { SegmentManager } from "./stream/SegmentManager.js";
import { StreamSwitcher } from "./stream/StreamSwitcher.js";
import { InterimChunkOrchestrator } from "./usecases/InterimChunkOrchestratotr.js";
import { StreamOrchestrator } from "./usecases/StreamOrchestrator.js";

const url = natsUrl || "nats://neemba:nats1234@localhost:4222";

export async function createStreamOrchestrator(): Promise<StreamOrchestrator> {
  const auth = new GoogleAuth({
    scopes: "https://www.googleapis.com/auth/cloud-platform",
  });

  const projectId = await auth.getProjectId();

  const recognizer = new GoogleRecognizerRepository();

  const recognizerName = await recognizer.ensure({
    parent: `projects/${projectId}/locations/us-central1`,
    recognizerId: "neemba-recognizer",
    languageCodes: ["ko-KR"],
    model: "latest_long",
    displayName: "neemba",
    sampleRateHertz: 16000,
    enableAutomaticPunctuation: true,
  });

  const speechClient = new speech.SpeechClient({
    apiEndpoint: "us-central1-speech.googleapis.com",
  });
  const googleRecognizer = new GoogleSttV2Adapter(speechClient, recognizerName);

  await googleRecognizer.getRecognizer();

  const transcriptPublisher = new JetStreamTranscriptPublisher(url);
  const segmentManager = new SegmentManager();
  const switcher = new StreamSwitcher((segmentId) => {
    console.log("stream switcher : current segmentId = ", segmentId);
  });
  const interimChunkOrchestra = new InterimChunkOrchestrator(
    transcriptPublisher
  );

  return new StreamOrchestrator(
    googleRecognizer,
    switcher,
    interimChunkOrchestra,
    segmentManager
  );
}
