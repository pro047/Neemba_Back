// §4-4 live repro: prove the retry buffer survives a real NATS outage.
//
// Run inside the node dev container (real nats.js against the real broker):
//   docker exec node npx tsx scripts/repro_publish_retry.ts
// While it publishes, stop the NATS container for ~18s and start it again:
//   sleep 8 && docker stop neemba-nats-1 && sleep 18 && docker start neemba-nats-1
//
// PASS criteria:
//   - every enqueued sequence reaches the broker (stream message count == TOTAL)
//   - "publish buffer: publish failed, retrying" appears during the outage
//   - final droppedCount == 0 and no "NATS - publish dropped" line
import { JetStreamTranscriptPublisher } from "../src/js_pub.js";
import { RetryingTranscriptPublisher } from "../src/retryingPublisher.js";

const TOTAL = 25;
const INTERVAL_MS = 1000;
const DRAIN_GRACE_MS = 25_000;

const natsUrl = process.env.NATS_URL;
if (!natsUrl) throw new Error("NATS_URL missing");

const sessionId = `repro-buffer-${process.pid}`;
const inner = new JetStreamTranscriptPublisher(natsUrl);
const publisher = new RetryingTranscriptPublisher(inner);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

await publisher.start();
console.log(`repro: start session=${sessionId} total=${TOTAL}`);

for (let seq = 1; seq <= TOTAL; seq++) {
  await publisher.publish({
    sessionId,
    segmentId: 1,
    sequence: seq,
    transcriptText: `재현 문장 ${seq}입니다.`,
    sourceLanguage: "ko-KR",
    targetLanguage: "en-US",
    sampleRateHz: 16000,
    createdAt: new Date().toISOString(),
  });
  console.log(`repro: enqueued seq=${seq} at ${new Date().toISOString()}`);
  await sleep(INTERVAL_MS);
}

console.log(`repro: all enqueued, waiting ${DRAIN_GRACE_MS}ms for drain`);
await sleep(DRAIN_GRACE_MS);
console.log(`repro: droppedCount=${publisher.droppedCount}`);
await publisher.stop();
console.log("repro: done");
process.exit(0);
