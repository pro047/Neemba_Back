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


class Pusher(Protocol):
    async def push_to_client(self,
                             push_text: TextResult | list[TextResult], sequence: int | None): ...


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
        self.sentence_queue: asyncio.Queue[str] = asyncio.Queue(
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
                await self.state_queue.put(state)
        finally:
            self.queue.task_done()

    async def _push_loop(self) -> None:
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

    async def _flush(self) -> None:
        try:
            while not self._stop:
                if self._stop:
                    break
                text = await self.state_queue.get()

                sentences: List[str] = await asyncio.to_thread(self.splitter, text.buffer)
                if not sentences:
                    continue

                last = sentences[-1]

                closed = _is_sentence_closed(last)

                end = len(sentences) if closed else max(
                    0, len(sentences) - 1)

                for s in sentences[:end]:
                    s_clean = s.strip()
                    if s_clean:
                        await self.sentence_queue.put(s_clean)
                text.buffer = " " if closed else last
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
