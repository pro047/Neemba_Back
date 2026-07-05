"""P0 #3 + P1 #7 regression suite for the sentence-separation pipeline.

Reproduces, deterministically and without NATS/DeepL/KSS:
- the flush race: a delta arriving while KSS splits in the worker thread is
  overwritten (lost) when ``_flush`` writes the buffer back,
- silent task death: one splitter/translator/nak exception permanently stops
  ``_flush`` / ``_push_loop`` / the consumer handler with no log and a healthy
  ``/health``.
"""
import asyncio
import threading
from unittest import mock

from src.consumer.consumer import TranscriptConsumer
from src.dto.translationDto import TranslationRequestDto
from src.separator.kss_separator import SentenceSeparator


def simple_split(text: str) -> list[str]:
    """Minimal KSS stand-in: sentences end at a period."""
    out: list[str] = []
    buf = ''
    for ch in text:
        buf += ch
        if ch == '.':
            out.append(buf)
            buf = ''
    if buf.strip():
        out.append(buf)
    return out


class FakePusher:
    def __init__(self) -> None:
        self.pushed: list[tuple[str | None, object]] = []

    async def push_to_client(self, push_text, sequence, *, source_text=None,
                             session_id=None, segment_id=None, source_lang=None,
                             target_lang=None, confidence=None):
        self.pushed.append((source_text, push_text))

    def sources(self) -> list[str | None]:
        return [source for source, _ in self.pushed]


class FakeTranslator:
    def translate(self, source_text, target_language):
        return f'EN::{source_text}'


class FlakyTranslator:
    """Raises on the first call only — models a transient DeepL failure."""

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, source_text, target_language):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError('deepl boom')
        return f'EN::{source_text}'


def make_separator(splitter, translator=None, pusher=None) -> SentenceSeparator:
    # Patch Kss so constructing the separator needs no real KSS model.
    with mock.patch('src.separator.kss_separator.Kss'):
        separator = SentenceSeparator(
            translator or FakeTranslator(), pusher or FakePusher())
    separator.splitter = splitter
    return separator


def dto(text: str, sequence: int) -> TranslationRequestDto:
    return TranslationRequestDto(
        'session-1', 1, sequence, text, 'en-US', 'ko-KR', 0.9)


async def eventually(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_delta_arriving_during_split_is_not_lost():
    entered = threading.Event()
    release = threading.Event()

    def blocking_splitter(text: str) -> list[str]:
        entered.set()
        assert release.wait(timeout=5)
        return simple_split(text)

    pusher = FakePusher()
    separator = make_separator(blocking_splitter, pusher=pusher)
    await separator.start()
    try:
        await separator.offer(dto('안녕하세요.', 1))
        # Wait until _flush is inside the splitter (worker thread).
        assert await asyncio.to_thread(entered.wait, 5)

        # Delta arrives while the split is still running.
        await separator.offer(dto('반갑', 2))
        assert await eventually(
            lambda: '반갑' in separator.state_by_key[('session-1', 1)].buffer)

        release.set()
        assert await eventually(lambda: len(pusher.pushed) >= 1)

        # Completing the sentence must yield "반갑습니다." — if the flush
        # overwrote the buffer, the "반갑" delta is gone and this times out.
        await separator.offer(dto('습니다.', 3))
        assert await eventually(
            lambda: '반갑습니다.' in [s for s in pusher.sources()])
        assert pusher.sources() == ['안녕하세요.', '반갑습니다.']
    finally:
        await separator.stop()


async def test_flush_survives_splitter_exception():
    calls = {'count': 0}

    def flaky_splitter(text: str) -> list[str]:
        calls['count'] += 1
        if calls['count'] == 1:
            raise RuntimeError('kss boom')
        return simple_split(text)

    pusher = FakePusher()
    separator = make_separator(flaky_splitter, pusher=pusher)
    await separator.start()
    try:
        await separator.offer(dto('첫 문장입니다.', 1))
        assert await eventually(lambda: calls['count'] >= 1)

        # The task must still be alive and the failed text retained: the next
        # delta re-flushes the combined buffer.
        await separator.offer(dto('그리고 둘째입니다.', 2))
        assert await eventually(lambda: len(pusher.pushed) >= 2)
        assert pusher.sources() == ['첫 문장입니다.', '그리고 둘째입니다.']
    finally:
        await separator.stop()


async def test_push_loop_survives_translator_exception():
    pusher = FakePusher()
    translator = FlakyTranslator()
    separator = make_separator(simple_split, translator=translator, pusher=pusher)
    await separator.start()
    try:
        await separator.offer(dto('하나입니다.', 1))
        assert await eventually(lambda: translator.calls >= 1)

        # First sentence is dropped (logged), but the loop must keep running.
        await separator.offer(dto('둘입니다.', 2))
        assert await eventually(lambda: len(pusher.pushed) == 1)
        assert pusher.sources() == ['둘입니다.']
    finally:
        await separator.stop()


class DummySeparator:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def offer(self, event) -> None: ...


class FakeMsg:
    def __init__(self, data: bytes, nak_raises: bool = False) -> None:
        self.data = data
        self.acked = False
        self.naked = False
        self._nak_raises = nak_raises

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        if self._nak_raises:
            raise RuntimeError('nak failed: connection lost')
        self.naked = True


def make_consumer() -> TranscriptConsumer:
    return TranscriptConsumer(
        'nats://unused', 'subject', 'stream', 'durable',
        separator=DummySeparator())


async def test_handle_message_naks_poison_message():
    consumer = make_consumer()
    message = FakeMsg(b'not-json')

    await consumer._handle_message(message)

    assert message.naked and not message.acked


async def test_handle_message_survives_nak_failure():
    consumer = make_consumer()
    message = FakeMsg(b'not-json', nak_raises=True)

    # A nak failing mid-outage must not propagate — an escaped exception here
    # kills the consumer task for good (silent, /health stays 200).
    await consumer._handle_message(message)

    assert not message.acked
