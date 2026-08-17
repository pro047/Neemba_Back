import { PassThrough } from "node:stream";
import { createStreamOrchestrator } from "./createStreamOrchestrator.js";
import type { AudioConsumerPort } from "./ports/audioConsumerPort.js";
import type { MicRuntime } from "./sessionRuntimeStore.js";

type StreamLanguages = {
  sourceLanguage?: string;
  targetLanguage?: string;
};

type RunMicPipelineDependencies = {
  consumer: AudioConsumerPort;
  sessionId?: string;
};

export async function runMicPipeline({
  consumer,
  sessionId,
}: RunMicPipelineDependencies): Promise<MicRuntime> {
  const inputWritable = new PassThrough();
  const stopConsumer = await consumer.start(
    inputWritable,
    sessionId == null ? undefined : { sessionId }
  );
  let stopped = false;

  return {
    inputWritable,
    stop: async () => {
      if (stopped) {
        return;
      }

      stopped = true;
      inputWritable.end();
      await stopConsumer();
    },
  };
}

// sessionId is required now: the orchestrator factory labels the session's
// gauge series with it, and start() would reject an absent id anyway.
export async function runDefaultMicPipeline(
  sessionId: string,
  languages: StreamLanguages = {}
): Promise<MicRuntime> {
  const orchestrator = await createStreamOrchestrator(sessionId, languages);
  return runMicPipeline({ consumer: orchestrator, sessionId });
}
