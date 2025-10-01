import type { AudioTranscoder } from "../ports/ports";
import type { AudioConsumerPort } from "../ports/audioConsumerPort";

export class StreamlinkToConsumerService {
  constructor(
    private readonly ffmpeg: AudioTranscoder,
    private readonly orchestra: AudioConsumerPort
  ) {}

  async run(): Promise<() => Promise<void>> {
    console.log("consumer service run");

    const {
      inputWritable,
      pcmReadable,
      stop: stopTranscoder,
    } = this.ffmpeg.startTranscoder();

    const streamStop = await this.orchestra.start(pcmReadable);

    let alreadyStopped = false;

    const stop = async () => {
      if (alreadyStopped) return;
      alreadyStopped = true;

      try {
        inputWritable.end();
      } catch {}
      try {
        await streamStop();
      } catch {}
      await Promise.resolve(stopTranscoder());
    };

    process.once("SIGINT", () => {
      stop().finally(() => process.exit(0));
    });
    process.once("SIGTERM", () => {
      stop().finally(() => process.exit(0));
    });

    return stop;
  }
}
