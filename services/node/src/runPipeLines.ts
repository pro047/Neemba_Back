import { FfmpegTranscoder } from "./adapters/FfmpegTranscoder";
import { StreamlinkToConsumerService } from "./usecases/StreamlinkToConsumerService";
import { StreamOrchestrator } from "./usecases/StreamOrchestrator";
import { GoogleSttV2Adapter } from "./adapters/googleSttV2";
import { JetStreamTranscriptPublisher } from "./js_pub";
import { natsUrl } from "./config";
import { GoogleRecognizerRepository } from "./adapters/GoogleRecognizerRepository";
import { GoogleAuth } from "google-auth-library";
import { v2 as speech } from "@google-cloud/speech";
import { InterimChunkOrchestrator } from "./usecases/InterimChunkOrchestratotr";
import { SegmentManager } from "./stream/SegmentManager";
import { StreamSwitcher } from "./stream/StreamSwitcher";

const url = natsUrl || "nats://neemba:nats1234@localhost:4222";
console.log(url);

export async function runPipelines(): Promise<{ stop: () => Promise<void> }> {
  const ffmpeg = new FfmpegTranscoder();

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

  const orchestrator = new StreamOrchestrator(
    googleRecognizer,
    switcher,
    interimChunkOrchestra,
    segmentManager
  );

  // 유즈 케이스 실행
  const service = new StreamlinkToConsumerService(ffmpeg, orchestrator);

  const serviceStop = await service.run();

  console.log("translate Starting");

  return { stop: serviceStop };
}
