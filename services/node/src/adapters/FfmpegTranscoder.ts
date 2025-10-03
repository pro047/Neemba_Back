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
      "rtmp://neemba.app:1935/live/translation",
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

    if (child) {
      console.log("ffmpeg spawned");
    }

    this.childProcess = child;

    this.childProcess.stderr.on("data", (d) => console.log(d));
    this.childProcess.on("error", (e) => console.error("spawn error:", e));
    this.childProcess.on("close", (c) => console.log("ffmpeg exit code :", c));

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
