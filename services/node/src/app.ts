import express, { type Router } from "express";
import http from "http";
import {
  collectDefaultMetrics,
  Counter,
  register as domainRegister,
  Registry,
} from "prom-client";
import micRouter from "./router/mic.js";
import rtmpRouter from "./router/rtmp.js";
import { createMicWebSocketServer } from "./micWebSocket.js";
import { setRtmpAuthEnabled } from "./monitoring/metrics.js";
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
  // Until now the only writer was the on_publish handler, so between a deploy
  // and the first publish the gauge sat at prom-client's default 0 and the
  // monitor's daily tick reported "RTMP auth off" — a false alarm that
  // recurred on every single deploy. Seeding it here makes the metric describe
  // the config rather than the traffic.
  //
  // Safe to read process.env at this point: importing rtmpRouter above pulls
  // in config.js, whose dotenv.config() runs during module evaluation, before
  // this function body can be called.
  setRtmpAuthEnabled(Boolean(process.env.RTMP_PUBLISH_KEY));

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
    // Domain metrics (src/monitoring/metrics.ts) live on prom-client's
    // default register so instrumented modules never need this app instance;
    // merge exposes both without double-registration.
    const merged = Registry.merge([registry, domainRegister]);
    res.setHeader("Content-Type", merged.contentType);
    res.end(await merged.metrics());
  });

  app.get("/api/ping", (_req, res) => {
    requestCounter.inc();
    res.json({ message: "pong" });
  });

  return { app, server, ws };
}
