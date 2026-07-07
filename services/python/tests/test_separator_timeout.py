"""Timeout flush + session-stop cleanup for the sentence separator.

Without a timeout, an unfinished sentence (no closing ending/punctuation)
waits in the buffer indefinitely — the speaker's last words of a pause never
reach the app. And a stopped session's buffers linger in ``state_by_key``
forever (audit M4).
"""
from unittest import mock

from src.separator.kss_separator import SentenceSeparator
from tests.test_separator_pipeline import (
    FakePusher,
    FakeTranslator,
    eventually,
    simple_split,
)
from tests.test_separator_segments import dto


def make_separator(timeout: float, pusher: FakePusher) -> SentenceSeparator:
    with mock.patch('src.separator.kss_separator.Kss'):
        separator = SentenceSeparator(
            FakeTranslator(), pusher, flush_timeout_seconds=timeout)
    separator.splitter = simple_split
    return separator


async def test_unfinished_buffer_flushes_after_timeout():
    pusher = FakePusher()
    separator = make_separator(timeout=0.15, pusher=pusher)
    await separator.start()
    try:
        # No closing ending and no punctuation: without the timeout this
        # would sit in the buffer forever.
        await separator.offer(dto('타임아웃 미완성 문장', segment_id=1, sequence=1))

        assert await eventually(lambda: '타임아웃 미완성 문장' in pusher.sources())
        # The segment stays usable for future deltas (key kept, buffer empty).
        state = separator.state_by_key.get(('session-1', 1))
        assert state is not None and state.buffer.strip() == ''
    finally:
        await separator.stop()


async def test_timeout_flush_repeats_for_subsequent_text():
    pusher = FakePusher()
    separator = make_separator(timeout=0.15, pusher=pusher)
    await separator.start()
    try:
        await separator.offer(dto('첫 미완성', segment_id=1, sequence=1))
        assert await eventually(lambda: '첫 미완성' in pusher.sources())

        # The one-shot force_closed mark must not leak into normal flow:
        # a later unfinished delta gets its own timeout flush.
        await separator.offer(dto('둘째 미완성', segment_id=1, sequence=2))
        assert await eventually(lambda: '둘째 미완성' in pusher.sources())
        assert pusher.sources() == ['첫 미완성', '둘째 미완성']
    finally:
        await separator.stop()


async def test_closed_sentences_still_flush_immediately():
    pusher = FakePusher()
    # Long timeout: an immediate flush proves the normal path does not wait.
    separator = make_separator(timeout=5.0, pusher=pusher)
    await separator.start()
    try:
        await separator.offer(dto('바로 나갑니다.', segment_id=1, sequence=1))
        assert await eventually(lambda: '바로 나갑니다.' in pusher.sources())
    finally:
        await separator.stop()


async def test_close_session_flushes_and_removes_only_that_session():
    pusher = FakePusher()
    separator = make_separator(timeout=5.0, pusher=pusher)
    await separator.start()
    try:
        await separator.offer(
            dto('종료 세션 미완성', segment_id=1, sequence=1, session_id='session-stop'))
        await separator.offer(
            dto('생존 세션 미완성', segment_id=1, sequence=1, session_id='session-live'))
        assert await eventually(
            lambda: ('session-stop', 1) in separator.state_by_key
            and ('session-live', 1) in separator.state_by_key)

        await separator.close_session('session-stop')

        assert await eventually(lambda: '종료 세션 미완성' in pusher.sources())
        assert ('session-stop', 1) not in separator.state_by_key
        # The other session is untouched: key intact, nothing pushed.
        assert ('session-live', 1) in separator.state_by_key
        assert '생존 세션 미완성' not in pusher.sources()
    finally:
        await separator.stop()
