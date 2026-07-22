import asyncio
import json
from typing import Protocol

import nats
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import NotFoundError

from src.dto.translationDto import TranslationRequestDto
from src.monitoring import metrics

# Defaults declared in code so a rebuilt NATS behaves the same as production.
# Tune after observing develop: ack_wait must exceed worst-case offer latency,
# max_deliver bounds poison-message redelivery.
DEFAULT_ACK_WAIT_SECONDS = 30
DEFAULT_MAX_DELIVER = 5
# §4-4 hygiene: without max_age the stream grows unbounded whenever the
# consumer is down while Node keeps publishing. 10 minutes is far beyond any
# useful subtitle latency. nats-py takes seconds (converts to ns itself).
DEFAULT_MAX_AGE_SECONDS = 600
# Watermark map safety cap — one int per session, evicted FIFO.
MAX_TRACKED_SESSIONS = 1000


def strip_credentials(url: str) -> str:
    """Return the URL without its user:password@ part, safe for logging.

    rsplit so a password containing '@' cannot partially survive.
    """
    if "@" not in url:
        return url
    proto, rest = url.split("://", 1) if "://" in url else ("nats", url)
    return f"{proto}://{rest.rsplit('@', 1)[1]}"


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
        # Never log self.nats_url directly — it may embed credentials.
        self.safe_url = strip_credentials(nats_url)
        self.client = None
        self.subscription = None
        self.separator = separator
        # Highest sequence successfully offered per session: redeliveries at
        # or below this watermark are duplicates and must not be re-buffered.
        self._last_sequence_by_session: dict[str, int] = {}

    async def connect(self):
        async def on_error(exception):
            print('NATS error : ', repr(exception))

        async def on_disconnect():
            metrics.set_nats_connected(False)
            print("NATS disconnect")

        async def on_reconnect():
            metrics.set_nats_connected(True)
            print("NATS reconnected to", self.safe_url)

        async def on_close():
            metrics.set_nats_connected(False)
            print('NATS connection closed')

        try:
            self.client = await nats.connect(
                self.nats_url,
                error_cb=on_error,
                disconnected_cb=on_disconnect,
                reconnected_cb=on_reconnect,
                closed_cb=on_close
            )
        except Exception as exc:
            print("NATS connect failed to", self.safe_url, "error:", repr(exc))
            raise
        metrics.set_nats_connected(True)
        print('NATS connected to', self.safe_url)

        jetstream = self.client.jetstream()

        await self._ensure_stream_and_consumer(jetstream)

        self.subscription = await jetstream.pull_subscribe(
            subject=self.nats_subject,
            durable=self.consumer_name,
            stream=self.stream_name,
        )

    async def _ensure_stream_and_consumer(self, jetstream) -> None:
        """Declare the stream/consumer idempotently (config-as-code).

        Existing (manually created) production objects are left untouched —
        only missing ones are created, so a rebuilt server reproduces the
        documented behavior instead of nats-py defaults.
        """
        try:
            info = await jetstream.stream_info(self.stream_name)
            # Deliberate exception to the "leave existing objects as-is"
            # rule, limited to this one field: the prod stream predates the
            # max_age hygiene fix, and add_stream-only would leave it
            # unbounded forever. The live config rides along unchanged.
            if info.config.max_age != DEFAULT_MAX_AGE_SECONDS:
                old_max_age = info.config.max_age
                info.config.max_age = DEFAULT_MAX_AGE_SECONDS
                await jetstream.update_stream(info.config)
                print(f'consumer: stream "{self.stream_name}" max_age '
                      f'RECONCILED {old_max_age} -> {DEFAULT_MAX_AGE_SECONDS}s '
                      f'(only max_age was changed)')
            else:
                print(f'consumer: stream "{self.stream_name}" exists, '
                      f'leaving as-is')
        except NotFoundError:
            await jetstream.add_stream(StreamConfig(
                name=self.stream_name,
                subjects=[self.nats_subject],
                max_age=DEFAULT_MAX_AGE_SECONDS,
            ))
            print(f'consumer: stream "{self.stream_name}" created '
                  f'(subjects=[{self.nats_subject}], '
                  f'max_age={DEFAULT_MAX_AGE_SECONDS}s)')

        try:
            await jetstream.consumer_info(self.stream_name, self.consumer_name)
            print(f'consumer: durable "{self.consumer_name}" exists, leaving as-is')
        except NotFoundError:
            await jetstream.add_consumer(self.stream_name, ConsumerConfig(
                durable_name=self.consumer_name,
                ack_wait=DEFAULT_ACK_WAIT_SECONDS,
                max_deliver=DEFAULT_MAX_DELIVER,
            ))
            print(f'consumer: durable "{self.consumer_name}" created '
                  f'(ack_wait={DEFAULT_ACK_WAIT_SECONDS}s, '
                  f'max_deliver={DEFAULT_MAX_DELIVER})')

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
            req = self._parse_request(message.data)
        except Exception as exc:
            # An unparseable message can never succeed — nak would redeliver
            # it forever (burning fetch slots), so terminate it instead.
            metrics.record_unparseable()
            print('consumer: unparseable message, term:', repr(exc))
            try:
                await message.term()
            except Exception as term_exc:
                print('consumer: term failed (ignored):', repr(term_exc))
            return

        try:
            if self._is_duplicate(req):
                # Already buffered on a previous delivery (e.g. ack_wait
                # expired before the ack landed): ack to stop redelivery,
                # never re-buffer.
                print(f'consumer: duplicate dropped '
                      f'(session={req.session_id} seq={req.sequence})')
                await message.ack()
                return
            async with self.worker_semaphore:
                await self.separator.offer(req)
            # Advance the watermark only after the offer succeeded, so a
            # failed attempt is not mistaken for a duplicate on redelivery.
            self._record_sequence(req)
            await message.ack()
        except Exception as exc:
            print('consumer: message handling failed, nak:', repr(exc))
            try:
                await message.nak()
            except Exception as nak_exc:
                # A nak can itself fail during a NATS outage; swallow it so
                # the consumer task never dies — the broker redelivers after
                # ack_wait anyway.
                print('consumer: nak failed (ignored):', repr(nak_exc))

    def _is_duplicate(self, req: TranslationRequestDto) -> bool:
        last = self._last_sequence_by_session.get(req.session_id)
        return last is not None and req.sequence <= last

    def _record_sequence(self, req: TranslationRequestDto) -> None:
        self._last_sequence_by_session[req.session_id] = req.sequence
        while len(self._last_sequence_by_session) > MAX_TRACKED_SESSIONS:
            oldest = next(iter(self._last_sequence_by_session))
            del self._last_sequence_by_session[oldest]

    async def run(self):
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

            tasks = [asyncio.create_task(self._handle_message(msg))
                     for msg in msgs]
            if tasks:
                # return_exceptions keeps one failed handler from killing the
                # whole consume loop (it would stop silently otherwise).
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        print('consumer: handler error (ignored):',
                              repr(result))

    async def close(self):
        if self.client:
            await self.client.drain()
