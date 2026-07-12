#!/usr/bin/env node
// Runtime verification harness — replaces the mobile app for server checks.
// Streams raw 16kHz mono s16le PCM to the mic WebSocket at real-time pace
// while printing everything the result WebSocket delivers.
//
// Usage:
//   node run-scenario.mjs <basic|long|ghost|stop-tail> [options]
// Options:
//   --api <url>     Node API base           (default http://localhost:3000)
//   --ws-url <url>  result WS base override (default: webSocketUrl from /mic/start,
//                   fallback ws://localhost:8080/ws via dev nginx)
//   --audio <file>  PCM file override (default: audio/short.pcm, long→audio/long.pcm)
//   --dry-run       parse/pace the audio locally without any server
//
// Requires Node >= 22 (built-in WebSocket / fetch).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRAME_BYTES = 3200; // 100ms of 16kHz mono s16le
const FRAME_MS = 100;

const args = process.argv.slice(2);
const scenario = args[0];
const opt = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : fallback;
};
const has = (name) => args.includes(name);

const SCENARIOS = {
  basic: {
    audio: "audio/short.pcm",
    stop: "after-tail", // wait for timeout flush, then /mic/stop
    note: [
      "확인 1 (첫 발화): 첫 번째 문장의 번역이 [RECV]로 도착해야 한다",
      "확인 2 (타임아웃 flush): 마지막 미완성 조각이 발화 종료 ~2초 후 도착해야 한다",
      "확인 3 (JetStream): docker logs python | grep 'consumer:' → stream/durable exists|created",
    ],
  },
  long: {
    audio: "audio/long.pcm",
    stop: "after-tail",
    note: [
      "확인 (rotation): 285초 경계(로그의 진행률 ~285s 부근) 전후 문장이 오염 없이 이어져야 한다",
      "         잘린 단어 1~2개는 알려진 한계(VAD 백로그), '조각 뒤섞임·중복'이 없어야 함",
    ],
  },
  ghost: {
    audio: "audio/short.pcm",
    stop: "never", // abruptly kill the socket, no /mic/stop
    killAfterMs: 10_000,
    note: [
      "확인 (유령 세션): 소켓 강제 종료 ~10초 후 node 로그에",
      "  docker logs node --since 2m | grep 'tearing down ghost session'",
      "  이후 python 세션도 ended 처리(모니터에서 live 아님) 확인",
    ],
  },
  "stop-tail": {
    audio: "audio/short.pcm",
    stop: "immediate", // stop mid-utterance, tail must still be captured
    stopAfterMs: 6_000,
    note: [
      "확인 (stop 잔여 문장): 발화 도중 stop — 잔여 조각이 DB/모니터에 기록돼야 한다",
      "  curl -s localhost:8080/api/monitor/sessions | tail  (dev nginx 경유)",
    ],
  },
};

if (!SCENARIOS[scenario]) {
  console.error(`usage: node run-scenario.mjs <${Object.keys(SCENARIOS).join("|")}> [--api url] [--ws-url url] [--audio file] [--dry-run]`);
  process.exit(1);
}

const cfg = SCENARIOS[scenario];
const api = opt("--api", "http://localhost:3000");
const audioPath = opt("--audio", join(HERE, cfg.audio));
const pcm = readFileSync(audioPath);
const totalSec = pcm.length / 32000;
const t0 = Date.now();
const ts = () => `+${((Date.now() - t0) / 1000).toFixed(1)}s`;

console.log(`scenario=${scenario} audio=${audioPath} (${totalSec.toFixed(0)}s)`);

if (has("--dry-run")) {
  console.log(`dry-run: ${Math.ceil(pcm.length / FRAME_BYTES)} frames x ${FRAME_MS}ms — parsing OK`);
  process.exit(0);
}

// 1) start the session
const startRes = await fetch(`${api}/api/mic/start`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ sourceLang: "ko-KR", targetLang: "en-US" }),
});
if (!startRes.ok && startRes.status !== 202) {
  console.error(`mic/start failed: ${startRes.status} ${await startRes.text()}`);
  process.exit(1);
}
const { sessionId, webSocketUrl } = await startRes.json();
console.log(`${ts()} session started: ${sessionId}`);

// 2) subscribe to translation results
const wsBase = opt("--ws-url", webSocketUrl || "ws://localhost:8080/ws");
const resultUrl = wsBase.includes("sessionId=")
  ? wsBase
  : `${wsBase}${wsBase.includes("?") ? "&" : "?"}sessionId=${sessionId}`;
const resultWs = new WebSocket(resultUrl);
let received = 0;
resultWs.addEventListener("open", () => console.log(`${ts()} result WS connected: ${resultUrl}`));
resultWs.addEventListener("message", (e) => {
  // The hub keepalive expects an application-level pong within 60s of the
  // first ping, otherwise it closes the socket and buffers for reconnect —
  // the real app replies automatically, so the harness must too.
  try {
    const msg = JSON.parse(e.data);
    if (msg?.type === "ping") {
      resultWs.send(JSON.stringify({ type: "pong" }));
      console.log(`${ts()} [PING] replied pong`);
      return;
    }
  } catch {
    // not JSON — plain translation text, fall through
  }
  received += 1;
  console.log(`${ts()} [RECV #${received}] ${e.data}`);
});
resultWs.addEventListener("close", (e) =>
  console.error(`${ts()} result WS closed by server (code=${e.code}) — translations after this point are lost`)
);
resultWs.addEventListener("error", () => console.error(`${ts()} result WS error (check --ws-url)`));

// 3) stream PCM at real-time pace
const micWs = new WebSocket(`${api.replace(/^http/, "ws")}/api/mic?sessionId=${sessionId}`);
await new Promise((resolve, reject) => {
  micWs.addEventListener("open", resolve);
  micWs.addEventListener("error", () => reject(new Error("mic WS connect failed")));
});
console.log(`${ts()} mic WS connected, streaming...`);

let offset = 0;
const streamStart = Date.now();
await new Promise((resolve) => {
  const tick = setInterval(() => {
    // drift-corrected: send every frame that is due by wall clock
    const due = Math.min(
      pcm.length,
      (Math.floor((Date.now() - streamStart) / FRAME_MS) + 1) * FRAME_BYTES
    );
    while (offset < due) {
      micWs.send(pcm.subarray(offset, Math.min(offset + FRAME_BYTES, pcm.length)));
      offset += FRAME_BYTES;
    }
    const sentSec = offset / 32000;
    if (Math.floor(sentSec) % 30 === 0 && sentSec >= 30 && offset % (32000 * 30) < FRAME_BYTES) {
      console.log(`${ts()} [SEND] ${sentSec.toFixed(0)}s / ${totalSec.toFixed(0)}s`);
    }
    if (cfg.killAfterMs && Date.now() - streamStart >= cfg.killAfterMs) {
      clearInterval(tick);
      return resolve("killed");
    }
    if (cfg.stopAfterMs && Date.now() - streamStart >= cfg.stopAfterMs) {
      clearInterval(tick);
      return resolve("early-stop");
    }
    if (offset >= pcm.length) {
      clearInterval(tick);
      return resolve("done");
    }
  }, FRAME_MS / 2);
});

// 4) scenario-specific ending
if (cfg.stop === "never") {
  console.log(`${ts()} killing mic WS abruptly (no /mic/stop) — ghost teardown due in ~10s`);
  micWs.close();
} else {
  if (cfg.stop === "after-tail") {
    console.log(`${ts()} audio done — waiting 8s for timeout-flushed tail...`);
    await new Promise((r) => setTimeout(r, 8_000));
  }
  console.log(`${ts()} calling /mic/stop`);
  const stopRes = await fetch(`${api}/api/mic/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId }),
  });
  console.log(`${ts()} stop → ${stopRes.status}`);
  await new Promise((r) => setTimeout(r, 3_000));
}

console.log(`\n=== summary: ${received} translation(s) received ===`);
for (const line of cfg.note) console.log(`  ${line}`);
resultWs.close();
process.exit(0);
