"""DeepL ``context`` window (observation-2026-09-20 §4 follow-up).

The separator ships sentences one at a time, so DeepL never sees the subject a
preacher dropped two sentences back ("조상이요" / "나를 도와줄 수가 없어요" →
"You can't help me."). Passing the session's previous source sentences as
``context`` fixed that offline: 17 → 1 wrong subjects on the tuning set and
11~12 → 1 on a hold-out sermon (~/neemba-logs/ab-context, N=8).

These tests pin how the window is kept, not DeepL's behaviour.
"""
from unittest import mock

from src.deepL.deepL import DeeplTranslationService
from src.separator.kss_separator import CONTEXT_IDLE_SEC, SentenceSeparator
from tests.test_separator_dedup import A, B, C, push_sentences
from tests.test_separator_pipeline import (
    FakePusher,
    eventually,
    simple_split,
)


class RecordingTranslator:
    """Captures the context each sentence was translated with."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail_on = fail_on

    def translate(self, source_text, target_language, context=None):
        self.calls.append((source_text, context))
        if self.fail_on is not None and self.fail_on in source_text:
            raise RuntimeError('deepl boom')
        return f'EN::{source_text}'

    def context_of(self, source_text: str) -> str | None:
        return [ctx for src, ctx in self.calls if src == source_text][-1]


def make_separator(translator, context_sentences: int = 8) -> SentenceSeparator:
    with mock.patch('src.separator.kss_separator.Kss'):
        separator = SentenceSeparator(translator, FakePusher(),
                                      context_sentences=context_sentences)
    separator.splitter = simple_split
    return separator


def numbered(n: int) -> list[str]:
    # Distinct and long enough that the dedup guard never interferes.
    return [f'{i}번째 문장은 서로 다른 내용을 담고 있습니다' for i in range(n)]


async def test_first_sentence_of_a_session_has_no_context():
    translator = RecordingTranslator()
    separator = make_separator(translator)
    await separator.start()
    try:
        await push_sentences(separator, [A])
        assert await eventually(lambda: len(translator.calls) == 1)
        assert translator.context_of(f'{A}.') is None
    finally:
        await separator.stop()


async def test_context_is_previous_sentences_oldest_first_capped_at_window():
    translator = RecordingTranslator()
    separator = make_separator(translator, context_sentences=3)
    texts = numbered(5)
    await separator.start()
    try:
        await push_sentences(separator, texts)
        assert await eventually(lambda: len(translator.calls) == 5)
        assert translator.context_of(f'{texts[2]}.') == f'{texts[0]}. {texts[1]}.'
        # The 4th and 5th overflow the window: the oldest falls out.
        assert translator.context_of(f'{texts[4]}.') == \
            f'{texts[1]}. {texts[2]}. {texts[3]}.'
    finally:
        await separator.stop()


async def test_sessions_do_not_share_context():
    translator = RecordingTranslator()
    separator = make_separator(translator)
    await separator.start()
    try:
        await push_sentences(separator, [A], session_id='session-1')
        assert await eventually(lambda: len(translator.calls) == 1)
        await push_sentences(separator, [B], session_id='session-2',
                             segment_id=2)
        assert await eventually(lambda: len(translator.calls) == 2)
        await push_sentences(separator, [C], session_id='session-1',
                             start_sequence=2)
        assert await eventually(lambda: len(translator.calls) == 3)

        assert translator.context_of(f'{B}.') is None
        assert translator.context_of(f'{C}.') == f'{A}.'
    finally:
        await separator.stop()


async def test_dropped_replay_is_not_remembered():
    translator = RecordingTranslator()
    separator = make_separator(translator)
    tail = '마지막으로 드리는 말씀을 기억하시기 바랍니다'
    await separator.start()
    try:
        await push_sentences(separator, [A, B])
        assert await eventually(lambda: len(translator.calls) == 2)
        # Replay walks A, B: A is republished, B is dropped before translate.
        await push_sentences(separator, [A, B], start_sequence=10)
        await push_sentences(separator, [tail], start_sequence=20)
        assert await eventually(lambda: len(translator.calls) == 4)

        assert translator.context_of(f'{tail}.') == f'{A}. {B}. {A}.'
    finally:
        await separator.stop()


async def test_failed_translation_is_still_remembered():
    translator = RecordingTranslator(fail_on=A)
    separator = make_separator(translator)
    await separator.start()
    try:
        # The sentence was spoken even if DeepL failed on it.
        await push_sentences(separator, [A, B])
        assert await eventually(lambda: len(translator.calls) == 2)
        assert translator.context_of(f'{B}.') == f'{A}.'
    finally:
        await separator.stop()


async def test_close_session_clears_context():
    translator = RecordingTranslator()
    separator = make_separator(translator)
    await separator.start()
    try:
        await push_sentences(separator, [A])
        assert await eventually(lambda: 'session-1' in separator.context_history)
        await separator.close_session('session-1')
        assert 'session-1' not in separator.context_history
    finally:
        await separator.stop()


async def test_idle_session_context_expires():
    translator = RecordingTranslator()
    separator = make_separator(translator)
    await separator.start()
    try:
        await push_sentences(separator, [A])
        await push_sentences(separator, [B], session_id='session-2',
                             segment_id=2)
        assert await eventually(lambda: len(translator.calls) == 2)

        # A long pause (a hymn) — the sermon before it is not this sentence's
        # context. The sweep also reclaims sessions that never got a stop call.
        separator.context_history['session-1'].last_seen -= CONTEXT_IDLE_SEC + 1
        separator.context_history['session-2'].last_seen -= CONTEXT_IDLE_SEC + 1
        await push_sentences(separator, [C], start_sequence=2)
        assert await eventually(lambda: len(translator.calls) == 3)

        assert translator.context_of(f'{C}.') is None
        assert 'session-2' not in separator.context_history
    finally:
        await separator.stop()


async def test_window_zero_disables_context():
    translator = RecordingTranslator()
    separator = make_separator(translator, context_sentences=0)
    await separator.start()
    try:
        await push_sentences(separator, [A, B])
        assert await eventually(lambda: len(translator.calls) == 2)
        assert translator.context_of(f'{B}.') is None
        assert separator.context_history == {}
    finally:
        await separator.stop()


def test_deepl_service_sends_context_only_when_present():
    with mock.patch('src.deepL.deepL.Translator') as translator_cls:
        service = DeeplTranslationService('key')
        service.translate('문장', 'en-US')
        service.translate('문장', 'en-US', context='')
        service.translate('문장', 'en-US', context='앞 문장')

    calls = translator_cls.return_value.translate_text.call_args_list
    assert calls[0] == mock.call('문장', target_lang='en-US')
    assert calls[1] == mock.call('문장', target_lang='en-US')
    assert calls[2] == mock.call('문장', target_lang='en-US', context='앞 문장')
