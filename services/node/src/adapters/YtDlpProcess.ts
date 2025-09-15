import { execFile } from "child_process";
import type { LiveMediaExtracter } from "../ports/ports";
import { promisify } from "util";
import { access } from "fs/promises";
import { promises as fs } from "fs";

const execFileAsync = promisify(execFile);

export type YoutubeClient =
  | "web"
  | "android"
  | "ios"
  | "mweb"
  | "web_embedded"
  | "android_embedded";

const DefalutClientsOrder: YoutubeClient[] = [
  "web",
  "android",
  "ios",
  "mweb",
  "web_embedded",
  "android_embedded",
];

const DesktopUserAgent =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36";
const AndroidUserAgent =
  "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36";

function normalizeYoutubeWatchUrl(inputUrl: string): string {
  const watchId = inputUrl.match(/[?&]v=([\w-]{11})/)?.[1];
  const liveId = inputUrl.match(/\/live\/([\w-]{11})/)?.[1];
  const videoId = watchId ?? liveId;
  return videoId ? `https://www.youtube.com/watch?v=${videoId}` : inputUrl;
}

function isPolicyBlock(message: string): boolean {
  const lower = message.toLowerCase();
  return (
    lower.includes("not available on this app") ||
    lower.includes("playability status: error") ||
    lower.includes("account cookies are no longer valid")
  );
}

export class YtDlpProcess implements LiveMediaExtracter {
  constructor(private cookieFilePath: string) {}

  async getStreamUrl(youtubeUrl: string): Promise<string> {
    console.log("call get Stream url");

    if (!/^https?:\/\/(www\.)?youtube\.com\/.+/.test(youtubeUrl)) {
      throw new Error("Invalid Youtube URL");
    }
    await access(this.cookieFilePath).catch(() => {
      throw new Error(`Cookie file not found :${this.cookieFilePath}`);
    });

    const normalizedUrl = normalizeYoutubeWatchUrl(youtubeUrl);
    const tempCookiePath = "/tmp/cookies.txt";
    await fs.copyFile(this.cookieFilePath, tempCookiePath);

    let lastError: unknown;

    for (const client of DefalutClientsOrder) {
      const userAgent =
        client === "android" || client === "android_embedded"
          ? AndroidUserAgent
          : DesktopUserAgent;

      const args = [
        "-g",
        "--cookies",
        tempCookiePath,
        "--extractor-args",
        `youtube:player_client=${client}`,
        "--user-agent",
        userAgent,
        "--add-header",
        "Referer:https://www.youtube.com/",
        "-f",
        "b",
        normalizedUrl,
      ];

      try {
        const { stdout } = await execFileAsync("yt-dlp", args, {
          maxBuffer: 10 * 1024 * 1024,
        });

        const playableUrl = stdout.trim().split("\n").at(-1);
        if (!playableUrl) throw new Error("yt-dlp returned empty URL");
        return playableUrl;
      } catch (err: any) {
        const meaage = `${err?.stderr || err?.message || ""}`;
        if (!isPolicyBlock(meaage)) {
        }
        lastError = err;
        continue;
      }
    }

    throw lastError ?? new Error("Failed to extract playable URL");
  }
}
