"""asyncpg-backed writes for the monitoring tables (``app.*``).

All functions take an :class:`asyncpg.Pool`. They are intentionally small and
side-effect-isolated: the translation INSERT runs on the fire-and-forget
capture path (see ``pushClient/pusher.py``) and must never raise into the
translation hot path. ``end_session`` is idempotent — the heart of the
``/internal/sessions/stop`` idempotency requirement (docs §3 / §7 Phase 4).
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from src.monitoring.metrics import record_ensure_session_failed

# --- SQL -------------------------------------------------------------------

# Session-row upsert. Created on the session start signal, but also called as
# a safety net from the first translation INSERT so the ended_at UPDATE always
# has a target even if the start signal was lost (docs §2 design decision).
_ENSURE_SESSION_SQL = (
    "INSERT INTO app.sessions (session_id, source_lang, target_lang) "
    "VALUES ($1, $2, $3) "
    "ON CONFLICT (session_id) DO NOTHING"
)

_INSERT_TRANSLATION_SQL = (
    "INSERT INTO app.translations "
    "(session_id, segment_id, sequence, source_text, translated_text, "
    " source_lang, target_lang, confidence) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
    "RETURNING id, created_at"
)

# Startup reconciliation (monitor-page-v2 D4-a): any session left with
# ended_at IS NULL when python boots is a ghost — its WS/pipeline state died
# with the previous process. Stamp them ended in bulk and refresh the
# translation_count that the lost stop handler would have written.
_CLOSE_STALE_SESSIONS_SQL = (
    "UPDATE app.sessions s SET ended_at = now(), "
    "translation_count = ("
    " SELECT count(*) FROM app.translations t WHERE t.session_id = s.session_id"
    ") "
    "WHERE ended_at IS NULL "
    "RETURNING session_id"
)

# Idempotent end: ended_at is stamped exactly once (the WHERE guard makes a
# second call affect zero rows, returning no row).
_END_SESSION_SQL = (
    "UPDATE app.sessions SET ended_at = now() "
    "WHERE session_id = $1 AND ended_at IS NULL "
    "RETURNING session_id"
)

_COUNT_TRANSLATIONS_SQL = (
    "SELECT count(*) FROM app.translations WHERE session_id = $1"
)

_SET_COUNT_SQL = (
    "UPDATE app.sessions SET translation_count = $2 WHERE session_id = $1"
)

_GET_COUNT_SQL = (
    "SELECT COALESCE(translation_count, 0) FROM app.sessions WHERE session_id = $1"
)


async def ensure_session(
    pool,
    session_id: str,
    source_lang: str | None = None,
    target_lang: str | None = None,
) -> None:
    """Create the session row if it does not already exist (no-op otherwise)."""
    async with pool.acquire() as conn:
        await conn.execute(_ENSURE_SESSION_SQL, session_id, source_lang, target_lang)


async def ensure_session_with_retry(
    pool,
    session_id: str,
    source_lang: str | None = None,
    target_lang: str | None = None,
    *,
    retries: int = 2,
    retry_delay: float = 0.1,
) -> None:
    """:func:`ensure_session` with transient-failure retries (WU2).

    Every failed attempt bumps ``neemba_ensure_session_failed_total``; the last
    attempt's exception propagates so callers keep their existing isolation
    (try/except) semantics. Delay doubles per retry (0.1s → 0.2s …) — this only
    ever runs on the start handler and the fire-and-forget capture task, never
    on the translation hot path.
    """
    for attempt in range(retries + 1):
        try:
            await ensure_session(pool, session_id, source_lang, target_lang)
        except Exception:
            record_ensure_session_failed()
            if attempt == retries:
                raise
            await asyncio.sleep(retry_delay * (2 ** attempt))
        else:
            return


async def insert_translation(
    pool,
    *,
    session_id: str,
    source_text: str,
    translated_text: str,
    segment_id: int | None = None,
    sequence: int | None = None,
    source_lang: str | None = None,
    target_lang: str | None = None,
    confidence: float | None = None,
) -> tuple[int, datetime] | None:
    """Insert one (already-masked) source↔translation pair.

    Returns the DB-assigned ``(id, created_at)`` so the capture path can put
    them on the live monitor payload (D3: broadcast after insert) — the keys
    the frontend uses for history↔live dedup and gap fill.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_TRANSLATION_SQL,
            session_id,
            segment_id,
            sequence,
            source_text,
            translated_text,
            source_lang,
            target_lang,
            confidence,
        )
    if row is None:
        return None
    return int(row["id"]), row["created_at"]


async def close_stale_sessions(pool) -> list[str]:
    """Stamp every ``ended_at IS NULL`` session ended (startup, D4-a).

    Called once from lifespan startup: sessions whose stop signal was lost
    (or that were live when the previous process died) would otherwise show a
    ghost LIVE badge forever. Also refreshes their ``translation_count``.
    Returns the reconciled session ids (empty on a clean start).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_CLOSE_STALE_SESSIONS_SQL)
    return [r["session_id"] for r in rows]


async def end_session(pool, session_id: str) -> tuple[bool, int]:
    """Idempotently mark a session ended and return ``(ended, count)``.

    ``ended`` is ``True`` only on the *first* call that transitions
    ``ended_at`` from NULL → now(); subsequent calls return ``False`` with no
    further writes. ``count`` is the session's translation count (recomputed
    and persisted on the first call, read back on later calls).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_END_SESSION_SQL, session_id)
        ended = row is not None
        if ended:
            count = await conn.fetchval(_COUNT_TRANSLATIONS_SQL, session_id)
            count = int(count or 0)
            await conn.execute(_SET_COUNT_SQL, session_id, count)
        else:
            count = await conn.fetchval(_GET_COUNT_SQL, session_id)
            count = int(count or 0)
        return ended, count
