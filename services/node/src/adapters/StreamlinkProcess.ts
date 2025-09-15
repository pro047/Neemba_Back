import { spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import type { LiveMediaReader } from "../ports/ports";
import { Readable } from "node:stream";

export class StreamlinkProcess implements LiveMediaReader {
  private childProcess?: ChildProcessWithoutNullStreams | undefined;

  constructor(
    private youtubeUrl: string,
    private userAgent: string = "Mozilla/5.0"
  ) {}

  start(): { mediaReadable: Readable } {
    const streamlinkArguments = [
      "--stdout",
      "--http-header",
      `User-Agent=${this.userAgent}`,
      "--retry-open",
      "9999",
      "--retry-streams",
      "9999",
      "--hls-live-edge",
      "3",
      this.youtubeUrl,
      "best",
    ];

    const child = spawn("streamlink", streamlinkArguments, {
      stdio: ["pipe", "pipe", "pipe"],
    }) as ChildProcessWithoutNullStreams;

    this.childProcess = child;

    return { mediaReadable: child.stdout };
  }

  stop(): void {
    const childProcess = this.childProcess;
    if (!childProcess) return;

    this.childProcess = undefined;
    try {
      childProcess.kill("SIGTERM");
    } catch {}
  }
}
