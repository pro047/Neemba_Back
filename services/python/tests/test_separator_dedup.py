"""Duplicate re-send guard (handover §7, 8/23 design).

A stalled STT stream makes Google replay an earlier span; node's ``computeDelta``
sees a reset ``prevText`` and republishes the whole thing. Every dedup layer
downstream keys on ``sequence``, and the replay carries fresh sequence numbers,
so viewers saw ~30 seconds of subtitles twice (observation-2026-08-23 §4-1).

The guard cannot ask "is this a duplicate" — a preacher repeats himself and a
worship refrain repeats by design. It asks whether the stream is WALKING the
published history in order, which is a thing only a machine replay does:

  first match  -> published (a person could be repeating himself)
  next sentence matching the NEXT history slot -> dropped (a replay is confirmed)

These tests pin that shape, not the thresholds. Parameters are confirmed
offline against the 30-day export by ``scripts/simulate_runtime_dedup.py``.
"""
from prometheus_client import REGISTRY

from src.separator.kss_separator import DEDUP_LOOKBACK_SEC, SentenceSeparator
from tests.test_separator_pipeline import (
    FakePusher,
    eventually,
    make_separator,
    simple_split,
)
from tests.test_separator_segments import dto

# Long enough to clear MIN_CHARS; a replay of short interjections is
# indistinguishable from speech and is deliberately let through.
A = '오늘 우리가 함께 예배를 드립니다'
B = '하나님의 은혜가 우리 모두에게 있습니다'
C = '이 말씀을 마음에 새기시기 바랍니다'


async def push_sentences(separator: SentenceSeparator, texts: list[str],
                         *, session_id: str = 'session-1',
                         segment_id: int = 1, start_sequence: int = 1) -> None:
    """Offer each text as its own closed sentence, in order."""
    for offset, text in enumerate(texts):
        await separator.offer(dto(f'{text}.', segment_id=segment_id,
                                  sequence=start_sequence + offset,
                                  session_id=session_id))


async def test_replayed_span_drops_every_sentence_after_the_first():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        await push_sentences(separator, [A, B, C])
        assert await eventually(lambda: len(pusher.pushed) == 3)

        # The replay walks the same three in the same order.
        await push_sentences(separator, [A, B, C], start_sequence=10)
        assert await eventually(lambda: len(pusher.pushed) == 4)

        # A is republished (one repeat is not evidence); B and C are the
        # consecutive walk that proves a machine replay.
        assert pusher.sources() == [f'{A}.', f'{B}.', f'{C}.', f'{A}.']
    finally:
        await separator.stop()


async def test_speaker_repeating_one_sentence_is_never_dropped():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        # A refrain: the same line over and over, never a walk through history.
        await push_sentences(separator, [A, B, A, A, A])
        assert await eventually(lambda: len(pusher.pushed) == 5)
        assert pusher.sources().count(f'{A}.') == 4
    finally:
        await separator.stop()


async def test_short_sentences_are_exempt():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        # `아멘`/`감사합니다` land in this order all service long; dropping them
        # would cut real speech, so they never enter the history either.
        await push_sentences(separator, ['아멘', '감사합니다', '아멘', '감사합니다'])
        assert await eventually(lambda: len(pusher.pushed) == 4)
    finally:
        await separator.stop()


async def test_history_is_isolated_per_session():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        await push_sentences(separator, [A, B], session_id='session-1')
        assert await eventually(lambda: len(pusher.pushed) == 2)

        # Same liturgy in a parallel service must not be silenced by the first.
        await push_sentences(separator, [A, B], session_id='session-2',
                             segment_id=2)
        assert await eventually(lambda: len(pusher.pushed) == 4)
    finally:
        await separator.stop()


async def test_close_session_releases_history():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        await push_sentences(separator, [A, B])
        assert await eventually(lambda: len(pusher.pushed) == 2)
        assert 'session-1' in separator.dedup_history

        await separator.close_session('session-1')
        # Sessions run for hours and never end for the process; a history that
        # outlives its session is the same leak audit M4 closed for buffers.
        assert 'session-1' not in separator.dedup_history
    finally:
        await separator.stop()


async def test_idle_session_history_is_reclaimed():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        await push_sentences(separator, [A, B])
        assert await eventually(lambda: 'session-1' in separator.dedup_history)

        # close_session pops the history while the session's last buffer tail is
        # still queued, and that tail recreates the entry on its way out — as do
        # sessions that die with no stop call at all. Only the idle sweep bounds
        # this, so it has to fire on a session it did not just judge.
        separator.dedup_history['session-1'].last_seen -= DEDUP_LOOKBACK_SEC + 1
        await push_sentences(separator, [A], session_id='session-2',
                             segment_id=2)

        assert await eventually(lambda: 'session-1' not in separator.dedup_history)
    finally:
        await separator.stop()


async def test_dropped_sentence_increments_the_counter():
    pusher = FakePusher()
    separator = make_separator(simple_split, pusher=pusher)
    await separator.start()
    try:
        before = REGISTRY.get_sample_value(
            'neemba_separator_duplicate_dropped_total') or 0.0
        await push_sentences(separator, [A, B])
        assert await eventually(lambda: len(pusher.pushed) == 2)
        await push_sentences(separator, [A, B], start_sequence=10)

        # Waiting on pusher.pushed == 3 would return when A is REpublished,
        # which is before B is judged: if the two land in separate flushes the
        # counter is still unchanged at that instant. Wait on the counter.
        assert await eventually(lambda: REGISTRY.get_sample_value(
            'neemba_separator_duplicate_dropped_total') == before + 1.0)
        assert pusher.sources() == [f'{A}.', f'{B}.', f'{A}.']
    finally:
        await separator.stop()
