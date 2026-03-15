import { PassThrough } from "node:stream";
import { createStreamOrchestrator } from "./createStreamOrchestrator.js";
import type { AudioConsumerPort } from "./ports/audioConsumerPort.js";
import type { MicRuntime } from "./sessionRuntimeStore.js";

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

export async function runDefaultMicPipeline(sessionId?: string): Promise<MicRuntime> {
  const orchestrator = await createStreamOrchestrator();
  return runMicPipeline(
    sessionId == null
      ? { consumer: orchestrator }
      : { consumer: orchestrator, sessionId }
  );
}
