import { FfmpegTranscoder } from "./adapters/FfmpegTranscoder.js";
import { StreamlinkToConsumerService } from "./usecases/StreamlinkToConsumerService.js";
import { createStreamOrchestrator } from "./createStreamOrchestrator.js";

type PipelineLanguages = {
  sourceLanguage?: string;
  targetLanguage?: string;
};

export async function runPipelines(
  languages: PipelineLanguages = {}
): Promise<{
  stop: () => Promise<void>;
  notifyPublisherReturned: () => void;
}> {
  const ffmpeg = new FfmpegTranscoder();
  const orchestrator = await createStreamOrchestrator(languages);

  // 유즈 케이스 실행
  const service = new StreamlinkToConsumerService(ffmpeg, orchestrator);

  const serviceStop = await service.run();

  console.log("translate Starting");

  return {
    stop: serviceStop,
    // §4-4: on_publish 훅이 "publisher 복귀" 신호를 주면 ffmpeg 의 백오프
    // 대기를 건너뛰고 즉시 재접속한다 (재송출 후 최대 60s 공백 제거).
    notifyPublisherReturned: () => ffmpeg.restartNow(),
  };
}
