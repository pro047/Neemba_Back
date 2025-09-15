import type { AudioTranscoder, LiveMediaReader } from "../ports/ports";
import type { AudioConsumerPort } from "../ports/audioConsumerPort";

export class StreamlinkToConsumerService {
  constructor(
    private readonly audioTranscoder: AudioTranscoder,
    private readonly liveMediaReader: LiveMediaReader,
    private readonly audioConsumer: AudioConsumerPort
  ) {}

  async run(): Promise<() => Promise<void>> {
    console.log("consumer service run");

    const { mediaReadable } = this.liveMediaReader.start();
    const {
      inputWritable,
      pcmReadable,
      stop: stopTranscoder,
    } = this.audioTranscoder.startTranscoder();

    mediaReadable.pipe(inputWritable);

    const streamStop = await this.audioConsumer.start(pcmReadable);

    let alreadyStopped = false;
    const stop = async () => {
      if (alreadyStopped) return;
      alreadyStopped = true;

      try {
        mediaReadable.destroy();
      } catch {}
      try {
        inputWritable.end();
      } catch {}
      try {
        await streamStop();
      } catch {}
      await Promise.resolve(stopTranscoder());
      await Promise.resolve(this.liveMediaReader.stop());
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
