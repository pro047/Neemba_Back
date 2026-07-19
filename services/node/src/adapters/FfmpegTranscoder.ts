import { spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import type { AudioTranscoder } from "../ports/ports.js";
import { PassThrough } from "node:stream";
import type { Readable, Writable } from "node:stream";

// "No publisher yet" (OBS not live) is a legitimate indefinite state, so
// restarts never give up — they back off exponentially to stop the 10s
// restart churn, and reset to the base delay once real progress arrives.
const DEFAULT_RESTART_BASE_DELAY_MS = 10_000;
const DEFAULT_RESTART_MAX_DELAY_MS = 60_000;
const DEFAULT_RTMP_PULL_URL = "rtmp://neemba.app:1935/live/translation";

type RestartBackoffOptions = {
  restartBaseDelayMs?: number;
  restartMaxDelayMs?: number;
};

export class FfmpegTranscoder implements AudioTranscoder {
  private childProcess?: ChildProcessWithoutNullStreams | undefined;
  private healthCheckTimer: NodeJS.Timeout | null = null;
  private restartTimer: NodeJS.Timeout | null = null;
  // The pending restart's action, kept so restartNow() can fire it early.
  private restartAction: (() => void) | null = null;
  private consecutiveRestarts = 0;
  private readonly restartBaseDelayMs: number;
  private readonly restartMaxDelayMs: number;
  private killedChildren = new WeakSet<ChildProcessWithoutNullStreams>();
  private killTimers = new WeakMap<ChildProcessWithoutNullStreams, NodeJS.Timeout>();

  constructor(
    private readonly spawnProcess: typeof spawn = spawn,
    options: RestartBackoffOptions = {}
  ) {
    this.restartBaseDelayMs =
      options.restartBaseDelayMs ?? DEFAULT_RESTART_BASE_DELAY_MS;
    this.restartMaxDelayMs =
      options.restartMaxDelayMs ?? DEFAULT_RESTART_MAX_DELAY_MS;
  }

  private spawnFfmpeg() {
    const ffmpegArguments = [
      "-hide_banner",
      "-loglevel",
      "info",
      // §4-2: 0 is NOT "skip analysis" — it means "format default", and for
      // live FLV the probe then effectively runs until -probesize fills,
      // which at low audio bitrates delays first output by seconds. 0.5s
      // bounds the probe explicitly regardless of input bitrate.
      "-analyzeduration",
      "500000",
      "-probesize",
      "32k",
      "-fflags",
      "nobuffer",
      "-i",
      // Read at spawn time (not import time) so tests and container env
      // both take effect. Prod sets the internal hostname (rtmp:1935) to
      // avoid hairpinning through the public internet.
      process.env.RTMP_PULL_URL ?? DEFAULT_RTMP_PULL_URL,
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

    const child = this.spawnProcess("ffmpeg", ffmpegArguments, {
      stdio: ["pipe", "pipe", "pipe"],
    }) as ChildProcessWithoutNullStreams;

    // Without an 'error' listener, a write into a just-died ffmpeg (the
    // EPIPE window before 'close' fires) raises an uncaught 'error' event
    // and kills the whole process. Restart handling stays on 'close'.
    child.stdin.on("error", (e: Error) => {
      console.warn("ffmpeg stdin error (ignored):", e.message);
    });

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
          // Real progress: the publisher is live, so the next failure is a
          // fresh incident — return to the base restart delay.
          this.consecutiveRestarts = 0;
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
        if (this.restartTimer) {
          clearTimeout(this.restartTimer);
          this.restartTimer = null;
          this.restartAction = null;
        }
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
    if (this.restartTimer) return;
    // Exponential backoff, no give-up: waiting for a publisher is a normal
    // state (operator opens the session before OBS goes live), but immediate
    // respawns every 10s spam logs and churn CPU until the session closes.
    const delay = Math.min(
      this.restartBaseDelayMs * 2 ** this.consecutiveRestarts,
      this.restartMaxDelayMs
    );
    this.consecutiveRestarts += 1;
    console.warn(`ffmpeg restart requested: ${reason} (in ${delay}ms)`);
    const fire = () => {
      this.restartTimer = null;
      this.restartAction = null;
      this.disposeChild("restart");
      attachTranscoder();
    };
    this.restartAction = fire;
    this.restartTimer = setTimeout(fire, delay);
    this.restartTimer.unref?.();
  }

  /** §4-4: nginx-rtmp on_publish 신호로 대기 중인 재시작을 즉시 실행한다.
   *
   * 백오프 폴링은 "publisher 가 언제 돌아올지 모른다"는 전제인데 on_publish
   * 는 그 도착 신호 그 자체 — 최대 60s 를 기다릴 이유가 없다. 재시작이
   * 예약돼 있지 않으면(정상 동작 중이거나 stop 이후) no-op.
   */
  restartNow(): void {
    if (!this.restartTimer || !this.restartAction) return;
    clearTimeout(this.restartTimer);
    // New publisher = fresh incident: next failure backs off from the base.
    this.consecutiveRestarts = 0;
    console.log("ffmpeg restart: publisher returned (on_publish), restarting now");
    this.restartAction();
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
