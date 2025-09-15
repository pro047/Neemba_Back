from typing import List, Protocol
from src.dto.translationDto import TranslationRequestDto


class Separator(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def offer(self, event: TranslationRequestDto) -> None: ...


class Translator(Protocol):
    def translate(self, source_text: str,
                  target_language: str) -> str: ...


class SentenceTranslator:
    def __init__(self, separator: Separator, translator: Translator, ) -> None:
        self.separator = separator
        self.translator = translator

    async def separator_text(self, dto: TranslationRequestDto):
        await self.separator.offer(dto)

    def translate_text(self, raw_text: str) -> List[str]:
        sentences = self.separator.separate(raw_text)
        translated_sentences: List[str] = []
        for sentence in sentences:
            translated = self.translator.translate(
                source_text=sentence, target_language="en-US")
            translated_sentences.append(translated)
            print('translated:', translated)
        return translated_sentences
