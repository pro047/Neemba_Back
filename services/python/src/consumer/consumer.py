import asyncio
import json
import logging
from typing import Protocol

import nats
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError

from src.dto.translationDto import TranslationRequestDto


class Separator(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def offer(self, event: TranslationRequestDto) -> None: ...


class TranscriptConsumer:
    def __init__(self, nats_url: str, nats_subject: str, stream_name: str, consumer_name: str, separator: Separator, worker_concurrency: int = 5,):
        self.nats_url = nats_url
        self.nats_subject = nats_subject
        self.stream_name = stream_name
        self.consumer_name = consumer_name
        self.worker_concurrency = worker_concurrency
        self.worker_semaphore = asyncio.Semaphore(worker_concurrency)
        self.client = None
        self.subscription = None
        self.separator = separator

    async def connect(self):
        async def on_error(exception):
            print('NATS error : ', repr(exception))

        async def on_disconnect():
            print("NATS disconnect")

        async def on_reconnect():
            print("NATS reconnected to", self.nats_url)

        async def on_close():
            print('NATS connection closed')

        safe_url = self.nats_url
        if "@" in safe_url:
            proto, rest = safe_url.split(
                "://", 1) if "://" in safe_url else ("nats", safe_url)
            safe_url = f"{proto}://{rest.split('@', 1)[1]}"
        try:
            self.client = await nats.connect(
                self.nats_url,
                error_cb=on_error,
                disconnected_cb=on_disconnect,
                reconnected_cb=on_reconnect,
                closed_cb=on_close
            )
        except Exception as exc:
            print("NATS connect failed to", safe_url, "error:", repr(exc))
            raise
        print('NATS connected to', safe_url)

        jetstream = self.client.jetstream()

        self.subscription = await jetstream.pull_subscribe(
            subject=self.nats_subject,
            durable=self.consumer_name,
            stream=self.stream_name,
        )
        print(
            "NATS subscription ready:",
            self.nats_subject,
            self.stream_name,
            self.consumer_name,
        )

    def _parse_request(self, raw: bytes) -> TranslationRequestDto:
        data = json.loads(raw.decode('utf-8'))
        return TranslationRequestDto(
            data["sessionId"],
            data["segmentId"],
            data["sequence"],
            data["transcriptText"],
            data["targetLanguage"],
            data.get("sourceLanguage"),
        )

    async def _handle_message(self, message: Msg) -> None:
        try:
            print("NATS raw subject:", message.subject)
            req = self._parse_request(message.data)
            print(
                "NATS received:",
                req.session_id,
                req.sequence,
                req.source_text,
            )
            async with self.worker_semaphore:
                # print(
                #     f"req: {req} / seg : {req.segment_id} - req_text : {req.source_text}")
                await self.separator.offer(req)
            await message.ack()
        except Exception as exc:
            print("NATS handle message error:", repr(exc))
            await message.nak()

    async def run(self):
        print("consumer run")
        if not self.subscription:
            raise RuntimeError(
                "Subscription not initialized")

        while True:
            try:
                msgs = await self.subscription.fetch(
                    batch=self.worker_concurrency,
                    timeout=5,
                )
            except (TimeoutError, NatsTimeoutError):
                continue

            if msgs:
                print("NATS fetched:", len(msgs))
            tasks = [asyncio.create_task(self._handle_message(msg))
                     for msg in msgs]
            if tasks:
                await asyncio.gather(*tasks)

    async def close(self):
        if self.client:
            await self.client.drain()
