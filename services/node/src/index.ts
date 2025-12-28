import express from "express";
import { collectDefaultMetrics, Counter, Registry } from "prom-client";
import rtmp from "./router/rtmp.js";
import mic from "./router/mic.js";
import http from "http";
import { WebSocketServer } from "ws";
import { v4 as uuidv4 } from "uuid";

const app = express();

app.use(express.json());

app.use(
  (
    err: any,
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
const ws = new WebSocketServer({ server: server, path: "/api/mic" });

ws.on("connection", (socket) => {
  console.log("connection");

  socket.on("message", async (m, isBinary) => {
    const text = isBinary ? m.toString("utf-8") : m.toString();
    console.log(text);
  });
});

app.get("/health", (req, res) => res.json({ ok: true }));

app.get("/metrics", async (req, res) => {
  res.setHeader("Content-Type", registry.contentType);
  res.end(await registry.metrics());
});

app.get("/api/ping", (req, res) => {
  requestCounter.inc();
  res.json({ message: "pong" });
});

const port = 3000;
server.listen(port, "0.0.0.0", () => {
  console.log(`Node server connected port : ${port}`);
});
