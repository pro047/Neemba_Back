import { FfmpegTranscoder } from "./adapters/FfmpegTranscoder";
import { StreamlinkProcess } from "./adapters/StreamlinkProcess";
import { StreamlinkToConsumerService } from "./usecases/StreamlinkToConsumerService";
import { YtDlpProcess } from "./adapters/YtDlpProcess";
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
import { StableSuffixGate } from "./stream/StableSuffixGate";
import { LcpStabilizer } from "./stream/lcp";

const url = natsUrl || "nats://neemba:nats1234@localhost:4222";
console.log(url);

export async function runPipelines(
  youtubeUrl: string
): Promise<{ stop: () => Promise<void> }> {
  const cookieFilePath =
    process.env.COOKIE_FILE_PATH ?? "/var/lib/neemba/secrets/cookies.txt";

  const youtubeUserAgent =
    process.env.YOUTUBE_USER_AGENT ||
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36";

  const extractor = new YtDlpProcess(cookieFilePath);

  const playableUrl = await extractor.getStreamUrl(youtubeUrl);

  console.log("playableUrl", playableUrl);

  // 어댑터 준비
  const liveMediaReader = new StreamlinkProcess(playableUrl, youtubeUserAgent);

  const audioTranscoder = new FfmpegTranscoder();

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

  const gate = new StableSuffixGate(4);

  const stabilizer = new LcpStabilizer();

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
  const service = new StreamlinkToConsumerService(
    audioTranscoder,
    liveMediaReader,
    orchestrator
  );

  const serviceStop = await service.run();

  console.log("Starting Streamlink to Consumer Service...");

  return { stop: serviceStop };
}
