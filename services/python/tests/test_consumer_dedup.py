"""P0 #6: sequence-based idempotent dedup + JetStream config-as-code + term.

NATS redelivery (ack_wait exceeded, nak, restart) currently re-appends the
same delta into the separator buffer → duplicated, garbled sentences. The
consumer must (A) drop already-seen sequences per session, (②) declare the
stream/consumer config idempotently at startup so behavior is reproducible,
and (③) ``term()`` unparseable messages instead of nak-ing them forever.
"""
import json

from nats.js.errors import NotFoundError

from src.consumer.consumer import TranscriptConsumer


def payload(sequence: int, session_id: str = 'session-1', text: str = '텍스트') -> bytes:
    return json.dumps({
        'sessionId': session_id,
        'segmentId': 1,
        'sequence': sequence,
        'transcriptText': text,
        'targetLanguage': 'en-US',
        'sourceLanguage': 'ko-KR',
    }).encode('utf-8')


class RecordingSeparator:
    def __init__(self, fail_first: bool = False) -> None:
        self.offers: list[tuple[str, int]] = []
        self._fail_first = fail_first

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def offer(self, event) -> None:
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError('offer boom')
        self.offers.append((event.session_id, event.sequence))


class FakeMsg:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acked = False
        self.naked = False
        self.termed = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.naked = True

    async def term(self) -> None:
        self.termed = True


def make_consumer(separator=None) -> TranscriptConsumer:
    return TranscriptConsumer(
        'nats://unused', 'transcript.session.*', 'transcripts', 'durable',
        separator=separator or RecordingSeparator())


async def test_redelivered_sequence_is_acked_but_not_reoffered():
    separator = RecordingSeparator()
    consumer = make_consumer(separator)

    first = FakeMsg(payload(1))
    redelivered = FakeMsg(payload(1))
    await consumer._handle_message(first)
    await consumer._handle_message(redelivered)

    # The duplicate is acknowledged (stop redelivery) but never re-buffered.
    assert separator.offers == [('session-1', 1)]
    assert first.acked and redelivered.acked


async def test_stale_sequence_arriving_late_is_dropped():
    separator = RecordingSeparator()
    consumer = make_consumer(separator)

    await consumer._handle_message(FakeMsg(payload(1)))
    await consumer._handle_message(FakeMsg(payload(2)))
    stale = FakeMsg(payload(1))
    await consumer._handle_message(stale)

    assert separator.offers == [('session-1', 1), ('session-1', 2)]
    assert stale.acked


async def test_sequences_are_tracked_per_session():
    separator = RecordingSeparator()
    consumer = make_consumer(separator)

    await consumer._handle_message(FakeMsg(payload(1, session_id='session-A')))
    await consumer._handle_message(FakeMsg(payload(1, session_id='session-B')))

    assert separator.offers == [('session-A', 1), ('session-B', 1)]


async def test_failed_offer_does_not_advance_the_watermark():
    separator = RecordingSeparator(fail_first=True)
    consumer = make_consumer(separator)

    failed = FakeMsg(payload(1))
    await consumer._handle_message(failed)
    assert failed.naked and not failed.acked

    # Redelivery of the SAME sequence must still be offered — the first
    # attempt never reached the buffer, so it is not a duplicate.
    redelivered = FakeMsg(payload(1))
    await consumer._handle_message(redelivered)

    assert separator.offers == [('session-1', 1)]
    assert redelivered.acked


async def test_unparseable_message_is_terminated_not_naked():
    consumer = make_consumer()
    poison = FakeMsg(b'not-json')

    await consumer._handle_message(poison)

    # nak would redeliver a message that can never succeed — term removes it.
    assert poison.termed
    assert not poison.naked and not poison.acked


class FakeStreamInfo:
    def __init__(self, config) -> None:
        self.config = config


class FakeJetStream:
    def __init__(self, stream_exists: bool, consumer_exists: bool,
                 existing_max_age: float | None = 600) -> None:
        self._stream_exists = stream_exists
        self._consumer_exists = consumer_exists
        self._existing_max_age = existing_max_age
        self.added_streams: list = []
        self.added_consumers: list = []
        self.updated_streams: list = []

    async def stream_info(self, name: str):
        if not self._stream_exists:
            raise NotFoundError
        from nats.js.api import StreamConfig
        return FakeStreamInfo(StreamConfig(
            name=name,
            subjects=['transcript.session.*'],
            max_age=self._existing_max_age,
        ))

    async def add_stream(self, config) -> None:
        self.added_streams.append(config)

    async def update_stream(self, config) -> None:
        self.updated_streams.append(config)

    async def consumer_info(self, stream: str, consumer: str):
        if not self._consumer_exists:
            raise NotFoundError
        return object()

    async def add_consumer(self, stream: str, config) -> None:
        self.added_consumers.append((stream, config))


async def test_ensure_creates_missing_stream_and_consumer():
    consumer = make_consumer()
    jetstream = FakeJetStream(stream_exists=False, consumer_exists=False)

    await consumer._ensure_stream_and_consumer(jetstream)

    assert len(jetstream.added_streams) == 1
    assert jetstream.added_streams[0].name == 'transcripts'
    assert jetstream.added_streams[0].subjects == ['transcript.session.*']
    # §4-4 hygiene: without max_age the stream grows unbounded whenever the
    # consumer is down while Node keeps publishing.
    assert jetstream.added_streams[0].max_age == 600
    assert len(jetstream.added_consumers) == 1
    stream_name, config = jetstream.added_consumers[0]
    assert stream_name == 'transcripts'
    assert config.durable_name == 'durable'
    assert config.ack_wait == 30
    assert config.max_deliver == 5


async def test_ensure_skips_existing_stream_and_consumer():
    consumer = make_consumer()
    jetstream = FakeJetStream(stream_exists=True, consumer_exists=True)

    # Existing objects whose config already matches must be left untouched.
    await consumer._ensure_stream_and_consumer(jetstream)

    assert jetstream.added_streams == []
    assert jetstream.updated_streams == []
    assert jetstream.added_consumers == []


async def test_ensure_reconciles_missing_max_age_on_existing_stream():
    """max_age is the one field code is allowed to reconcile on live streams.

    The prod stream predates the max_age hygiene fix; add_stream-only would
    leave it unbounded forever. Only max_age may be touched — everything
    else on a manually created stream stays as-is.
    """
    consumer = make_consumer()
    jetstream = FakeJetStream(stream_exists=True, consumer_exists=True,
                              existing_max_age=None)

    await consumer._ensure_stream_and_consumer(jetstream)

    assert jetstream.added_streams == []
    assert len(jetstream.updated_streams) == 1
    updated = jetstream.updated_streams[0]
    assert updated.max_age == 600
    # The rest of the live config must ride along unchanged.
    assert updated.name == 'transcripts'
    assert updated.subjects == ['transcript.session.*']
    assert jetstream.added_consumers == []
