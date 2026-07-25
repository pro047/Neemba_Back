// dev 검증용 WS 프로브(가짜 청중): python hub 에 세션을 붙여 active 상태 유지.
// 서버 ping 에 pong 으로 응답해 keepalive 를 통과한다.
// usage (node 컨테이너 안에서, docker 네트워크의 python 호스트명 사용):
//   docker compose -f docker-compose.dev.yml exec node \
//     node scripts/ws_probe_dev.cjs <sessionId=hb1> <durationMs=600000>
const WebSocket = require("ws");
const sessionId = process.argv[2] || "hb1";
const durationMs = Number(process.argv[3] || 600000);
const ws = new WebSocket(`ws://python:8000/ws?sessionId=${sessionId}`);
ws.on("open", () => console.log(`probe: attached ${sessionId}`));
ws.on("message", (data) => {
  const text = data.toString();
  try {
    const msg = JSON.parse(text);
    if (msg.type === "ping") {
      ws.send(JSON.stringify({ type: "pong" }));
      return;
    }
  } catch {}
  console.log("probe: recv:", text.slice(0, 80));
});
ws.on("close", (code) => {
  console.log("probe: closed", code);
  process.exit(0);
});
ws.on("error", (err) => console.log("probe: error", err.message));
setTimeout(() => ws.close(), durationMs);
