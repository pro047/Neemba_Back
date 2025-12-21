import { spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import type { AudioTranscoder } from "../ports/ports";
import { PassThrough } from "node:stream";
import type { Readable, Writable } from "node:stream";

export class FfmpegTranscoder implements AudioTranscoder {
  private childProcess?: ChildProcessWithoutNullStreams | undefined;
  private healthCheckTimer: NodeJS.Timeout | null = null;
  private restarting = false;

  private spawnFfmpeg() {
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

    this.childProcess = child;
    return child;
  }

  startTranscoder(): {
    inputWritable: Writable;
    pcmReadable: Readable;
    stop: () => void;
  } {
    const outputMultiplexer = new PassThrough();
    const inputProxy = new PassThrough();
    let stopped = false;
    let lastProcessAt = Date.now();

    const attachTranscoder = () => {
      const child = this.spawnFfmpeg();

      lastProcessAt = Date.now();

      child.stdout.on("data", (chunk) => {
        outputMultiplexer.write(chunk);
      });

      child.stderr.on("data", (d) => {
        const str = d.toString();
        if (str.includes("out_time_ms")) {
          lastProcessAt = Date.now();
        }
      });

      child.on("error", (e) => console.error("spawn error:", e));
      child.on("close", (code) => {
        console.log("ffmpeg exit code :", code);
        if (!stopped) {
          this.requestRestart(attachTranscoder);
        }
      });
    };

    inputProxy.on("data", (chunk) => {
      const child = this.childProcess;
      if (!child || child.stdin.destroyed) return;
      child.stdin.write(chunk);
    });

    inputProxy.on("end", () => {
      const child = this.childProcess;
      if (!child || child.stdin.destroyed) return;
      child.stdin.end();
    });

    attachTranscoder();

    this.healthCheckTimer = setInterval(() => {
      if (Date.now() - lastProcessAt > 3000) {
        console.warn("ffmpeg process not updated for 3s");
        this.requestRestart(attachTranscoder);
      }
    }, 3000);

    return {
      inputWritable: inputProxy,
      pcmReadable: outputMultiplexer,
      stop: () => {
        stopped = true;
        this.disposeChild();
        if (this.healthCheckTimer) {
          clearInterval(this.healthCheckTimer);
          this.healthCheckTimer = null;
        }
        inputProxy.end();
        outputMultiplexer.end();
      },
    };
  }

  private requestRestart(attachTranscoder: () => void): void {
    if (this.restarting) return;
    this.restarting = true;
    queueMicrotask(() => {
      try {
        this.disposeChild();
        attachTranscoder();
      } finally {
        this.restarting = false;
      }
    });
  }

  private disposeChild() {
    if (!this.childProcess) return;
    try {
      this.childProcess.kill("SIGKILL");
    } catch {}
    this.childProcess = undefined;
  }
}
