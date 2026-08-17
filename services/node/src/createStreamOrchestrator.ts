import { GoogleAuth } from "google-auth-library";
import { v2 as speech } from "@google-cloud/speech";
import { GoogleRecognizerRepository } from "./adapters/GoogleRecognizerRepository.js";
import { GoogleSttV2Adapter } from "./adapters/googleSttV2.js";
import { natsUrl } from "./config.js";
import { JetStreamTranscriptPublisher } from "./js_pub.js";
import { RetryingTranscriptPublisher } from "./retryingPublisher.js";
import {
  incPublishBufferDropped,
  setPublishBufferSize,
} from "./monitoring/metrics.js";
import { SegmentManager } from "./stream/SegmentManager.js";
import { StreamSwitcher } from "./stream/StreamSwitcher.js";
import { InterimChunkOrchestrator } from "./usecases/InterimChunkOrchestratotr.js";
import { StreamOrchestrator } from "./usecases/StreamOrchestrator.js";

// Missing NATS_URL fails fast inside JetStreamTranscriptPublisher.start().
const url = natsUrl;

type StreamLanguages = {
  sourceLanguage?: string;
  targetLanguage?: string;
};

// sessionId must be the SAME id the caller later passes to
// orchestrator.start(pcm, {sessionId}): the queue-depth hook below labels the
// buffer gauge with this id, while start()'s failure/stop paths remove the
// series under its own id — a mismatch would leak the series forever.
export async function createStreamOrchestrator(
  sessionId: string,
  languages: StreamLanguages = {}
): Promise<StreamOrchestrator> {
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

  const transcriptPublisher = new RetryingTranscriptPublisher(
    new JetStreamTranscriptPublisher(url),
    undefined,
    undefined,
    undefined,
    {
      onDropped: incPublishBufferDropped,
      onQueueSize: (size) => setPublishBufferSize(sessionId, size),
    }
  );
  // Seeding at 0 happens in StreamOrchestrator.start(), not here: assembly is
  // not a teardown-covered scope (a throw between this factory and start()
  // would leave a series nothing can remove), so seed and remove both live
  // inside start()'s try/catch.
  const segmentManager = new SegmentManager();
  const switcher = new StreamSwitcher((segmentId) => {
    console.log("stream switcher : current segmentId = ", segmentId);
  });
  const interimChunkOrchestra = new InterimChunkOrchestrator(
    transcriptPublisher,
    {
      sourceLanguage: languages.sourceLanguage ?? "ko-KR",
      targetLanguage: languages.targetLanguage ?? "en-US",
    }
  );

  return new StreamOrchestrator(
    googleRecognizer,
    switcher,
    interimChunkOrchestra,
    segmentManager
  );
}
