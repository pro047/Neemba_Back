"""Read-side (history) queries for the monitoring page (``app.*`` tables).

Phase 5 of docs/monitoring-plan.md. These back the three history endpoints in
``main.py``:

- ``GET /api/monitor/sessions``                       → :func:`list_sessions`
- ``GET /api/monitor/sessions/{id}/translations``     → :func:`list_session_translations`
- ``GET /api/monitor/translations``                   → :func:`search_translations`

All functions take an :class:`asyncpg.Pool` and are pure async read helpers.
Every value that reaches SQL is passed as a bound parameter (``$1``, ``$2`` …)
— never string-formatted — so user input (``q``, ``lang``, cursors, ranges)
cannot inject. The keyword search ``q`` runs ``ILIKE`` against the *already
masked* ``source_text``/``translated_text`` (the raw PII never lands in the DB;
see §6), so it can never surface unmasked sensitive data.

Pagination notes:
- Sessions use simple ``OFFSET`` paging (ordered ``started_at DESC``); the row
  count is small and bounded.
- Per-session translations use a **stable keyset cursor on the PK ``id``**
  (``ORDER BY id ASC``, ``WHERE id > $cursor``) because ``sequence`` is
  nullable and non-unique (docs §10 Phase 3 / Phase 5 brief) — id is the only
  stable, total order.
- Search uses a compound keyset cursor on ``(created_at, id)`` to match the
  ``created_at DESC, id DESC`` ordering and the ``ix_translations_created_at``
  index.

Each list helper returns ``(rows, next_cursor)`` where ``next_cursor`` is
``None`` once the last page has been reached, so the route layer stays thin and
the cursor maths are unit-testable without a route/TestClient.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime

# --- limit / cursor helpers (pure, unit-tested) ----------------------------

# Per-endpoint defaults/caps (docs §7 Phase 5 brief).
SESSIONS_LIMIT_DEFAULT = 50
SESSIONS_LIMIT_MAX = 200
SESSION_TRANSLATIONS_LIMIT_DEFAULT = 100
SESSION_TRANSLATIONS_LIMIT_MAX = 500
SEARCH_LIMIT_DEFAULT = 100
SEARCH_LIMIT_MAX = 500


def clamp_limit(raw: int | None, *, default: int, maximum: int) -> int:
    """Clamp a client-supplied ``limit`` into ``[1, maximum]``.

    ``None`` (omitted) falls back to ``default``. Out-of-range values are
    safely clamped rather than rejected (docs §7 Phase 5: "안전 클램프").
    """
    if raw is None:
        return default
    if raw < 1:
        return 1
    if raw > maximum:
        return maximum
    return raw


def clamp_offset(raw: int | None) -> int:
    """Clamp a client-supplied ``offset`` to ``>= 0`` (``None`` → 0)."""
    if raw is None or raw < 0:
        return 0
    return raw


def parse_int_cursor(cursor: str | None) -> int | None:
    """Parse an integer (``id``) cursor; raise ``ValueError`` if malformed."""
    if cursor is None or cursor == "":
        return None
    return int(cursor)


def encode_search_cursor(created_at: datetime, id_: int) -> str:
    """Encode a ``(created_at, id)`` keyset position into an opaque token."""
    raw = json.dumps({"t": created_at.isoformat(), "i": int(id_)})
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_search_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    """Decode a search cursor token; raise ``ValueError`` if malformed."""
    if cursor is None or cursor == "":
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        obj = json.loads(raw)
        return datetime.fromisoformat(obj["t"]), int(obj["i"])
    except (binascii.Error, ValueError, KeyError, TypeError) as e:
        # Route layer turns this into a 422; message lives there.
        raise ValueError from e


# --- SQL -------------------------------------------------------------------

_LIST_SESSIONS_SQL = (
    "SELECT session_id, started_at, ended_at, source_lang, target_lang, "
    "translation_count "
    "FROM app.sessions "
    "ORDER BY started_at DESC, session_id DESC "
    "LIMIT $1 OFFSET $2"
)

_TRANSLATION_COLS = (
    "id, session_id, segment_id, sequence, source_text, translated_text, "
    "source_lang, target_lang, confidence, created_at"
)


# --- queries ---------------------------------------------------------------

async def list_sessions(
    pool,
    *,
    limit: int = SESSIONS_LIMIT_DEFAULT,
    offset: int = 0,
) -> tuple[list, int | None]:
    """List sessions newest-first. Returns ``(rows, next_offset)``.

    ``next_offset`` is ``offset + limit`` when a full page came back (more may
    exist), else ``None``.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LIST_SESSIONS_SQL, limit, offset)
    next_offset = offset + limit if len(rows) == limit else None
    return list(rows), next_offset


async def list_session_translations(
    pool,
    session_id: str,
    *,
    limit: int = SESSION_TRANSLATIONS_LIMIT_DEFAULT,
    cursor: int | None = None,
) -> tuple[list, int | None]:
    """List one session's pairs ordered by the stable PK ``id``.

    Keyset paginated: ``WHERE id > $cursor ORDER BY id ASC``. Returns
    ``(rows, next_cursor)`` where ``next_cursor`` is the last row's ``id`` when
    a full page came back, else ``None``.
    """
    params: list = [session_id]
    where = "WHERE session_id = $1"
    if cursor is not None:
        params.append(cursor)
        where += f" AND id > ${len(params)}"
    params.append(limit)
    sql = (
        f"SELECT {_TRANSLATION_COLS} FROM app.translations "
        f"{where} ORDER BY id ASC LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    next_cursor = rows[-1]["id"] if len(rows) == limit else None
    return list(rows), next_cursor


async def search_translations(
    pool,
    *,
    lang: str | None = None,
    dt_from: datetime | None = None,
    dt_to: datetime | None = None,
    q: str | None = None,
    cursor: tuple[datetime, int] | None = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> tuple[list, str | None]:
    """Search pairs by language / created_at range / keyword, newest-first.

    Filters (all optional, AND-combined):
    - ``lang``     → matches **either** ``source_lang`` **or** ``target_lang``.
    - ``dt_from``  → ``created_at >= dt_from``.
    - ``dt_to``    → ``created_at <= dt_to``.
    - ``q``        → ``ILIKE '%q%'`` over the masked ``source_text`` OR
                     ``translated_text`` (bound param; injection-safe).

    Keyset paginated on ``(created_at, id)`` to match ``ORDER BY created_at
    DESC, id DESC``. Returns ``(rows, next_cursor)`` (encoded token or ``None``).
    """
    conditions: list[str] = []
    params: list = []

    def add(value) -> str:
        params.append(value)
        return f"${len(params)}"

    if lang:
        p = add(lang)
        conditions.append(f"(source_lang = {p} OR target_lang = {p})")
    if dt_from is not None:
        conditions.append(f"created_at >= {add(dt_from)}")
    if dt_to is not None:
        conditions.append(f"created_at <= {add(dt_to)}")
    if q:
        p = add(f"%{q}%")
        conditions.append(f"(source_text ILIKE {p} OR translated_text ILIKE {p})")
    if cursor is not None:
        cur_t, cur_i = cursor
        conditions.append(f"(created_at, id) < ({add(cur_t)}, {add(cur_i)})")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    limit_p = add(limit)
    sql = (
        f"SELECT {_TRANSLATION_COLS} FROM app.translations "
        f"{where} ORDER BY created_at DESC, id DESC LIMIT {limit_p}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = encode_search_cursor(last["created_at"], last["id"])
    return list(rows), next_cursor
