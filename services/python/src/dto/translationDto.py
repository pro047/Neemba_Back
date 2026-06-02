from dataclasses import dataclass


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
    """A finished source↔translation pair plus the metadata needed to store it.

    Phase 4 enriches this beyond the original ``translated_text`` so the
    capture path (``pusher.py`` → ``app.translations`` + monitor WS) has the
    full payload. The extra fields are optional to stay backward compatible
    with any caller that only set ``translated_text``.
    """

    session_id: str
    translated_text: str
    source_text: str | None = None
    source_lang: str | None = None
    target_lang: str | None = None
    sequence: int | None = None
    segment_id: int | None = None
    confidence: float | None = None
