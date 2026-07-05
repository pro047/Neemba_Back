import asyncio
from typing import Any

from deepl import TextResult

from src.masking import mask_text
from src.repository.implementation.translation_repository import (
    ensure_session,
    insert_translation,
)
from src.ws.monitor import MonitorHub
from src.ws.websocket import WebSocketHub


def _coerce_text(value: Any) -> str:
    """Flatten a DeepL result (TextResult | list[TextResult] | str) to text."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_coerce_text(v) for v in value)
    if isinstance(value, TextResult):
        return value.text
    return str(value)


class Pusher:
    """Pushes translations to the client WS and (Phase 4) captures them.

    The client broadcast happens first and is never blocked by capture. The
    monitoring capture (mask → monitor WS fan-out → ``app.translations`` INSERT)
    runs as a fire-and-forget task fully wrapped in try/except, so a slow or
    failing DB/monitor never delays or breaks translation delivery.
    """

    def __init__(
        self,
        hub: WebSocketHub,
        *,
        monitor_hub: MonitorHub | None = None,
        db_pool: Any = None,
    ) -> None:
        self.hub = hub
        self.monitor_hub = monitor_hub
        self._db_pool = db_pool
        # Sessions we have already upserted, so the per-translation safety-net
        # ensure_session runs at most once per session.
        self._ensured_sessions: set[str] = set()
        # Keep strong refs to in-flight capture tasks (avoid GC of bare tasks).
        self._tasks: set[asyncio.Task[None]] = set()

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
    ) -> None:
        # 1) Client delivery — hot path, must not be blocked by capture.
        await self.hub.broadcast_to_session(payload={
            "sequence": sequence,
            "sentence": push_text,
            "isFinal": True,
        })

        # 2) Monitoring capture — fire-and-forget, isolated. Only when we have
        #    enough context (source text + session) to store a pair.
        if source_text is None or not session_id:
            return

        task = asyncio.create_task(self._capture(
            session_id=session_id,
            segment_id=segment_id,
            sequence=sequence,
            source_text=source_text,
            translated_text=_coerce_text(push_text),
            source_lang=source_lang,
            target_lang=target_lang,
            confidence=confidence,
        ))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(
        self,
        *,
        session_id: str,
        segment_id: int | None,
        sequence: int | None,
        source_text: str,
        translated_text: str,
        source_lang: str | None,
        target_lang: str | None,
        confidence: float | None,
    ) -> None:
        try:
            masked_source = mask_text(source_text)
            masked_translated = mask_text(translated_text)

            payload = {
                "type": "translation",
                "sessionId": session_id,
                "segmentId": segment_id,
                "sequence": sequence,
                "sourceText": masked_source,
                "translatedText": masked_translated,
                "sourceLang": source_lang,
                "targetLang": target_lang,
                "confidence": confidence,
            }

            # Live monitor fan-out (masked) before the DB write.
            if self.monitor_hub is not None:
                await self.monitor_hub.broadcast(session_id, payload)

            # Persist the masked pair (mask-at-write).
            pool = self._db_pool
            if pool is not None:
                if session_id not in self._ensured_sessions:
                    await ensure_session(pool, session_id, source_lang, target_lang)
                    self._ensured_sessions.add(session_id)
                await insert_translation(
                    pool,
                    session_id=session_id,
                    source_text=masked_source or "",
                    translated_text=masked_translated or "",
                    segment_id=segment_id,
                    sequence=sequence,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    confidence=confidence,
                )
        except Exception as e:
            # Capture failures never touch the translation/broadcast path.
            print("pusher: capture failed (ignored):", repr(e))
