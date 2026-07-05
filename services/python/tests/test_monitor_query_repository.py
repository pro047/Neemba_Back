"""Unit tests for the Phase 5 read-side query repository.

These cover the pure cursor/limit helpers plus the query builders, asserting
the generated SQL + bound params (no real DB). The builders never string-format
user input into SQL — every value goes through ``$n`` placeholders — and these
tests pin that contract along with cursor boundaries, limit clamping, the
lang/from/to/q filter combinations and empty-result handling.
"""
from datetime import UTC, datetime

import pytest

from src.repository.implementation import monitor_query_repository as mq

# --- fake asyncpg pool ------------------------------------------------------

class _CapturingConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []  # list of (sql, args)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows=None):
        self.conn = _CapturingConn(rows or [])

    def acquire(self):
        return _Acquire(self.conn)

    @property
    def last_sql(self):
        return self.conn.calls[-1][0]

    @property
    def last_args(self):
        return self.conn.calls[-1][1]


def _row(**kw):
    """A dict that supports ``r["id"]`` like an asyncpg Record and ``dict(r)``."""
    return kw


# --- clamp_limit ------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (None, 50),     # omitted → default
    (0, 1),         # below floor → 1
    (-5, 1),        # negative → 1
    (10, 10),       # within range → unchanged
    (200, 200),     # at cap → unchanged
    (999, 200),     # above cap → clamped
])
def test_clamp_limit(raw, expected):
    assert mq.clamp_limit(raw, default=50, maximum=200) == expected


def test_clamp_offset():
    assert mq.clamp_offset(None) == 0
    assert mq.clamp_offset(-1) == 0
    assert mq.clamp_offset(0) == 0
    assert mq.clamp_offset(25) == 25


# --- int cursor -------------------------------------------------------------

def test_parse_int_cursor():
    assert mq.parse_int_cursor(None) is None
    assert mq.parse_int_cursor("") is None
    assert mq.parse_int_cursor("42") == 42


def test_parse_int_cursor_bad():
    with pytest.raises(ValueError):
        mq.parse_int_cursor("not-an-int")


# --- search cursor roundtrip ------------------------------------------------

def test_search_cursor_roundtrip():
    ts = datetime(2026, 6, 4, 12, 30, 5, tzinfo=UTC)
    token = mq.encode_search_cursor(ts, 123)
    decoded = mq.decode_search_cursor(token)
    assert decoded == (ts, 123)


def test_decode_search_cursor_empty():
    assert mq.decode_search_cursor(None) is None
    assert mq.decode_search_cursor("") is None


@pytest.mark.parametrize("bad", ["!!!notbase64!!!", "YWJj", "eyJ0IjoxfQ=="])
def test_decode_search_cursor_bad(bad):
    # garbage, valid-base64-but-not-json, and json-missing-keys all → ValueError
    with pytest.raises(ValueError):
        mq.decode_search_cursor(bad)


# --- list_sessions ----------------------------------------------------------

async def test_list_sessions_sql_and_params():
    pool = _FakePool(rows=[_row(session_id="s") for _ in range(50)])
    rows, next_offset = await mq.list_sessions(pool, limit=50, offset=0)
    sql = pool.last_sql
    assert "FROM app.sessions" in sql
    assert "ORDER BY started_at DESC, session_id DESC" in sql
    assert "LIMIT $1 OFFSET $2" in sql
    assert pool.last_args == (50, 0)
    # full page ⇒ there may be more
    assert next_offset == 50
    assert len(rows) == 50


async def test_list_sessions_last_page_no_next():
    pool = _FakePool(rows=[_row(session_id="s") for _ in range(3)])
    rows, next_offset = await mq.list_sessions(pool, limit=50, offset=100)
    assert pool.last_args == (50, 100)
    assert next_offset is None  # partial page ⇒ end


async def test_list_sessions_empty():
    pool = _FakePool(rows=[])
    rows, next_offset = await mq.list_sessions(pool, limit=50, offset=0)
    assert rows == []
    assert next_offset is None


# --- list_session_translations ---------------------------------------------

async def test_session_translations_no_cursor():
    pool = _FakePool(rows=[_row(id=i) for i in range(1, 11)])
    rows, next_cursor = await mq.list_session_translations(pool, "sess", limit=10)
    sql = pool.last_sql
    assert "WHERE session_id = $1" in sql
    assert "id >" not in sql            # no cursor clause
    assert "ORDER BY id ASC" in sql
    assert pool.last_args == ("sess", 10)
    assert next_cursor == 10            # full page ⇒ last id


async def test_session_translations_with_cursor_boundary():
    pool = _FakePool(rows=[_row(id=i) for i in range(6, 11)])
    rows, next_cursor = await mq.list_session_translations(
        pool, "sess", limit=5, cursor=5
    )
    sql = pool.last_sql
    # stable keyset: strictly greater than the cursor id
    assert "AND id > $2" in sql
    assert "ORDER BY id ASC" in sql
    assert pool.last_args == ("sess", 5, 5)
    assert next_cursor == 10


async def test_session_translations_last_page():
    pool = _FakePool(rows=[_row(id=i) for i in range(1, 4)])
    rows, next_cursor = await mq.list_session_translations(pool, "sess", limit=10)
    assert next_cursor is None         # partial page ⇒ no next cursor


async def test_session_translations_empty():
    pool = _FakePool(rows=[])
    rows, next_cursor = await mq.list_session_translations(pool, "sess", limit=10)
    assert rows == []
    assert next_cursor is None


# --- search_translations ----------------------------------------------------

async def test_search_no_filters():
    pool = _FakePool(rows=[])
    rows, next_cursor = await mq.search_translations(pool, limit=100)
    sql = pool.last_sql
    assert "WHERE" not in sql
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert pool.last_args == (100,)    # only the LIMIT bind
    assert rows == []
    assert next_cursor is None


async def test_search_lang_matches_either_side():
    pool = _FakePool(rows=[])
    await mq.search_translations(pool, lang="ko", limit=100)
    sql = pool.last_sql
    assert "(source_lang = $1 OR target_lang = $1)" in sql
    assert pool.last_args == ("ko", 100)


async def test_search_date_range():
    pool = _FakePool(rows=[])
    f = datetime(2026, 6, 1, tzinfo=UTC)
    t = datetime(2026, 6, 4, tzinfo=UTC)
    await mq.search_translations(pool, dt_from=f, dt_to=t, limit=100)
    sql = pool.last_sql
    assert "created_at >= $1" in sql
    assert "created_at <= $2" in sql
    assert pool.last_args == (f, t, 100)


async def test_search_q_is_bound_ilike_both_columns():
    pool = _FakePool(rows=[])
    await mq.search_translations(pool, q="hello", limit=100)
    sql = pool.last_sql
    assert "source_text ILIKE $1 OR translated_text ILIKE $1" in sql
    # bound param carries the wildcards, never string-formatted into SQL
    assert pool.last_args == ("%hello%", 100)
    assert "hello" not in sql


async def test_search_q_injection_is_bound_not_formatted():
    pool = _FakePool(rows=[])
    evil = "'; DROP TABLE app.translations; --"
    await mq.search_translations(pool, q=evil, limit=100)
    assert "DROP TABLE" not in pool.last_sql       # not in SQL text
    assert pool.last_args == (f"%{evil}%", 100)    # safely bound


async def test_search_all_filters_combined_param_order():
    pool = _FakePool(rows=[])
    f = datetime(2026, 6, 1, tzinfo=UTC)
    t = datetime(2026, 6, 4, tzinfo=UTC)
    cur_t = datetime(2026, 6, 3, tzinfo=UTC)
    await mq.search_translations(
        pool, lang="ko", dt_from=f, dt_to=t, q="hi", cursor=(cur_t, 999), limit=50
    )
    sql = pool.last_sql
    assert "(source_lang = $1 OR target_lang = $1)" in sql
    assert "created_at >= $2" in sql
    assert "created_at <= $3" in sql
    assert "source_text ILIKE $4 OR translated_text ILIKE $4" in sql
    assert "(created_at, id) < ($5, $6)" in sql
    assert "LIMIT $7" in sql
    assert pool.last_args == ("ko", f, t, "%hi%", cur_t, 999, 50)


async def test_search_next_cursor_on_full_page():
    last_ts = datetime(2026, 6, 4, 9, 0, 0, tzinfo=UTC)
    rows = [_row(id=i, created_at=last_ts) for i in range(3)]
    pool = _FakePool(rows=rows)
    _, next_cursor = await mq.search_translations(pool, limit=3)
    assert next_cursor is not None
    # the cursor decodes back to the last row's (created_at, id)
    assert mq.decode_search_cursor(next_cursor) == (last_ts, rows[-1]["id"])


async def test_search_no_next_cursor_on_partial_page():
    rows = [_row(id=i, created_at=datetime.now(UTC)) for i in range(2)]
    pool = _FakePool(rows=rows)
    _, next_cursor = await mq.search_translations(pool, limit=100)
    assert next_cursor is None
