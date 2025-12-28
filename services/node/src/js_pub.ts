import {
  connect,
  headers,
  NatsError,
  StringCodec,
  type JetStreamClient,
  type NatsConnection,
} from "nats";
import type {
  PublishEvent,
  TranscriptPublisherPort,
} from "./ports/transcriptPublisher.js";

type ConnectionState = "connected" | "closed";

export class JetStreamTranscriptPublisher implements TranscriptPublisherPort {
  private connection!: NatsConnection;
  private jetStream!: JetStreamClient;
  private state: ConnectionState = "closed";
  private stringCodec = StringCodec();

  constructor(
    private readonly natsUrl: string,
    private readonly streamSubjectPrefix = "transcript.session"
  ) {}

  async start() {
    console.log("publisher start");

    const raw = (this.natsUrl ?? "nats://neemba:nats1234@nats:4222")
      .replace(/^\[|\]$/g, "")
      .replace("@localhost:", "@nats:");

    const url = new URL(raw);
    if (
      url.protocol !== "nats:" &&
      url.protocol !== "tls:" &&
      url.protocol !== "ws:" &&
      url.protocol !== "wss"
    ) {
      throw new Error(`Invalid NATS schema: ${url.protocol}`);
    }

    this.connection = await connect({
      servers: `${url.protocol}//${url.hostname}${url.port ? `:${url.port}` : ""}`,
      user: decodeURIComponent(url.username),
      pass: decodeURIComponent(url.password),
    });
    console.log("connected to:", this.connection.getServer());

    this.jetStream = this.connection.jetstream();
    this.state = "connected";

    this.connection.closed().then((err) => {
      this.state = "closed";
      if (err) {
        console.error("NATS - closed with error:", err.message);
      } else {
        console.log("NATS - connection closed");
      }
    });
  }

  async stop() {
    if (!this.connection) return;
    try {
      await this.connection?.drain();
    } catch (err) {
      console.error("NATS - stop error:", err);
    } finally {
      await this.connection.closed();
      this.state = "closed";
    }
  }

  async publish(message: PublishEvent): Promise<void> {
    if (this.state !== "connected" || this.connection.isClosed()) {
      console.warn("NATS - publish dropped");
      return;
    }

    const subject = `${this.streamSubjectPrefix}.${message.sessionId}`;
    const dedupId = `${message.sessionId}:${message.sequence}`;

    const msgHeaders = headers();
    msgHeaders.set("Nats-Msg-Id", dedupId);
    msgHeaders.set("Content-Type", "application/json");

    const payload = this.stringCodec.encode(JSON.stringify(message));

    try {
      await this.jetStream.publish(subject, payload, { headers: msgHeaders });
      console.log("published :", message.transcriptText);
    } catch (err) {
      const code = (err as NatsError)?.code;
      if (code === "CONNECTION_CLOSED") {
        console.warn("NATS - connection closed during published");
        return;
      }
      throw err;
    }
  }
}
