"""P0 #2 (Python side): segment rotation must not orphan buffered text.

When a new segment for the same session appears (285s stream rotation), the
old segment's stream is gone for good — its buffered, unfinished sentence must
be force-flushed instead of sitting in ``state_by_key`` forever (text loss +
memory leak).
"""
from src.dto.translationDto import TranslationRequestDto
from tests.test_separator_pipeline import (
    FakePusher,
    eventually,
    make_separator,
    simple_split,
)


def dto(text: str, segment_id: int, sequence: int,
        session_id: str = 'session-1') -> TranslationRequestDto:
    return TranslationRequestDto(
        session_id, segment_id, sequence, text, 'en-US', 'ko-KR', 0.9)


async def test_new_segment_flushes_previous_segment_buffer():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        # Unfinished sentence: stays buffered under (session-1, segment 1).
        await separator.offer(dto('미완성 문장', segment_id=1, sequence=1))
        assert await eventually(
            lambda: separator.state_by_key.get(('session-1', 1)) is not None
            and separator.state_by_key[('session-1', 1)].buffer != '')

        # Rotation: segment 2 appears for the same session.
        await separator.offer(dto('새 세그먼트 문장입니다.', segment_id=2, sequence=2))

        assert await eventually(lambda: len(pusher.pushed) >= 2)
        assert '미완성 문장' in pusher.sources()
        assert '새 세그먼트 문장입니다.' in pusher.sources()
        # The orphaned key must be gone (memory leak fix).
        assert ('session-1', 1) not in separator.state_by_key
    finally:
        await separator.stop()


async def test_other_sessions_are_untouched_by_segment_rotation():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        await separator.offer(
            dto('세션 A 미완성', segment_id=1, sequence=1, session_id='session-A'))
        assert await eventually(
            lambda: ('session-A', 1) in separator.state_by_key
            and separator.state_by_key[('session-A', 1)].buffer != '')

        # A rotation in session B must not flush session A's buffer.
        await separator.offer(
            dto('세션 B 문장입니다.', segment_id=2, sequence=1, session_id='session-B'))

        assert await eventually(lambda: '세션 B 문장입니다.' in pusher.sources())
        assert ('session-A', 1) in separator.state_by_key
        assert '세션 A 미완성' not in pusher.sources()
    finally:
        await separator.stop()
