import logging
from typing import Awaitable, Callable, List, Protocol, Optional
from src.dto.translationDto import TranslationRequestDto
from src.sentenceTranslator.sentenceTranslator import SentenceTranslator

OnRequest = Callable[[TranslationRequestDto], Awaitable[None]]


class Pusher(Protocol):
    async def push_to_client(self,
                             push_text: str, sequence: Optional[int]): ...


class SessionManager(Protocol):
    async def set_session(self, session_id: str) -> None: ...
    async def get_session(self) -> str: ...


def build_on_request(sentence_translator: SentenceTranslator, pusher: Pusher) -> OnRequest:
    async def on_request(dto: TranslationRequestDto) -> None:
        raw_texts: List[str] = (
            dto.source_text if isinstance(dto.source_text, list) else [
                dto.source_text]
        )
        for raw_text in raw_texts:
            translated_lists = await sentence_translator.translate_text(raw_text=raw_text)
            for translated_text in translated_lists:
                print('on Request text : ', translated_text)
                try:
                    await pusher.push_to_client(translated_text, None)
                except Exception:
                    logging.basicConfig(level=logging.ERROR)
                    logging.exception('push to client erro')
    return on_request
