import asyncio
import os
import re
import threading


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
    return bool(re.search(r'([\.!\?…]|\s*[다요죠네])\s*$', text.strip()))


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
                print(f'sep - start ignored {os.getpid()} {id(self)}')
                return
            self._start = True
            print(f'sep - start : {os.getpid} {id(self)}')
        self._tasks = [
            asyncio.create_task(self._flush()),
            asyncio.create_task(self._store_text_loop()),
            asyncio.create_task(self._push_loop()),
        ]
        print(self._tasks)

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
        print('regist _store_text_loop')

        try:
            while not self._stop:
                event: TranslationRequestDto = await self.queue.get()
                key = (event.session_id, event.segment_id)
                state = self.state_by_key.setdefault(key, SegmentState())

                state.buffer = (state.buffer + event.source_text).strip()
        finally:
            self.queue.task_done()

    async def _push_loop(self) -> None:
        print('regist _push_loop')

        try:
            while not self._stop:
                sentence = await self.sentence_queue.get()
                print(f'push loop : {sentence}')
                try:
                    translated = self.translator.translate(
                        sentence, target_language='en-US')
                    await self.pusher.push_to_client(translated, None)
                finally:
                    self.sentence_queue.task_done()
        except asyncio.CancelledError:
            pass

    # async def _ticker_loop(self) -> None:
    #     print('regist _ticker_loop')

    #     try:
    #         while not self._stop:
    #             try:
    #                 await asyncio.sleep(3.0)
    #             except asyncio.CancelledError:
    #                 break
    #             await self._flush()

    #     except asyncio.CancelledError:
    #         pass

    async def _get_state_item(self) -> str:
        print('get state item')
        text = ''
        for key, state in list(self.state_by_key.items()):
            text = state.buffer.strip()
        return text

    async def _flush(self) -> None:
        print('regist flush')

        try:
            while not self._stop:
                text = await self._get_state_item()
                print(f'text : {text}')
                if not text:
                    print('flush : not found text')
                    return

                sentences: List[str] = await asyncio.to_thread(self.splitter, text)
                print('separator - sentences : ', sentences)
                if not sentences:
                    return

                last = sentences[-1]
                print(f'sep - last : {last}')
                closed = _is_sentence_closed(last)
                print(f'sep - closed : {closed}')
                end = len(sentences) if closed else max(
                    0, len(sentences) - 1)
                print(f'sep - end : {end}')

                for s in sentences[:end]:
                    s_clean = s.strip()
                    if s_clean:
                        await self.sentence_queue.put(s_clean)
                        print(f's_clean : {s_clean}')
                # state.buffer = " " if closed else last
        except asyncio.CancelledError:
            print('flush finished')
