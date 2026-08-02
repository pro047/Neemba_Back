"""asyncpg-backed reads/writes for ``app.ws_blips`` (handover §4-7).

One row per /ws disconnect: inserted when the hub loses the client, closed in
place on reconnect. Never called on the translation hot path directly — the
hub goes through :class:`src.ws.blip_recorder.WsBlipRecorder`, which isolates
DB failures. Every value reaches SQL as a bound parameter (``$n``).
"""
from __future__ import annotations

# --- limits (route-level clamp, sessions 와 동일 규약) ----------------------

BLIPS_LIMIT_DEFAULT = 50
BLIPS_LIMIT_MAX = 200

# --- SQL -------------------------------------------------------------------

_INSERT_BLIP_SQL = (
    "INSERT INTO app.ws_blips (session_id, close_code, close_reason, detected_by) "
    "VALUES ($1, $2, $3, $4) "
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
    "id, session_id, disconnected_at, reconnected_at, duration_ms, "
    "flushed_count, lost_count, close_code, close_reason, detected_by"
)


# --- queries ---------------------------------------------------------------

async def insert_blip(
    pool,
    *,
    session_id: str,
    close_code: int | None = None,
    close_reason: str | None = None,
    detected_by: str,
) -> int | None:
    """Record a disconnect and return the new blip id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_BLIP_SQL, session_id, close_code, close_reason, detected_by
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
    offset: int = 0,
) -> tuple[list, int | None]:
    """List blips newest-first, optionally for one session.

    OFFSET-paginated like ``list_sessions``: returns ``(rows, next_offset)``
    where ``next_offset`` is ``offset + limit`` when a full page came back.
    """
    params: list = []
    where = ""
    if session_id is not None:
        params.append(session_id)
        where = f"WHERE session_id = ${len(params)} "
    params.append(limit)
    limit_p = f"${len(params)}"
    params.append(offset)
    offset_p = f"${len(params)}"
    sql = (
        f"SELECT {_BLIP_COLS} FROM app.ws_blips {where}"
        f"ORDER BY disconnected_at DESC, id DESC "
        f"LIMIT {limit_p} OFFSET {offset_p}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    next_offset = offset + limit if len(rows) == limit else None
    return list(rows), next_offset
