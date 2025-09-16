from src.consumer.consumer import TranscriptConsumer
from src.separator.kss_separator import SentenceSeparator
from src.ws.websocket import WebSocketHub


async def build(hub: WebSocketHub, separator: SentenceSeparator, nats_config: dict[str, str]) -> None:
    print(
        f'start build / nats url : {nats_config["nats_url"]} / nats subject : {nats_config["nats_subject"]} / nats stream : {nats_config["nats_stream_name"]} / nats consumer : {nats_config["nats_consumer_name"]}')

    consumer = TranscriptConsumer(
        nats_url=nats_config["nats_url"],
        nats_subject=nats_config["nats_subject"],
        stream_name=nats_config["nats_stream_name"],
        consumer_name=nats_config["nats_consumer_name"],
        separator=separator,
        worker_concurrency=5,
    )

    await consumer.connect()
    try:
        await consumer.run()
    finally:
        await consumer.close()
