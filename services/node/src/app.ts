import express, { type Router } from "express";
import http from "http";
import { collectDefaultMetrics, Counter, Registry } from "prom-client";
import micRouter from "./router/mic.js";
import rtmpRouter from "./router/rtmp.js";
import { createMicWebSocketServer } from "./micWebSocket.js";
import {
  micRuntimeStore,
  type SessionRuntimeStore,
} from "./sessionRuntimeStore.js";

type CreateAppDependencies = {
  mic?: Router;
  rtmp?: Router;
  runtimeStore?: SessionRuntimeStore;
};

export function createApp({
  mic = micRouter,
  rtmp = rtmpRouter,
  runtimeStore = micRuntimeStore,
}: CreateAppDependencies = {}) {
  const app = express();

  app.use(express.json());

  app.use(
    (
      err: Error,
      _req: express.Request,
      res: express.Response,
      _next: express.NextFunction
    ) => {
      console.error("unhandled", err);
      if (res.headersSent) return;
      res.status(500).json({ error: "unhandled", message: String(err) });
    }
  );

  const registry = new Registry();
  collectDefaultMetrics({ register: registry });
  const requestCounter = new Counter({
    name: "demo_request_total",
    help: "Total number of demo requests",
  });
  registry.registerMetric(requestCounter);

  app.use("/api", rtmp);
  app.use("/api", mic);

  const server = http.createServer(app);
  const ws = createMicWebSocketServer({ server, runtimeStore });

  app.get("/health", (_req, res) => res.json({ ok: true }));

  app.get("/metrics", async (_req, res) => {
    res.setHeader("Content-Type", registry.contentType);
    res.end(await registry.metrics());
  });

  app.get("/api/ping", (_req, res) => {
    requestCounter.inc();
    res.json({ message: "pong" });
  });

  return { app, server, ws };
}
