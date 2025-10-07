import { spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import type { AudioTranscoder } from "../ports/ports";
import type { Readable, Writable } from "node:stream";

export class FfmpegTranscoder implements AudioTranscoder {
  private childProcess?: ChildProcessWithoutNullStreams | undefined;
  private healthCheckTimer: NodeJS.Timeout | null = null;
  startTranscoder(): {
    inputWritable: Writable;
    pcmReadable: Readable;
    stop: () => void;
  } {
    const ffmpegArguments = [
      "-hide_banner",
      "-loglevel",
      "info",
      "-analyzeduration",
      "0",
      "-probesize",
      "32k",
      "-fflags",
      "nobuffer",
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
      "-progress",
      "pipe:2",
    ];

    const child = spawn("ffmpeg", ffmpegArguments, {
      stdio: ["pipe", "pipe", "pipe"],
    }) as ChildProcessWithoutNullStreams;

    let lastProcessAt = Date.now();

    this.childProcess = child;

    this.childProcess.stderr.on("data", (d) => {
      const str = d.toString();
      if (str.includes("out_time_ms")) {
        lastProcessAt = Date.now();
      }
    });

    this.healthCheckTimer = setInterval(() => {
      if (Date.now() - lastProcessAt > 3000) {
        console.warn("ffmpeg process not updated for 3s");
      }
    }, 3000);

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
        if (this.healthCheckTimer) {
          clearInterval(this.healthCheckTimer);
          this.healthCheckTimer = null;
        }
      },
    };
  }
}
