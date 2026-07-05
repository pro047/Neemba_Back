import asyncio
import re


from dataclasses import dataclass
from typing import List, Protocol

from kss import Kss  # type: ignore

from deepl import TextResult
from src.dto.translationDto import TranslationRequestDto


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
                 ) -> None:
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self.queue: asyncio.Queue[TranslationRequestDto] = asyncio.Queue(
            maxsize=1000)
        self.sentence_queue: asyncio.Queue[PendingSentence] = asyncio.Queue(
        )
        self.state_queue: asyncio.Queue[SegmentState] = asyncio.Queue()
        self.state_by_key: dict[tuple[str, int], SegmentState] = {}

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
                state = self.state_by_key.setdefault(key, SegmentState())

                state.buffer = (state.buffer + event.source_text).strip()
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

    async def _push_loop(self) -> None:
        while not self._stop:
            item = await self.sentence_queue.get()
            try:
                # Translate to the segment's requested target language
                # (falls back to en-US); recorded as the pair's target_lang.
                target_language = item.target_lang or 'en-US'
                translated = self.translator.translate(
                    item.source_text, target_language=target_language)
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

            closed = _is_sentence_closed(last)

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
            if not closed:
                # The unfinished tail goes back in front of whatever arrived
                # while the splitter was running.
                state.buffer = last + state.buffer
