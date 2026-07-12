import { FfmpegTranscoder } from "./adapters/FfmpegTranscoder.js";
import { StreamlinkToConsumerService } from "./usecases/StreamlinkToConsumerService.js";
import { createStreamOrchestrator } from "./createStreamOrchestrator.js";

type PipelineLanguages = {
  sourceLanguage?: string;
  targetLanguage?: string;
};

export async function runPipelines(
  languages: PipelineLanguages = {}
): Promise<{ stop: () => Promise<void> }> {
  const ffmpeg = new FfmpegTranscoder();
  const orchestrator = await createStreamOrchestrator(languages);

  // 유즈 케이스 실행
  const service = new StreamlinkToConsumerService(ffmpeg, orchestrator);

  const serviceStop = await service.run();

  console.log("translate Starting");

  return { stop: serviceStop };
}
