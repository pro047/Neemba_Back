"""asyncpg write for ``app.client_diagnostics`` (client-diagnostics M1).

One statement per batch. ``ON CONFLICT DO NOTHING`` on ``(install_id, seq)``
makes a resend after an ambiguous 503 harmless. Every value reaches SQL as a
bound parameter.
"""
from __future__ import annotations

from collections.abc import Sequence

from src.diagnostics.client_logs import ParsedEntry

# Explicit casts on $1/$7: in a SELECT list Postgres has no column to infer
# their type from.
_INSERT_ENTRIES_SQL = (
    "INSERT INTO app.client_diagnostics "
    "(install_id, seq, line, line_truncated, client_ts, event, app_version) "
    "SELECT $1::text, u.seq, u.line, u.trunc, u.ts, u.ev, $7::text "
    "FROM unnest($2::bigint[], $3::text[], $4::boolean[], $5::timestamptz[], $6::text[]) "
    "AS u(seq, line, trunc, ts, ev) "
    "ON CONFLICT (install_id, seq) DO NOTHING "
    "RETURNING seq"
)


async def insert_entries(
    pool,
    *,
    install_id: str,
    app_version: str | None,
    entries: Sequence[ParsedEntry],
) -> int:
    """Insert a batch and return how many rows were actually new."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _INSERT_ENTRIES_SQL,
            install_id,
            [e.seq for e in entries],
            [e.line for e in entries],
            [e.line_truncated for e in entries],
            [e.client_ts for e in entries],
            [e.event for e in entries],
            app_version,
        )
    return len(rows)
