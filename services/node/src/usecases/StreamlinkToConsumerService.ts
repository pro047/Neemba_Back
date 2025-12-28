import type { AudioTranscoder } from "../ports/ports.js";
import type { AudioConsumerPort } from "../ports/audioConsumerPort.js";

// 전역 변수로 리스너가 등록되었는지 추적
let signalListenersRegistered = false;

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

    // 리스너가 이미 등록되었으면 건너뛰기
    if (!signalListenersRegistered) {
      signalListenersRegistered = true;
      process.once("SIGINT", () => {
        stop().finally(() => process.exit(0));
      });
      process.once("SIGTERM", () => {
        stop().finally(() => process.exit(0));
      });
    }

    return stop;
  }
}
