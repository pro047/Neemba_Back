import { spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import type { AudioTranscoder } from "../ports/ports";
import type { Readable, Writable } from "node:stream";

export class FfmpegTranscoder implements AudioTranscoder {
  private childProcess?: ChildProcessWithoutNullStreams | undefined;

  startTranscoder(): {
    inputWritable: Writable;
    pcmReadable: Readable;
    stop: () => void;
  } {
    const ffmpegArguments = [
      "-hide_banner",
      "-loglevel",
      "warning",
      "-fflags",
      "nobuffer",
      "-flags",
      "low_delay",
      "-i",
      "rtmp://neemba.app/live/translation",
      "-vn",
      "-ac",
      "1",
      "-ar",
      "16000",
      "-acodec",
      "pcm_s16le",
      "-f",
      "s16le",
      "pipe:1",
    ];

    const child = spawn("ffmpeg", ffmpegArguments, {
      stdio: ["pipe", "pipe", "pipe"],
    }) as ChildProcessWithoutNullStreams;

    this.childProcess = child;

    return {
      inputWritable: child.stdin,
      pcmReadable: child.stdout,
      stop: () => {
        if (child) {
          child.kill("SIGKILL");
          this.childProcess = undefined;
        }
      },
    };
  }
}
