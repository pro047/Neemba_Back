"""P1 성능 계기 3종 회귀 테스트 (perf-test-plan.md §7).

이 세 지표는 하나의 가설을 위해 존재한다 — **DeepL 이 동기 호출인데 _push_loop
이 단일 태스크라, 번역 한 건이 도는 동안 이벤트 루프 전체가 멈춘다.** 따라서
"값이 갱신되는가" 만 보면 부족하고, 루프를 실제로 막았을 때 그것이 지연으로
잡히는지까지 본다. 잡히지 않으면 계기가 있어도 가설을 반증할 수 없다.
"""
import asyncio
import time
from unittest import mock

from prometheus_client import REGISTRY

from src.monitoring import metrics
from src.separator.kss_separator import (
    PendingSentence,
    SegmentState,
    SentenceSeparator,
)


def sample(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


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
        self.pushed: list[object] = []

    async def push_to_client(self, push_text, sequence, **kwargs):
        self.pushed.append(push_text)


class SlowTranslator:
    """Blocking translator — mirrors DeepL's synchronous round trip."""

    def __init__(self, seconds: float = 0.0) -> None:
        self.seconds = seconds

    def translate(self, source_text, target_language):
        if self.seconds:
            time.sleep(self.seconds)
        return f'EN::{source_text}'


class FailingTranslator:
    def translate(self, source_text, target_language):
        raise RuntimeError('deepl boom')


class DepthProbingTranslator:
    """Reads the depth gauge mid-translation.

    That instant is the only place the drain-side sample is observable: by the
    time the loop comes back the queue has moved on.
    """

    def __init__(self) -> None:
        self.observed: list[float | None] = []

    def translate(self, source_text, target_language):
        self.observed.append(sample('neemba_sentence_queue_depth'))
        return f'EN::{source_text}'


def make_separator(translator) -> SentenceSeparator:
    # Patch Kss so constructing the separator needs no real KSS model.
    with mock.patch('src.separator.kss_separator.Kss'):
        return SentenceSeparator(translator, FakePusher())


def pending(text: str) -> PendingSentence:
    return PendingSentence(
        source_text=text, session_id='s1', segment_id=1, sequence=1,
        source_lang='ko-KR', target_lang='en-US', confidence=0.9,
    )


async def run_until(coro_fn, done, timeout: float = 2.0) -> None:
    """Run one separator loop until ``done()`` holds, then cancel it.

    The loops are infinite by design, so every test drives them this way
    instead of awaiting completion.
    """
    task = asyncio.create_task(coro_fn())
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_successful_translation_records_its_round_trip_duration():
    separator = make_separator(SlowTranslator(0.05))
    before_count = sample('neemba_translate_duration_seconds_count') or 0.0
    before_sum = sample('neemba_translate_duration_seconds_sum') or 0.0
    await separator.sentence_queue.put(pending('안녕하세요.'))

    await run_until(separator._push_loop,
                    lambda: separator.sentence_queue.qsize() == 0)

    assert sample('neemba_translate_duration_seconds_count') == before_count + 1.0
    assert (sample('neemba_translate_duration_seconds_sum') or 0.0) >= before_sum + 0.05


async def test_failed_translation_is_recorded_too_so_the_worst_stall_survives():
    separator = make_separator(FailingTranslator())
    before = sample('neemba_translate_duration_seconds_count') or 0.0
    await separator.sentence_queue.put(pending('안녕하세요.'))

    await run_until(separator._push_loop,
                    lambda: separator.sentence_queue.qsize() == 0)

    assert sample('neemba_translate_duration_seconds_count') == before + 1.0


async def test_queue_depth_is_recorded_as_each_sentence_is_drained():
    translator = DepthProbingTranslator()
    separator = make_separator(translator)
    for i in range(3):
        await separator.sentence_queue.put(pending(f'문장{i}.'))

    await run_until(separator._push_loop,
                    lambda: len(translator.observed) == 3)

    # 꺼낸 뒤의 잔량이므로 3건이면 2 → 1 → 0 이다.
    assert translator.observed == [2.0, 1.0, 0.0]


async def test_queue_depth_is_visible_before_any_translation_runs():
    # 유입 폭증은 push 쪽 샘플만으로는 안 보인다 — 소비가 시작되기 전에
    # 생산 쪽이 값을 올려야 "유입 > 처리" 판정이 가능하다.
    separator = make_separator(SlowTranslator())
    separator.splitter = simple_split
    metrics.set_sentence_queue_depth(0)
    await separator.state_queue.put(SegmentState(
        buffer='첫 문장. 둘째 문장.', session_id='s1', segment_id=1,
        force_closed=True,
    ))

    await run_until(separator._flush,
                    lambda: separator.sentence_queue.qsize() == 2)

    assert sample('neemba_sentence_queue_depth') == 2.0


async def test_lag_sampler_observes_once_per_interval():
    before = sample('neemba_event_loop_lag_seconds_count') or 0.0

    await run_until(
        lambda: metrics.sample_event_loop_lag(0.01),
        lambda: (sample('neemba_event_loop_lag_seconds_count') or 0.0) >= before + 2.0,
    )

    assert (sample('neemba_event_loop_lag_seconds_count') or 0.0) >= before + 2.0


async def test_blocking_the_loop_synchronously_shows_up_as_lag():
    # 이 테스트가 P1 의 존재 이유다. time.sleep 은 DeepL 동기 왕복과 같은 모양으로
    # 루프 스레드를 점유하므로, 샘플러가 이것을 못 잡으면 블로킹 가설은 이 계기로
    # 증명도 반증도 되지 않는다.
    before_sum = sample('neemba_event_loop_lag_seconds_sum') or 0.0
    task = asyncio.create_task(metrics.sample_event_loop_lag(0.01))
    await asyncio.sleep(0)  # 샘플러를 sleep 안까지 진입시킨다
    time.sleep(0.3)
    await asyncio.sleep(0.05)  # 샘플러가 깨어나 관측할 틈
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert (sample('neemba_event_loop_lag_seconds_sum') or 0.0) >= before_sum + 0.2
