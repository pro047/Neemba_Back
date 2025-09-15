import express from "express";
import { collectDefaultMetrics, Counter, Registry } from "prom-client";
import sessions from "./sessions/sessions";

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

app.use("/api", sessions);

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
app.listen(port, "0.0.0.0", () => {
  console.log(`Node server connected port : ${port}`);
});
