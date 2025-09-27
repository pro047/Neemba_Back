import asyncio
import re
import heapq
from dataclasses import dataclass
from typing import List, Protocol

from kss import Kss  # type: ignore

from deepl import TextResult
from src.dto.translationDto import TranslationRequestDto


@dataclass
class SegmentState:
    carryover_text: str = ''


class Pusher(Protocol):
    async def push_to_client(self,
                             push_text: TextResult | list[TextResult], sequence: int | None): ...


class Translator(Protocol):
    def translate(self, source_text: str,
                  target_language: str) -> TextResult | list[TextResult]: ...


def _is_sentence_closed(text: str) -> bool:
    return bool(re.search(r'([\.!\?…]|\s*[다요죠네])\s*$', text.strip()))


class SentenceSeparator:
    def __init__(self,
                 translator: Translator,
                 pusher: Pusher,
                 curr_seq=0
                 ) -> None:
        self.queue: asyncio.Queue[TranslationRequestDto] = asyncio.Queue(
            maxsize=1000)
        self.sentence_queue: asyncio.Queue[str] = asyncio.Queue(
        )
        self.state_by_key: dict[tuple[str, int], SegmentState] = {}
        self.heap: list[tuple[int, TranslationRequestDto]] = []

        self.sep_sentenc = ""
        self._stop = False
        self.lastSentAt = 0.0
        self.curr_seq = curr_seq

        self._push_task: asyncio.Task[None] | None = None
        self._ticker_task: asyncio.Task[None] | None = None
        self._store_text_task: asyncio.Task[None] | None = None

        self.pusher = pusher
        self.translator = translator
        self.splitter = Kss("split_sentences")

    async def start(self) -> None:
        self._ticker_task = asyncio.create_task(self._ticker_loop())
        self._store_text_task = asyncio.create_task(self._store_text_loop())
        self._push_task = asyncio.create_task(self._push_loop())

    async def stop(self) -> None:
        self._stop = True
        for t in (self._ticker_task, self._push_task, self._store_text_task):
            if t:
                t.cancel()

    async def offer(self, event: TranslationRequestDto):
        await self.queue.put(event)

    async def _store_text_loop(self) -> None:
        print('regist _store_text_loop')

        try:
            while not self._stop:
                event: TranslationRequestDto = await self.queue.get()
                key = (event.session_id, event.segment_id)
                state = self.state_by_key.setdefault(key, SegmentState())

                state.carryover_text += event.source_text
                try:
                    pass
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _push_loop(self) -> None:
        print('regist _push_loop')

        try:
            while not self._stop:
                sentence = await self.sentence_queue.get()
                try:
                    translated = self.translator.translate(
                        sentence, target_language='en-US')
                    await self.pusher.push_to_client(translated, None)
                finally:
                    self.sentence_queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _ticker_loop(self) -> None:
        print('regist _ticker_loop')

        try:
            while not self._stop:
                await asyncio.sleep(3.0)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        for key, state in list(self.state_by_key.items()):
            text = state.carryover_text.strip()
            if not text:
                continue

            sentences: List[str] = await asyncio.to_thread(self.splitter, text)
            if not sentences:
                continue

            last = sentences[-1]
            closed = _is_sentence_closed(last)
            end = len(sentences) if closed else max(
                0, len(sentences) - 1)

            for s in sentences[:end]:
                s_clean = s.strip()
                if s_clean:
                    print(f's_clean : {s_clean}')
                    await self.sentence_queue.put(s_clean)
            state.carryover_text = " " if closed else last
