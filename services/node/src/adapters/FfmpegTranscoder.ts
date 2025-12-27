import { spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import type { AudioTranscoder } from "../ports/ports";
import { PassThrough } from "node:stream";
import type { Readable, Writable } from "node:stream";

export class FfmpegTranscoder implements AudioTranscoder {
  private childProcess?: ChildProcessWithoutNullStreams | undefined;
  private healthCheckTimer: NodeJS.Timeout | null = null;
  private restarting = false;
  private killedChildren = new WeakSet<ChildProcessWithoutNullStreams>();
  private killTimers = new WeakMap<ChildProcessWithoutNullStreams, NodeJS.Timeout>();

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
    let lastStderrLogAt = 0;
    let childStartAt = Date.now();

    const attachTranscoder = () => {
      const child = this.spawnFfmpeg();

      lastProcessAt = Date.now();
      childStartAt = lastProcessAt;

      child.stdout.on("data", (chunk) => {
        outputMultiplexer.write(chunk);
      });

      child.stderr.on("data", (d) => {
        const str = d.toString();
        if (str.includes("out_time_ms")) {
          lastProcessAt = Date.now();
        } else {
          const now = Date.now();
          if (now - lastStderrLogAt > 1000) {
            lastStderrLogAt = now;
            console.warn("ffmpeg stderr:", str.trim());
          }
        }
      });

      child.on("error", (e) => console.error("ffmpeg spawn error:", e));
      child.on("close", (code, signal) => {
        const killedByUs = this.killedChildren.has(child);
        const killTimer = this.killTimers.get(child);
        if (killTimer) {
          clearTimeout(killTimer);
          this.killTimers.delete(child);
        }
        console.log("ffmpeg exit:", { code, signal, killedByUs });
        if (!stopped && !killedByUs) {
          this.requestRestart(attachTranscoder, "exit");
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
      const now = Date.now();
      if (now - childStartAt < 15000) return;
      if (now - lastProcessAt > 10000) {
        console.warn("ffmpeg process not updated for 10s");
        this.requestRestart(attachTranscoder, "stalled");
      }
    }, 5000);

    return {
      inputWritable: inputProxy,
      pcmReadable: outputMultiplexer,
      stop: () => {
        stopped = true;
        this.disposeChild("stop");
        if (this.healthCheckTimer) {
          clearInterval(this.healthCheckTimer);
          this.healthCheckTimer = null;
        }
        inputProxy.end();
        outputMultiplexer.end();
      },
    };
  }

  private requestRestart(
    attachTranscoder: () => void,
    reason: "stalled" | "exit"
  ): void {
    if (this.restarting) return;
    this.restarting = true;
    queueMicrotask(() => {
      try {
        console.warn("ffmpeg restart requested:", reason);
        this.disposeChild("restart");
        attachTranscoder();
      } finally {
        this.restarting = false;
      }
    });
  }

  private disposeChild(reason: "restart" | "stop") {
    if (!this.childProcess) return;
    const child = this.childProcess;
    this.killedChildren.add(child);
    try {
      child.kill("SIGTERM");
    } catch {}
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {}
    }, 2000);
    this.killTimers.set(child, timer);
    this.childProcess = undefined;
  }
}
