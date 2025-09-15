import dotenv from "dotenv";
dotenv.config();

export const natsUrl =
  process.env.NATS_URL || "nats://neemba:nats1234@nats:4222";
export const natsToken = process.env.NATS_TOKEN || "";
export const natsHost = process.env.NATS_HOST || "nats";
export const natsPort = process.env.NATS_PORT || "4222";
export const natsUser = process.env.NATS_USER || "neemba";
export const natsPassword = process.env.NATS_PASSWORD || "nats1234";
export const youtubeUrl = process.env.YOUTUBE_URL;
export const cookieFilePath =
  process.env.COOKIE_FILE_PATH || "/var/lib/neemba/secrets/cookies.txt";
export const pythonHost = process.env.PYTHON_HOST || "http://python:8000";
export const wsUrl = process.env.WS_URL || "ws://localhost:8000";

export const postgres_host = process.env.POSTGRES_HOST || "127.0.0.1";
export const postgres_port = process.env.POSTGRES_PORT || "5432";
export const postgres_user = process.env.POSTGRES_USER || "neemba";
export const postgres_password = process.env.POSTGRES_PASSWORD || "1234";
export const postgres_database = process.env.POSTGRES_DATABASE || "neemba";
