import { once } from "node:events";
import type { Readable } from "node:stream";
import { describe, expect, it } from "vitest";
import type {
  AudioConsumerContext,
  AudioConsumerPort,
} from "../src/ports/audioConsumerPort.js";
import { runMicPipeline } from "../src/runMicPipeline.js";

class FakeConsumer implements AudioConsumerPort {
  public readonly chunks: Buffer[] = [];
  public stopped = false;
  public readableEnded = false;
  public context: AudioConsumerContext | undefined;

  async start(
    readable: Readable,
    context?: AudioConsumerContext
  ): Promise<() => Promise<void>> {
    this.context = context;
    (async () => {
      for await (const chunk of readable as AsyncIterable<Buffer>) {
        this.chunks.push(Buffer.from(chunk));
      }
      this.readableEnded = true;
    })().catch(() => undefined);

    return async () => {
      this.stopped = true;
    };
  }
}

describe("runMicPipeline", () => {
  it("returns inputWritable and stop()", async () => {
    const runtime = await runMicPipeline({ consumer: new FakeConsumer() });

    expect(runtime.inputWritable).toBeDefined();
    expect(runtime.stop).toBeTypeOf("function");
  });

  it("forwards chunks written to inputWritable to the consumer", async () => {
    const consumer = new FakeConsumer();
    const runtime = await runMicPipeline({ consumer });
    const chunk = Buffer.from("pcm-data");

    runtime.inputWritable.write(chunk);
    runtime.inputWritable.end();
    await once(runtime.inputWritable, "finish");
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(consumer.chunks).toEqual([chunk]);
  });

  it("stop() shuts down cleanly", async () => {
    const consumer = new FakeConsumer();
    const runtime = await runMicPipeline({ consumer });

    await runtime.stop();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(consumer.stopped).toBe(true);
    expect(consumer.readableEnded).toBe(true);
  });

  it("passes the explicit sessionId to the consumer", async () => {
    const consumer = new FakeConsumer();

    await runMicPipeline({ consumer, sessionId: "session-1" });

    expect(consumer.context).toEqual({ sessionId: "session-1" });
  });
});
