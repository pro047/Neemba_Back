import dotenv from "dotenv";
dotenv.config();

// No credential-bearing fallback: a missing NATS_URL must fail fast at
// connect time instead of silently using a known-leaked default.
export const natsUrl = process.env.NATS_URL || "";
export const youtubeUrl = process.env.YOUTUBE_URL;
export const cookieFilePath =
  process.env.COOKIE_FILE_PATH || "/var/lib/neemba/secrets/cookies.txt";
export const pythonHost = process.env.PYTHON_HOST || "http://python:8000";
export const wsUrl = process.env.WS_URL || "ws://localhost:8000";

// The RTMP stream node consumes. It lives here rather than next to its two
// readers because both of them must agree: FfmpegTranscoder pulls this URL, and
// the on_publish hook derives the one allowed stream name from it. A second
// source of truth would let the hook admit a publisher whose stream ffmpeg
// never reads. Both read process.env.RTMP_PULL_URL at call time, not here, so
// container env and tests can override it.
export const DEFAULT_RTMP_PULL_URL = "rtmp://neemba.app:1935/live/translation";

export const postgres_host = process.env.POSTGRES_HOST || "127.0.0.1";
export const postgres_port = process.env.POSTGRES_PORT || "5432";
export const postgres_user = process.env.POSTGRES_USER || "neemba";
export const postgres_password = process.env.POSTGRES_PASSWORD || "";
export const postgres_database = process.env.POSTGRES_DATABASE || "neemba";
