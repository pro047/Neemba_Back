from dataclasses import dataclass
from typing import Optional


@dataclass
class TranslationRequestDto:
    session_id: str
    segment_id: int
    sequence: int
    source_text: str
    target_lang: str
    source_lang: str
    confidence: float = 0.0


@dataclass
class TranslationResultDto:
    session_id: str
    translated_text: str
