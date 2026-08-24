import asyncio
import re
import time


from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import List, Protocol

from kss import Kss  # type: ignore

from deepl import TextResult
from src.dto.translationDto import TranslationRequestDto
from src.monitoring import metrics


# --- duplicate re-send guard (handover §7, 8/23 design) -----------------------
# A stalled STT stream makes Google replay an earlier span, and node's
# computeDelta republishes all of it under fresh sequence numbers — which every
# dedup layer downstream keys on, so none of them stop it. The guard cannot ask
# "have we said this before" (preachers repeat themselves, refrains repeat by
# design); it asks whether the stream is walking the published history IN ORDER.
#
# Thresholds are confirmed against the 30-day export by
# scripts/simulate_runtime_dedup.py: the shortest sentence a real re-send
# carried was 12 normalized chars and the weakest true match scored 0.94, so
# both sit one notch inside. Loosening either only widens the false-positive
# surface without dropping one more replayed sentence.
DEDUP_MIN_CHARS = 10
DEDUP_SIM_THRESHOLD = 0.90
DEDUP_LOOKBACK_SEC = 300.0
DEDUP_HISTORY_MAX = 200

_NON_WORD = re.compile(r'[^0-9A-Za-z가-힣]')


def _normalize(text: str) -> str:
    """Strip everything the recognizer varies between passes (spacing, punctuation)."""
    return _NON_WORD.sub('', text)


def _similar(a: str, b: str) -> bool:
    """difflib ratio against the threshold, with a length prune.

    ratio() is bounded above by 2*min(len)/(len(a)+len(b)), so texts of very
    different length are rejected without running the matcher. This runs inside
    _push_loop, the single task the whole pipeline drains through, so the
    comparison budget per sentence has to stay small.
    """
    la, lb = len(a), len(b)
    if 2 * min(la, lb) < DEDUP_SIM_THRESHOLD * (la + lb):
        return False
    return SequenceMatcher(None, a, b).ratio() >= DEDUP_SIM_THRESHOLD


@dataclass
class DedupState:
    """One session's published-sentence history and its replay cursor."""
    # (absolute index, normalized text, monotonic clock) of published sentences.
    history: deque[tuple[int, str, float]] = field(
        default_factory=lambda: deque(maxlen=DEDUP_HISTORY_MAX))
    next_index: int = 0
    # History position the last match landed on. A replay advances it by one
    # slot per sentence; anything else restarts the run.
    cursor: int | None = None
    # Monotonic clock of the last sentence judged for this session.
    last_seen: float = 0.0


@dataclass
class SegmentState:
    buffer: str = ''
    # Metadata carried alongside the buffer so a flushed sentence can be paired
    # back to its source segment for monitoring/storage (Phase 4), and so the
    # hub can gate delivery to the session that owns the client slot.
    session_id: str = ''
    segment_id: int = 0
    sequence: int = 0
    source_lang: str | None = None
    target_lang: str | None = None
    confidence: float = 0.0
    # Set when the segment's stream is gone (rotation), the session stopped,
    # or input went quiet past the flush timeout: the next flush treats the
    # buffer as closed regardless of sentence-ending heuristics (one-shot).
    force_closed: bool = False
    # Monotonic clock of the last buffer append; drives the timeout flush.
    last_appended_at: float = 0.0


@dataclass
class PendingSentence:
    """A split source sentence plus the metadata needed to store the pair."""
    source_text: str
    session_id: str
    segment_id: int
    sequence: int
    source_lang: str | None
    target_lang: str | None
    confidence: float


class Pusher(Protocol):
    async def push_to_client(
        self,
        push_text: TextResult | list[TextResult],
        sequence: int | None,
        *,
        source_text: str | None = None,
        session_id: str | None = None,
        segment_id: int | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,
        confidence: float | None = None,
    ): ...


class Translator(Protocol):
    def translate(self, source_text: str,
                  target_language: str) -> TextResult | list[TextResult]: ...


def _is_sentence_closed(text: str) -> bool:
    """
    문장이 종결되었는지 확인하는 함수
    - 마침표, 느낌표, 물음표, 줄임표로 끝나는지 확인
    - 한국어 종결어미로 끝나는지 확인 (공백 유무와 관계없이)
    """
    text = text.strip()
    if not text:
        return False

    # 구두점으로 끝나는지 확인
    if re.search(r'[\.!\?…]\s*$', text):
        return True

    # 한국어 종결어미로 끝나는지 확인
    # 단순 종결어미: 다, 요, 죠, 네, 어요, 아요
    # 복합 종결어미: ~는데요, ~습니다, ~습니까, ~지요, ~게요, ~을게요, ~을까요,
    #                ~으니까요, ~네요, ~인데요, ~래요, ~거예요 등
    # 공백이 있을 수도 있고 없을 수도 있음
    endings = [
        r'[다요죠네]\s*$',  # 단순 종결어미
        r'어요\s*$',
        r'아요\s*$',
        r'는데요\s*$',
        r'은데요\s*$',
        r'습니다\s*$',
        r'습니까\s*$',
        r'지요\s*$',
        r'게요\s*$',
        r'을게요\s*$',
        r'을까요\s*$',
        r'으니까요\s*$',
        r'네요\s*$',
        r'인데요\s*$',
        r'래요\s*$',
        r'거예요\s*$',
        r'니다\s*$',
    ]

    for ending in endings:
        if re.search(ending, text):
            return True

    return False


class SentenceSeparator:
    def __init__(self,
                 translator: Translator,
                 pusher: Pusher,
                 flush_timeout_seconds: float = 2.0,
                 ) -> None:
        # After this much input silence, an unfinished buffered sentence is
        # shipped as-is instead of waiting (possibly forever) for a closing
        # ending. Tune against real speech pauses.
        self._flush_timeout = flush_timeout_seconds
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self.queue: asyncio.Queue[TranslationRequestDto] = asyncio.Queue(
            maxsize=1000)
        self.sentence_queue: asyncio.Queue[PendingSentence] = asyncio.Queue(
        )
        self.state_queue: asyncio.Queue[SegmentState] = asyncio.Queue()
        self.state_by_key: dict[tuple[str, int], SegmentState] = {}
        self.dedup_history: dict[str, DedupState] = {}

        self._start = False
        self._stop = False
        self.lastSentAt = 0.0

        self.pusher = pusher
        self.translator = translator
        self.splitter = Kss("split_sentences")

    async def start(self) -> None:
        async with self._lock:
            if self._start:
                return
            self._start = True
        self._tasks = [
            asyncio.create_task(self._flush()),
            asyncio.create_task(self._store_text_loop()),
            asyncio.create_task(self._push_loop()),
            asyncio.create_task(self._timeout_sweeper()),
        ]

    async def stop(self) -> None:
        self._stop = True
        for t in self._tasks:
            if t:
                t.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def offer(self, event: TranslationRequestDto):
        await self.queue.put(event)

    async def _store_text_loop(self) -> None:
        try:
            while not self._stop:
                event: TranslationRequestDto = await self.queue.get()
                key = (event.session_id, event.segment_id)

                if key not in self.state_by_key:
                    # A new segment means the session's previous stream was
                    # rotated away: its buffered tail can never be completed,
                    # so force-flush it now instead of orphaning it forever
                    # (text loss + state_by_key leak).
                    stale_keys = [
                        k for k in self.state_by_key
                        if k[0] == event.session_id and k != key
                    ]
                    for stale_key in stale_keys:
                        stale = self.state_by_key.pop(stale_key)
                        if stale.buffer.strip():
                            stale.force_closed = True
                            await self.state_queue.put(stale)

                state = self.state_by_key.setdefault(key, SegmentState())

                state.buffer = (state.buffer + event.source_text).strip()
                state.last_appended_at = asyncio.get_running_loop().time()
                # Carry the latest metadata so a flushed sentence keeps its
                # session/segment context for monitoring + storage.
                state.session_id = event.session_id
                state.segment_id = event.segment_id
                state.sequence = event.sequence
                state.source_lang = event.source_lang
                state.target_lang = event.target_lang
                state.confidence = event.confidence
                await self.state_queue.put(state)
        finally:
            self.queue.task_done()

    def _is_replayed(self, item: PendingSentence) -> bool:
        """Decide, on arrival, whether this sentence is a machine replay.

            short sentence            -> publish (indistinguishable from speech)
            no match in history       -> publish, record, run reset
            match at position j       -> publish, cursor = j
            match at cursor + 1       -> DROP, cursor advances
            match, cursor not contiguous -> publish, run restarts at j

        A dropped or matched sentence is never recorded: history holds
        originals only, so a replay lines up against the span it copied instead
        of against an earlier copy of itself. That is also why the FIRST
        sentence of a replayed block gets through — one repeat is not evidence,
        and catching it would need node's stall signal in the NATS payload.
        """
        norm = _normalize(item.source_text)
        if len(norm) < DEDUP_MIN_CHARS:
            return False

        now = time.monotonic()
        # close_session cannot be the only reclaim path: it pops the history
        # while the session's last buffer tail is still on state_queue, and that
        # tail reaches this method afterwards and recreates the entry. Sessions
        # that die without a stop call (publisher gone, crash) never pop at all.
        # Anything idle past the lookback can no longer match, so dropping it
        # costs no detection.
        for tracked_id, tracked in list(self.dedup_history.items()):
            if (tracked_id != item.session_id
                    and now - tracked.last_seen > DEDUP_LOOKBACK_SEC):
                del self.dedup_history[tracked_id]

        state = self.dedup_history.setdefault(item.session_id, DedupState())
        state.last_seen = now
        while state.history and now - state.history[0][2] > DEDUP_LOOKBACK_SEC:
            state.history.popleft()

        match: int | None = None
        for index, prev_norm, _ in reversed(state.history):
            if not _similar(prev_norm, norm):
                continue
            if match is None:
                # Scanning newest first: a replay's original is the most recent
                # match, since a matched sentence is never recorded and so the
                # history keeps only the first utterance of anything repeated.
                match = index
            if state.cursor is not None and index == state.cursor + 1:
                # Continuing the run beats a nearer coincidence.
                match = index
                break

        if match is None:
            state.history.append((state.next_index, norm, now))
            state.next_index += 1
            state.cursor = None
            return False

        replayed = state.cursor is not None and match == state.cursor + 1
        state.cursor = match
        return replayed

    async def _push_loop(self) -> None:
        while not self._stop:
            item = await self.sentence_queue.get()
            metrics.set_sentence_queue_depth(self.sentence_queue.qsize())
            try:
                # Judged here rather than in _flush because rotation, timeout
                # and stop all converge on sentence_queue — this is the single
                # outlet — and it sits before translate, so a dropped sentence
                # costs no DeepL call either.
                if self._is_replayed(item):
                    metrics.record_duplicate_dropped()
                    # The only record a dropped sentence leaves: nothing reaches
                    # the DB or the screen, so a false positive is undebuggable
                    # without the text.
                    print(f'separator: duplicate re-send dropped '
                          f'(session={item.session_id} seq={item.sequence}): '
                          f'{item.source_text[:40]!r}')
                    continue
                # Translate to the segment's requested target language
                # (falls back to en-US); recorded as the pair's target_lang.
                target_language = item.target_lang or 'en-US'
                started = time.perf_counter()
                try:
                    translated = self.translator.translate(
                        item.source_text, target_language=target_language)
                finally:
                    # Timed in a finally so a failed round trip still lands in
                    # the histogram: translate() is synchronous, so a DeepL
                    # timeout is the longest the event loop ever stalls — the
                    # exact tail this metric exists to expose.
                    metrics.observe_translate_duration(
                        time.perf_counter() - started)
                await self.pusher.push_to_client(
                    translated,
                    item.sequence,
                    source_text=item.source_text,
                    session_id=item.session_id,
                    segment_id=item.segment_id,
                    source_lang=item.source_lang,
                    target_lang=target_language,
                    confidence=item.confidence,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One failed translation must not kill the pipeline task; the
                # sentence is logged and dropped (retry policy is a follow-up).
                print(
                    f'separator: translate/push failed, sentence dropped '
                    f'(session={item.session_id} seq={item.sequence}): {exc!r}')
            finally:
                self.sentence_queue.task_done()

    async def _flush(self) -> None:
        while not self._stop:
            state = await self.state_queue.get()

            # Snapshot-and-clear with no await in between, so the store loop
            # cannot interleave here. Deltas arriving while KSS runs in the
            # worker thread accumulate in state.buffer and are re-merged
            # below instead of being overwritten (data-loss race).
            snapshot = state.buffer
            state.buffer = ''
            if not snapshot.strip():
                continue

            try:
                sentences: List[str] = await asyncio.to_thread(
                    self.splitter, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Transient splitter failure: put the text back (in front of
                # any deltas that arrived meanwhile) and keep the task alive.
                state.buffer = snapshot + state.buffer
                print(f'separator: split failed, buffer retained: {exc!r}')
                continue

            if not sentences:
                state.buffer = snapshot + state.buffer
                continue

            last = sentences[-1]

            closed = state.force_closed or _is_sentence_closed(last)
            # One-shot: a rotation/stop/timeout mark applies to this flush
            # only, never to the segment's future sentences.
            state.force_closed = False

            end = len(sentences) if closed else max(
                0, len(sentences) - 1)

            for s in sentences[:end]:
                s_clean = s.strip()
                if s_clean:
                    await self.sentence_queue.put(PendingSentence(
                        source_text=s_clean,
                        session_id=state.session_id,
                        segment_id=state.segment_id,
                        sequence=state.sequence,
                        source_lang=state.source_lang,
                        target_lang=state.target_lang,
                        confidence=state.confidence,
                    ))
            # Depth is sampled at both ends of the queue — here (arrival) and in
            # _push_loop (drain). Only the producer side shows a burst that the
            # single push task cannot keep up with.
            metrics.set_sentence_queue_depth(self.sentence_queue.qsize())
            if not closed:
                # The unfinished tail goes back in front of whatever arrived
                # while the splitter was running.
                state.buffer = last + state.buffer

    async def _timeout_sweeper(self) -> None:
        interval = max(self._flush_timeout / 4, 0.05)
        while not self._stop:
            await asyncio.sleep(interval)
            try:
                now = asyncio.get_running_loop().time()
                for state in list(self.state_by_key.values()):
                    if not state.buffer.strip():
                        continue
                    if now - state.last_appended_at < self._flush_timeout:
                        continue
                    state.force_closed = True
                    # Reset the clock so the state is not re-queued on every
                    # sweep while it waits in state_queue.
                    state.last_appended_at = now
                    await self.state_queue.put(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'separator: timeout sweeper error (ignored): {exc!r}')

    async def close_session(self, session_id: str) -> None:
        """Flush and drop every segment buffer of a stopped session.

        Called from /internal/sessions/stop — without this, a stopped
        session's buffered tail is lost and its state_by_key entries leak.
        """
        keys = [k for k in self.state_by_key if k[0] == session_id]
        for key in keys:
            state = self.state_by_key.pop(key)
            if state.buffer.strip():
                state.force_closed = True
                await self.state_queue.put(state)
        # Reclaims the history immediately in the normal path; the idle sweep in
        # _is_replayed is what actually bounds it, because the tail queued above
        # can recreate this entry after the pop. Segment rotation must NOT clear
        # it — the history is per session, and the replay that motivated all
        # this crossed a rotation boundary.
        self.dedup_history.pop(session_id, None)
