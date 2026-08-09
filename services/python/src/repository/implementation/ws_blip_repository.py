"""asyncpg-backed reads/writes for ``app.ws_blips`` (handover §4-7).

One row per /ws disconnect: inserted when the hub loses the client, closed in
place on reconnect. Never called on the translation hot path directly — the
hub goes through :class:`src.ws.blip_recorder.WsBlipRecorder`, which isolates
DB failures. Every value reaches SQL as a bound parameter (``$n``).
"""
from __future__ import annotations

from datetime import datetime

# (disconnected_at, id) 키셋 커서는 translations 검색과 같은 인코딩을 쓴다 —
# 토큰이 base64(JSON {"t","i"}) 라 컬럼 이름에 중립적이고, 프런트도 같은 opaque
# 규약(문자열을 그대로 되돌려준다)을 이미 쓰고 있다.
from src.repository.implementation.monitor_query_repository import (
    encode_search_cursor,
)

# --- limits (route-level clamp, sessions 와 동일 규약) ----------------------

BLIPS_LIMIT_DEFAULT = 50
BLIPS_LIMIT_MAX = 200

# --- SQL -------------------------------------------------------------------

_INSERT_BLIP_SQL = (
    "INSERT INTO app.ws_blips "
    "(session_id, client_id, close_code, close_reason, detected_by) "
    "VALUES ($1, $2, $3, $4, $5) "
    "RETURNING id"
)

# Idempotent close: the reconnected_at IS NULL guard makes a second close a
# no-op. duration_ms is derived from the DB clock so both timestamps share
# one time source.
_CLOSE_BLIP_SQL = (
    "UPDATE app.ws_blips SET reconnected_at = now(), "
    "duration_ms = (extract(epoch from (now() - disconnected_at)) * 1000)::bigint, "
    "flushed_count = $2, lost_count = $3 "
    "WHERE id = $1 AND reconnected_at IS NULL"
)

# Session ended before the client came back: stamp only what was lost.
# reconnected_at stays NULL on purpose (§4-7 결정 2 — NULL = 미복귀), so the
# same IS NULL guard also keeps this from touching an already-closed row.
_ABANDON_BLIP_SQL = (
    "UPDATE app.ws_blips SET lost_count = $2 "
    "WHERE id = $1 AND reconnected_at IS NULL"
)

_BLIP_COLS = (
    "id, session_id, client_id, disconnected_at, reconnected_at, duration_ms, "
    "flushed_count, lost_count, close_code, close_reason, detected_by"
)


# --- queries ---------------------------------------------------------------

async def insert_blip(
    pool,
    *,
    session_id: str,
    client_id: str | None = None,
    close_code: int | None = None,
    close_reason: str | None = None,
    detected_by: str,
) -> int | None:
    """Record a disconnect and return the new blip id.

    ``client_id`` identifies which of the session's sockets dropped (P1 D5);
    NULL is legal and means "not attributed to a socket".
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_BLIP_SQL,
            session_id,
            client_id,
            close_code,
            close_reason,
            detected_by,
        )
    return row["id"] if row is not None else None


async def close_blip(
    pool,
    *,
    blip_id: int,
    flushed_count: int,
    lost_count: int,
) -> None:
    """Stamp the reconnect (timestamp + duration + counts) on an open blip."""
    async with pool.acquire() as conn:
        await conn.execute(_CLOSE_BLIP_SQL, blip_id, flushed_count, lost_count)


async def abandon_blip(pool, *, blip_id: int, lost_count: int) -> None:
    """Close the books on a blip whose session ended without a reconnect."""
    async with pool.acquire() as conn:
        await conn.execute(_ABANDON_BLIP_SQL, blip_id, lost_count)


async def list_blips(
    pool,
    *,
    session_id: str | None = None,
    limit: int = BLIPS_LIMIT_DEFAULT,
    cursor: tuple[datetime, int] | None = None,
) -> tuple[list, str | None]:
    """List blips newest-first, optionally for one session.

    Keyset-paginated on ``(disconnected_at, id)`` to match ``ORDER BY
    disconnected_at DESC, id DESC``, the same shape ``search_translations``
    uses. Returns ``(rows, next_cursor)`` (encoded token or ``None``).

    OFFSET paging was wrong here, not merely slow: a blip recorded while the
    operator is reading shifts every later page by one, so the next page
    repeats a row or skips one. Blips cluster *during* an outage — exactly
    when the history is being read to count "how many times did it drop".
    """
    conditions: list[str] = []
    params: list = []

    def add(value) -> str:
        params.append(value)
        return f"${len(params)}"

    if session_id is not None:
        conditions.append(f"session_id = {add(session_id)}")
    if cursor is not None:
        cur_t, cur_i = cursor
        conditions.append(f"(disconnected_at, id) < ({add(cur_t)}, {add(cur_i)})")

    where = ("WHERE " + " AND ".join(conditions) + " ") if conditions else ""
    limit_p = add(limit)
    sql = (
        f"SELECT {_BLIP_COLS} FROM app.ws_blips {where}"
        f"ORDER BY disconnected_at DESC, id DESC LIMIT {limit_p}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = encode_search_cursor(last["disconnected_at"], last["id"])
    return list(rows), next_cursor
