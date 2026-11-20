"""client-diagnostics M1: POST /api/diagnostics/client-logs.

Design: .pipeline/client-diagnostics/DESIGN.md §3 (interfaces), §4 (contract), §6 (AC-1 ~ AC-14).

- Pure functions (src/diagnostics/client_logs.py) are tested directly.
- HTTP-level ACs go through httpx.ASGITransport(app=main.app) only ([결정] 6).
  The DB pool is injected with app.dependency_overrides[main.get_db_pool].
- DB behaviour (idempotency, truncation, parsed columns) uses the real test DB
  (conftest ``pg_pool``) plus a local fixture that truncates app.client_diagnostics.
- Failure injection (AC-3b, AC-10, AC-12, AC-13) uses fake pools: a dead / hanging
  DB cannot be produced deterministically from the shared test container.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import main
from src.diagnostics import client_logs as cl
from src.repository.implementation import client_diagnostics_repository as repo

SERVICE_ROOT = Path(__file__).resolve().parents[1]
URL = "/api/diagnostics/client-logs"
INSTALL_ID = "3f1c2a9e-7b4d-4e2a-9c1f-0a2b3c4d5e6f"
OTHER_INSTALL_ID = "0b7e1d2c-1111-4a2b-8c3d-9e8f7a6b5c4d"


# --- helpers ----------------------------------------------------------------


@contextlib.asynccontextmanager
async def _client(pool):
    main.app.dependency_overrides[main.get_db_pool] = lambda: pool
    try:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        main.app.dependency_overrides.pop(main.get_db_pool, None)


def _body(entries, *, install_id=INSTALL_ID, app_version: str | None = "1.4.2+37", **extra) -> dict:
    body = {"installId": install_id, "entries": entries, **extra}
    if app_version is not None:
        body["appVersion"] = app_version
    return body


async def _rows(pool):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT install_id, seq, line, line_truncated, client_ts, event, "
            "app_version, received_at FROM app.client_diagnostics ORDER BY install_id, seq"
        )


async def _count(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM app.client_diagnostics")


def _assert_no_internal_leak(res: httpx.Response, *markers: str) -> None:
    text = res.text
    for m in markers:
        assert m not in text
    assert "Traceback" not in text
    assert "site-packages" not in text
    assert ".py" not in text
    assert "INSERT" not in text
    # fixed message body: {"detail": "<str>"} — not FastAPI's default error list
    assert isinstance(res.json()["detail"], str)


async def _chunks(*parts: bytes, consumed: list | None = None):
    for p in parts:
        if consumed is not None:
            consumed.append(p)
        yield p


def _padded_body_bytes(total: int) -> bytes:
    """Valid upload JSON whose UTF-8 serialization is exactly ``total`` bytes.

    Padding goes into an unknown top-level field (extra="ignore")."""
    base = {"installId": INSTALL_ID, "entries": [{"seq": 1, "line": "2026-11-15T02:09:00.123Z pad"}], "pad": ""}
    empty = len(json.dumps(base).encode("utf-8"))
    base["pad"] = "x" * (total - empty)
    raw = json.dumps(base).encode("utf-8")
    assert len(raw) == total
    return raw


@pytest.fixture
async def diag_pool(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE app.client_diagnostics RESTART IDENTITY")
    return pg_pool


# --- fake pools for failure injection ----------------------------------------


class _UntouchablePool:
    """Records any use; the route must not reach the DB."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        msg = f"pool.{name} must not be used"
        raise AssertionError(msg)


class _ExplodingPool:
    """acquire() (and any direct pool call) raises a secret-bearing error."""

    def acquire(self, *a, **k):
        msg = "SECRET-xyz /srv/app/secret_path.py"
        raise RuntimeError(msg)

    def __getattr__(self, name):
        def _boom(*a, **k):
            msg = "SECRET-xyz /srv/app/secret_path.py"
            raise RuntimeError(msg)

        return _boom


async def _forever():
    await asyncio.Event().wait()


class _HangingAcquire:
    def __init__(self, pool=None):
        self._pool = pool

    async def __aenter__(self):
        if self._pool is not None:
            self._pool.enter()
        try:
            await _forever()
        finally:
            if self._pool is not None:
                self._pool.leave()

    async def __aexit__(self, *exc):
        return False

    def __await__(self):
        return self.__aenter__().__await__()


class _HangingPool:
    """acquire() never completes; counts concurrent acquires."""

    def __init__(self):
        self.current = 0
        self.max_seen = 0

    def enter(self):
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)

    def leave(self):
        self.current -= 1

    def acquire(self, *a, **k):
        return _HangingAcquire(self)

    def __getattr__(self, name):
        async def _hang(*a, **k):
            self.enter()
            try:
                await _forever()
            finally:
                self.leave()

        return _hang


# --- contract constants (AC-2, AC-3, AC-6 boundaries) ------------------------


def test_contract_limits_match_design_values():
    # AC-2 AC-3 AC-6 — boundary constants are part of the approved contract
    assert cl.MAX_BODY_BYTES == 2_097_152
    assert cl.MAX_ENTRIES == 10_000
    assert cl.MAX_LINE_CHARS == 4_000
    assert cl.MAX_APP_VERSION_CHARS == 64
    assert cl.MAX_EVENT_CHARS == 64
    assert cl.SEQ_MAX == 2**63 - 1
    assert cl.RECOMMENDED_BATCH_ENTRIES == 5_000
    assert cl.RECOMMENDED_BATCH_BYTES == 1_048_576


# --- AC-1: idempotency on (installId, seq) -----------------------------------


async def test_same_install_and_seq_posted_twice_with_uppercase_id_keeps_one_lowercase_row(diag_pool):
    # AC-1
    # Arrange
    entries = [{"seq": 1041, "line": "2026-11-15T02:09:00.123Z ws_close code=1006 reason=<empty>"}]
    async with _client(diag_pool) as c:
        # Act
        first = await c.post(URL, json=_body(entries))
        second = await c.post(URL, json=_body(entries, install_id=INSTALL_ID.upper()))

    # Assert
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"ackedMaxSeq": 1041, "inserted": 1, "duplicates": 0, "truncated": 0}
    assert second.json() == {"ackedMaxSeq": 1041, "inserted": 0, "duplicates": 1, "truncated": 0}
    rows = await _rows(diag_pool)
    assert len(rows) == 1
    assert rows[0]["install_id"] == INSTALL_ID


async def test_same_seq_under_different_install_ids_stores_two_rows(diag_pool):
    # AC-1 — uniqueness is per (installId, seq), not per seq
    # Arrange
    entries = [{"seq": 7, "line": "2026-11-15T02:09:00.123Z x"}]
    async with _client(diag_pool) as c:
        # Act
        a = await c.post(URL, json=_body(entries))
        b = await c.post(URL, json=_body(entries, install_id=OTHER_INSTALL_ID))

    # Assert
    assert (a.status_code, b.status_code) == (200, 200)
    assert b.json()["inserted"] == 1
    rows = await _rows(diag_pool)
    assert sorted(r["install_id"] for r in rows) == sorted([INSTALL_ID, OTHER_INSTALL_ID])


async def test_concurrent_uploads_of_same_batch_store_one_row_and_insert_once_in_total(diag_pool):
    # AC-1 — retransmit racing the original (e.g. after a 503) must not duplicate
    # Arrange
    entries = [{"seq": 5, "line": "2026-11-15T02:09:00.123Z ws_open"}]
    async with _client(diag_pool) as c:
        # Act
        r1, r2 = await asyncio.gather(c.post(URL, json=_body(entries)), c.post(URL, json=_body(entries)))

    # Assert
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert r1.json()["inserted"] + r2.json()["inserted"] == 1
    assert r1.json()["duplicates"] + r2.json()["duplicates"] == 1
    assert await _count(diag_pool) == 1


async def test_insert_entries_returns_new_row_count_and_zero_on_repeat(diag_pool):
    # AC-1 — repository: ON CONFLICT (install_id, seq) DO NOTHING
    # Arrange
    parsed = [
        cl.ParsedEntry(seq=1, line="a", line_truncated=False, client_ts=None, event=None),
        cl.ParsedEntry(seq=2, line="b", line_truncated=True,
                       client_ts=datetime(2026, 11, 15, 2, 9, tzinfo=UTC), event="ws_open"),
    ]

    # Act
    first = await repo.insert_entries(diag_pool, install_id=INSTALL_ID, app_version="1.0", entries=parsed)
    again = await repo.insert_entries(diag_pool, install_id=INSTALL_ID, app_version="1.0", entries=parsed)

    # Assert
    assert first == 2
    assert again == 0
    rows = await _rows(diag_pool)
    assert [(r["seq"], r["line"], r["line_truncated"], r["event"], r["app_version"]) for r in rows] == [
        (1, "a", False, None, "1.0"),
        (2, "b", True, "ws_open", "1.0"),
    ]
    assert rows[1]["client_ts"] == datetime(2026, 11, 15, 2, 9, tzinfo=UTC)
    assert rows[0]["received_at"] is not None


# --- AC-2: body byte cap -------------------------------------------------------


async def test_body_one_byte_over_max_is_rejected_with_413_and_stores_nothing(diag_pool):
    # AC-2 (a)
    # Arrange
    raw = _padded_body_bytes(cl.MAX_BODY_BYTES + 1)
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, content=raw, headers={"Content-Type": "application/json"})

    # Assert
    assert res.status_code == 413
    assert await _count(diag_pool) == 0


async def test_body_exactly_max_bytes_is_accepted(diag_pool):
    # AC-2 — boundary: exactly MAX_BODY_BYTES is not too large
    # Arrange
    raw = _padded_body_bytes(cl.MAX_BODY_BYTES)
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, content=raw, headers={"Content-Type": "application/json"})

    # Assert
    assert res.status_code == 200
    assert await _count(diag_pool) == 1


async def test_chunked_body_over_max_without_content_length_is_rejected_with_413():
    # AC-2 — streaming check applies when Content-Length is absent
    # Arrange
    pool = _UntouchablePool()
    raw = _padded_body_bytes(cl.MAX_BODY_BYTES + 1)

    async def gen():
        for i in range(0, len(raw), 65_536):
            yield raw[i:i + 65_536]

    async with _client(pool) as c:
        # Act
        res = await c.post(URL, content=gen(), headers={"Content-Type": "application/json"})

    # Assert
    assert "content-length" not in {k.lower() for k in res.request.headers}
    assert res.status_code == 413
    assert pool.touched == []


async def test_read_body_capped_rejects_large_content_length_before_consuming_any_chunk():
    # AC-2 (b)
    # Arrange
    consumed: list[bytes] = []
    chunks = _chunks(b"{}", consumed=consumed)

    # Act / Assert
    with pytest.raises(cl.PayloadTooLarge):
        await cl.read_body_capped(chunks, content_length="2097153")
    assert consumed == []


async def test_read_body_capped_rejects_stream_one_byte_over_limit_without_content_length():
    # AC-2 (c)
    # Arrange
    half = cl.MAX_BODY_BYTES // 2
    chunks = _chunks(b"a" * half, b"b" * (cl.MAX_BODY_BYTES - half), b"c")

    # Act / Assert
    with pytest.raises(cl.PayloadTooLarge):
        await cl.read_body_capped(chunks, content_length=None)


async def test_read_body_capped_accepts_stream_of_exactly_limit_bytes():
    # AC-2 (c)
    # Arrange
    half = cl.MAX_BODY_BYTES // 2
    a, b = b"a" * half, b"b" * (cl.MAX_BODY_BYTES - half)

    # Act
    out = await cl.read_body_capped(_chunks(a, b), content_length=None)

    # Assert
    assert out == a + b


async def test_read_body_capped_stops_consuming_once_limit_is_exceeded():
    # AC-2 — "stop consuming" after the cap is crossed
    # Arrange
    consumed: list[bytes] = []
    chunks = _chunks(b"x" * 11, b"never-read", consumed=consumed)

    # Act / Assert
    with pytest.raises(cl.PayloadTooLarge):
        await cl.read_body_capped(chunks, content_length=None, limit=10)
    assert consumed == [b"x" * 11]


async def test_read_body_capped_ignores_unparseable_content_length():
    # AC-2 — unparseable Content-Length is ignored, streaming check still applies
    # Arrange / Act
    out = await cl.read_body_capped(_chunks(b"abc", b"def"), content_length="not-a-number", limit=10)

    # Assert
    assert out == b"abcdef"


async def test_read_body_capped_with_small_honest_content_length_reads_all_chunks():
    # AC-2 — a Content-Length within the limit does not short-circuit
    # Arrange / Act
    out = await cl.read_body_capped(_chunks(b"ab", b"cd"), content_length="4", limit=10)

    # Assert
    assert out == b"abcd"


# --- AC-3: entries count cap ---------------------------------------------------


def _entries_raw(n: int, *, install_id=INSTALL_ID, seq_start=0) -> bytes:
    entries = [{"seq": seq_start + i, "line": "x"} for i in range(n)]
    return json.dumps({"installId": install_id, "entries": entries}).encode("utf-8")


def test_decode_upload_rejects_one_entry_over_max_with_payload_too_large():
    # AC-3 (a)
    # Arrange
    raw = _entries_raw(cl.MAX_ENTRIES + 1)

    # Act / Assert
    with pytest.raises(cl.PayloadTooLarge):
        cl.decode_upload(raw)


def test_decode_upload_accepts_exactly_max_entries():
    # AC-3 (a)
    # Arrange
    raw = _entries_raw(cl.MAX_ENTRIES)

    # Act
    upload = cl.decode_upload(raw)

    # Assert
    assert len(upload.entries) == cl.MAX_ENTRIES
    assert str(upload.install_id) == INSTALL_ID


def test_decode_upload_checks_entry_count_before_schema_validation():
    # AC-3 — count check runs after json.loads and before pydantic: invalid id still yields 413
    # Arrange
    raw = _entries_raw(cl.MAX_ENTRIES + 1, install_id="abc", seq_start=-5)

    # Act / Assert
    with pytest.raises(cl.PayloadTooLarge):
        cl.decode_upload(raw)


async def test_http_batch_one_entry_over_max_is_rejected_with_413_without_touching_db():
    # AC-3 (b)
    # Arrange
    raw = _entries_raw(cl.MAX_ENTRIES + 1)
    assert len(raw) <= cl.MAX_BODY_BYTES
    pool = _UntouchablePool()
    async with _client(pool) as c:
        # Act
        res = await c.post(URL, content=raw, headers={"Content-Type": "application/json"})

    # Assert
    assert res.status_code == 413
    assert pool.touched == []


# --- AC-4: line head parsing ---------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected_ts", "expected_event"),
    [
        ("2026-11-18T10:18:15.988504Z ws_open a=b",
         datetime(2026, 11, 18, 10, 18, 15, 988504, tzinfo=UTC), "ws_open"),
        ("2026-11-15T02:09:00.123Z ws_close code=1006",
         datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC), "ws_close"),
        ("2026-11-15T11:09:00.123+09:00 x",
         datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC), "x"),
        ("garbage line", None, None),
        ("2026-11-15T02:09:00.123 x", None, None),
        ("2026-11-15T02:09:00.123Z",
         datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC), None),
        ("2026-11-15T02:09:00.123Z " + "e" * 65,
         datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC), None),
        ("2026-11-15T02:09:00.123Z " + "e" * 64 + " k=v",
         datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC), "e" * 64),
        # split(" ", 2): a double space leaves an empty 2nd token -> event None
        ("2026-11-15T02:09:00.123Z  ws_open",
         datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC), None),
        ("", None, None),
        (" leading-space ws_open", None, None),
    ],
    ids=["us-z", "ms-z", "plus9-to-utc", "garbage", "naive", "no-event",
         "event-65", "event-64", "double-space", "empty", "leading-space"],
)
def test_parse_line_head_returns_utc_ts_and_event_per_contract(line, expected_ts, expected_event):
    # AC-4
    # Arrange / Act
    ts, event = cl.parse_line_head(line)

    # Assert
    assert ts == expected_ts
    if expected_ts is not None:
        assert ts.utcoffset() == timedelta(0)
    assert event == expected_event


def test_normalize_entries_parses_head_into_client_ts_and_event():
    # AC-4 — normalize_entries wires parse_line_head into ParsedEntry
    # Arrange
    entries = [cl.ClientLogEntry(seq=3, line="2026-11-15T11:09:00.123+09:00 ws_retry attempt=1")]

    # Act
    [p] = cl.normalize_entries(entries)

    # Assert
    assert p == cl.ParsedEntry(
        seq=3,
        line="2026-11-15T11:09:00.123+09:00 ws_retry attempt=1",
        line_truncated=False,
        client_ts=datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC),
        event="ws_retry",
    )


# --- AC-5: unparseable line is still stored ------------------------------------


async def test_unparseable_line_is_stored_verbatim_with_null_ts_and_event(diag_pool):
    # AC-5
    # Arrange
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, json=_body([{"seq": 1, "line": "garbage line"}]))

    # Assert
    assert res.status_code == 200
    [row] = await _rows(diag_pool)
    assert row["line"] == "garbage line"
    assert row["client_ts"] is None
    assert row["event"] is None
    assert row["line_truncated"] is False


async def test_parseable_line_is_stored_with_utc_ts_event_and_app_version(diag_pool):
    # AC-5 AC-4 — happy path columns at DB level
    # Arrange
    line = "2026-11-15T11:09:00.123+09:00 ws_close code=1006 reason=<empty>"
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, json=_body([{"seq": 1041, "line": line}], app_version="1.4.2+37"))

    # Assert
    assert res.status_code == 200
    [row] = await _rows(diag_pool)
    assert row["line"] == line
    assert row["client_ts"] == datetime(2026, 11, 15, 2, 9, 0, 123000, tzinfo=UTC)
    assert row["event"] == "ws_close"
    assert row["app_version"] == "1.4.2+37"
    assert row["received_at"] is not None


async def test_missing_or_null_app_version_is_stored_as_null(diag_pool):
    # AC-5 — appVersion is optional
    # Arrange
    async with _client(diag_pool) as c:
        # Act
        r1 = await c.post(URL, json=_body([{"seq": 1, "line": "a"}], app_version=None))
        r2 = await c.post(URL, json={"installId": INSTALL_ID, "appVersion": None, "entries": [{"seq": 2, "line": "b"}]})

    # Assert
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert [r["app_version"] for r in await _rows(diag_pool)] == [None, None]


# --- AC-6: over-long line is truncated, not rejected ---------------------------


async def test_line_over_max_chars_is_truncated_and_flagged_and_4000_is_kept(diag_pool):
    # AC-6
    # Arrange
    prefix = "2026-11-15T02:09:00.123Z ws_close "
    long_line = prefix + "a" * (cl.MAX_LINE_CHARS + 1 - len(prefix))
    exact_line = prefix + "b" * (cl.MAX_LINE_CHARS - len(prefix))
    assert (len(long_line), len(exact_line)) == (4001, 4000)
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, json=_body([{"seq": 1, "line": long_line}, {"seq": 2, "line": exact_line}]))

    # Assert
    assert res.status_code == 200
    assert res.json() == {"ackedMaxSeq": 2, "inserted": 2, "duplicates": 0, "truncated": 1}
    async with diag_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT seq, length(line) AS n, line, line_truncated, event "
            "FROM app.client_diagnostics ORDER BY seq"
        )
    assert [(r["seq"], r["n"], r["line_truncated"], r["event"]) for r in rows] == [
        (1, 4000, True, "ws_close"),
        (2, 4000, False, "ws_close"),
    ]
    assert rows[0]["line"] == long_line[:4000]
    assert rows[1]["line"] == exact_line


def test_normalize_entries_truncates_by_characters_not_bytes():
    # AC-6 — limit is in str characters (multi-byte text)
    # Arrange
    entries = [
        cl.ClientLogEntry(seq=1, line="가" * 4001),
        cl.ClientLogEntry(seq=2, line="가" * 4000),
    ]

    # Act
    parsed = {p.seq: p for p in cl.normalize_entries(entries)}

    # Assert
    assert parsed[1].line == "가" * 4000
    assert parsed[1].line_truncated is True
    assert parsed[2].line == "가" * 4000
    assert parsed[2].line_truncated is False


# --- AC-7: malformed input -> 422, 0 rows, no leak -----------------------------


_VALID_ENTRY = {"seq": 1, "line": "LEAKMARK-line-91c"}

_MALFORMED_CASES = [
    ("not-json", b"not json", ["not json"]),
    ("top-level-array", b"[]", []),
    ("top-level-null", b"null", []),
    ("install-id-not-uuid", {"installId": "abc-not-uuid-q7", "entries": [_VALID_ENTRY]}, ["abc-not-uuid-q7"]),
    ("install-id-abc", {"installId": "abc", "entries": [_VALID_ENTRY]}, ["LEAKMARK-line-91c"]),
    ("install-id-missing", {"entries": [_VALID_ENTRY]}, ["LEAKMARK-line-91c"]),
    ("seq-negative", {"installId": INSTALL_ID, "entries": [{"seq": -1, "line": "LEAKMARK-line-91c"}]}, ["LEAKMARK-line-91c"]),
    ("seq-float", {"installId": INSTALL_ID, "entries": [{"seq": 1.5, "line": "LEAKMARK-line-91c"}]}, ["1.5"]),
    ("seq-string", {"installId": INSTALL_ID, "entries": [{"seq": "7", "line": "LEAKMARK-line-91c"}]}, ["LEAKMARK-line-91c"]),
    ("seq-bool", {"installId": INSTALL_ID, "entries": [{"seq": True, "line": "LEAKMARK-line-91c"}]}, ["LEAKMARK-line-91c"]),
    ("seq-2pow63", {"installId": INSTALL_ID, "entries": [{"seq": 2**63, "line": "LEAKMARK-line-91c"}]}, ["9223372036854775808"]),
    ("seq-missing", {"installId": INSTALL_ID, "entries": [{"line": "LEAKMARK-line-91c"}]}, ["LEAKMARK-line-91c"]),
    ("entries-empty", {"installId": INSTALL_ID, "entries": []}, []),
    ("entries-missing", {"installId": INSTALL_ID}, []),
    ("entries-not-list", {"installId": INSTALL_ID, "entries": "LEAKMARK-entries-55"}, ["LEAKMARK-entries-55"]),
    ("line-empty", {"installId": INSTALL_ID, "entries": [{"seq": 1, "line": ""}]}, []),
    ("line-missing", {"installId": INSTALL_ID, "entries": [{"seq": 1}]}, []),
    ("app-version-65", {"installId": INSTALL_ID, "appVersion": "V" * 65, "entries": [_VALID_ENTRY]}, ["V" * 65]),
    # json.loads raises UnicodeDecodeError on invalid UTF-8: still "not JSON" -> 422, not 503
    ("invalid-utf8", b'{"installId": "\xff\xfe\xfd", "entries": []}', []),
]


@pytest.mark.parametrize(("body", "markers"), [(b, m) for _, b, m in _MALFORMED_CASES],
                         ids=[i for i, _, _ in _MALFORMED_CASES])
async def test_malformed_upload_is_rejected_with_422_without_rows_or_leak(diag_pool, body, markers):
    # AC-7
    # Arrange
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, content=raw, headers={"Content-Type": "application/json"})

    # Assert
    assert res.status_code == 422
    assert await _count(diag_pool) == 0
    _assert_no_internal_leak(res, *markers)


async def test_boundary_values_seq_zero_seq_max_and_64_char_app_version_are_accepted(diag_pool):
    # AC-7 — the valid side of each 422 boundary
    # Arrange
    body = _body([{"seq": 0, "line": "a"}, {"seq": cl.SEQ_MAX, "line": "b"}], app_version="V" * 64)
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, json=body)

    # Assert
    assert res.status_code == 200
    assert res.json() == {"ackedMaxSeq": 2**63 - 1, "inserted": 2, "duplicates": 0, "truncated": 0}
    rows = await _rows(diag_pool)
    assert [r["seq"] for r in rows] == [0, 2**63 - 1]
    assert rows[0]["app_version"] == "V" * 64


async def test_unknown_fields_are_ignored(diag_pool):
    # AC-7 — extra="ignore": forward-compatible app payloads are not 422
    # Arrange
    body = _body([{"seq": 1, "line": "a", "level": "warn"}], deviceModel="Pixel 9")
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, json=body)

    # Assert
    assert res.status_code == 200
    assert await _count(diag_pool) == 1


def test_decode_upload_raises_invalid_upload_for_bad_json_and_non_object():
    # AC-7 — pure mapping to InvalidUpload
    for raw in (b"not json", b"[]", b'"str"', b"{\"installId\": \"abc\", \"entries\": [{\"seq\": 1, \"line\": \"a\"}]}"):
        with pytest.raises(cl.InvalidUpload):
            cl.decode_upload(raw)


def test_decode_upload_accepts_uppercase_install_id_as_uuid():
    # AC-1 AC-7 — installId is case-insensitive
    # Arrange
    raw = json.dumps({"installId": INSTALL_ID.upper(), "entries": [{"seq": 1, "line": "a"}]}).encode()

    # Act
    upload = cl.decode_upload(raw)

    # Assert
    assert str(upload.install_id) == INSTALL_ID
    assert upload.app_version is None


# --- AC-8: ackedMaxSeq is the batch maximum ------------------------------------


async def test_acked_max_seq_is_batch_max_and_counts_already_stored_as_duplicate(diag_pool):
    # AC-8
    # Arrange
    async with _client(diag_pool) as c:
        pre = await c.post(URL, json=_body([{"seq": 9, "line": "nine"}]))
        assert pre.status_code == 200

        # Act
        res = await c.post(URL, json=_body([{"seq": 5, "line": "five"}, {"seq": 3, "line": "three"},
                                            {"seq": 9, "line": "nine-again"}]))

    # Assert
    assert res.status_code == 200
    assert res.json() == {"ackedMaxSeq": 9, "inserted": 2, "duplicates": 1, "truncated": 0}
    rows = await _rows(diag_pool)
    assert [(r["seq"], r["line"]) for r in rows] == [(3, "three"), (5, "five"), (9, "nine")]


def test_build_ack_computes_max_seq_duplicates_and_truncated():
    # AC-8
    # Arrange
    upload = cl.ClientLogUpload.model_validate(
        {"installId": INSTALL_ID, "entries": [{"seq": 5, "line": "a"}, {"seq": 3, "line": "b"},
                                               {"seq": 9, "line": "c"}, {"seq": 3, "line": "d"}]}
    )
    parsed = [
        cl.ParsedEntry(seq=5, line="a", line_truncated=True, client_ts=None, event=None),
        cl.ParsedEntry(seq=3, line="b", line_truncated=False, client_ts=None, event=None),
        cl.ParsedEntry(seq=9, line="c", line_truncated=True, client_ts=None, event=None),
    ]

    # Act
    ack = cl.build_ack(upload, parsed, inserted=2)

    # Assert
    assert (ack.acked_max_seq, ack.inserted, ack.duplicates, ack.truncated) == (9, 2, 2, 2)
    assert ack.model_dump(by_alias=True) == {"ackedMaxSeq": 9, "inserted": 2, "duplicates": 2, "truncated": 2}


# --- AC-9: in-batch duplicate seq, first wins -----------------------------------


async def test_duplicate_seq_within_batch_keeps_first_line_only(diag_pool):
    # AC-9
    # Arrange
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, json=_body([{"seq": 1, "line": "A"}, {"seq": 1, "line": "B"}]))

    # Assert
    assert res.status_code == 200
    assert res.json() == {"ackedMaxSeq": 1, "inserted": 1, "duplicates": 1, "truncated": 0}
    rows = await _rows(diag_pool)
    assert [(r["seq"], r["line"]) for r in rows] == [(1, "A")]


def test_normalize_entries_drops_later_duplicates_of_same_seq():
    # AC-9
    # Arrange
    entries = [
        cl.ClientLogEntry(seq=2, line="first-2"),
        cl.ClientLogEntry(seq=1, line="A"),
        cl.ClientLogEntry(seq=2, line="second-2"),
        cl.ClientLogEntry(seq=1, line="B"),
    ]

    # Act
    parsed = cl.normalize_entries(entries)

    # Assert
    assert sorted((p.seq, p.line) for p in parsed) == [(1, "A"), (2, "first-2")]


# --- AC-10: DB failure -> 503 without internals --------------------------------


async def test_db_failure_returns_503_without_exception_details():
    # AC-10
    # Arrange
    async with _client(_ExplodingPool()) as c:
        # Act
        res = await c.post(URL, json=_body([{"seq": 1, "line": "2026-11-15T02:09:00.123Z x"}]))

    # Assert
    assert res.status_code == 503
    _assert_no_internal_leak(res, "SECRET-xyz", "secret_path", "RuntimeError")


# --- AC-11: migration round trip -------------------------------------------------


def _alembic(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVICE_ROOT,
        env={**os.environ, **env},
        capture_output=True, text=True, check=False,
    )


async def _regclass(env: dict, name: str):
    import asyncpg

    conn = await asyncpg.connect(
        host=env["POSTGRES_HOST"], port=int(env["POSTGRES_PORT"]), user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"], database=env["POSTGRES_DATABASE"],
    )
    try:
        return await conn.fetchval("SELECT to_regclass($1)::text", name)
    finally:
        await conn.close()


async def _constraint_exists(env: dict, name: str) -> bool:
    import asyncpg

    conn = await asyncpg.connect(
        host=env["POSTGRES_HOST"], port=int(env["POSTGRES_PORT"]), user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"], database=env["POSTGRES_DATABASE"],
    )
    try:
        return await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE conname = $1", name) == 1
    finally:
        await conn.close()


async def test_migration_downgrade_to_0003_drops_only_new_table_and_upgrade_restores_it(_pg_server):
    # AC-11
    env = _pg_server
    try:
        # Arrange / Act
        down = _alembic(env, "downgrade", "0003_ws_blips_client_id")

        # Assert (downgraded)
        assert down.returncode == 0, down.stderr
        assert await _regclass(env, "app.client_diagnostics") is None
        assert await _regclass(env, "app.ix_client_diagnostics_client_ts") is None
        assert await _regclass(env, "app.ix_client_diagnostics_received_at") is None
        assert await _regclass(env, "app.ws_blips") is not None

        # Act
        up = _alembic(env, "upgrade", "head")

        # Assert (upgraded)
        assert up.returncode == 0, up.stderr
        assert await _regclass(env, "app.client_diagnostics") == "app.client_diagnostics"
        assert await _regclass(env, "app.ix_client_diagnostics_client_ts") is not None
        assert await _regclass(env, "app.ix_client_diagnostics_received_at") is not None
        assert await _constraint_exists(env, "uq_client_diagnostics_install_seq")
    finally:
        # protect other tests sharing the container
        _alembic(env, "upgrade", "head")


# --- AC-12: hanging DB -> 503 within the deadline -------------------------------


async def test_unresponsive_db_returns_503_within_deadline(monkeypatch):
    # AC-12
    # Arrange
    monkeypatch.setattr(main, "CLIENT_LOGS_DB_TIMEOUT_SECONDS", 0.2)
    async with _client(_HangingPool()) as c:
        started = time.monotonic()

        # Act
        res = await asyncio.wait_for(
            c.post(URL, json=_body([{"seq": 1, "line": "2026-11-15T02:09:00.123Z x"}])), timeout=10
        )
        elapsed = time.monotonic() - started

    # Assert
    assert res.status_code == 503
    assert elapsed < 2.0
    _assert_no_internal_leak(res)


# --- AC-13: at most 2 concurrent DB connections for diagnostics ------------------


async def test_concurrent_uploads_hold_at_most_two_db_connections(monkeypatch):
    # AC-13
    # Arrange
    monkeypatch.setattr(main, "CLIENT_LOGS_DB_TIMEOUT_SECONDS", 0.3)
    pool = _HangingPool()
    async with _client(pool) as c:
        # Act
        results = await asyncio.wait_for(
            asyncio.gather(*[
                c.post(URL, json=_body([{"seq": i, "line": "2026-11-15T02:09:00.123Z x"}]))
                for i in range(5)
            ]),
            timeout=10,
        )

    # Assert
    assert [r.status_code for r in results] == [503] * 5
    assert 1 <= pool.max_seen <= 2
    assert pool.current == 0


# --- AC-14: unstorable characters are replaced, not rejected ---------------------


def test_sanitize_text_replaces_nul_with_replacement_char():
    # AC-14
    assert cl.sanitize_text("a\x00b") == "a�b"


def test_sanitize_text_replaces_lone_surrogate_so_result_encodes_as_utf8():
    # AC-14
    # Arrange / Act
    out = cl.sanitize_text("a\ud800b")

    # Assert
    out.encode("utf-8")
    assert out == "a?b"


def test_sanitize_text_leaves_ordinary_unicode_untouched():
    # AC-14 — no over-sanitizing of normal text
    s = "2026-11-15T02:09:00.123Z ws_close 한글 ✓ 😀 \t tab"
    assert cl.sanitize_text(s) == s


def test_decode_upload_does_not_reject_lone_surrogate_and_normalize_replaces_it():
    # AC-14 — contract §4: lone surrogates are replaced, never rejected (no 422).
    # pydantic-core cannot hold a lone surrogate in a str, so decode_upload must not
    # hand the raw json.loads output with "\ud800" to model validation unchanged.
    # Arrange
    raw = b'{"installId": "' + INSTALL_ID.encode() + b'", "entries": [{"seq": 1, "line": "a\\ud800b"}]}'

    # Act
    upload = cl.decode_upload(raw)
    [p] = cl.normalize_entries(upload.entries)

    # Assert
    assert p.line == "a?b"
    p.line.encode("utf-8")


async def test_line_with_nul_is_stored_with_replacement_char(diag_pool):
    # AC-14
    # Arrange
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, json=_body([{"seq": 1, "line": "2026-11-15T02:09:00.123Z x \u0000"}]))

    # Assert
    assert res.status_code == 200
    [row] = await _rows(diag_pool)
    assert row["line"] == "2026-11-15T02:09:00.123Z x �"
    assert row["event"] == "x"


async def test_lone_surrogate_in_line_and_nul_in_app_version_are_stored_sanitized(diag_pool):
    # AC-14 — JSON "\ud800" decodes to a lone surrogate; appVersion also goes through sanitize_text
    # Arrange
    raw = b'{"installId": "' + INSTALL_ID.encode() + b'", "appVersion": "1.0\\u0000", ' \
          b'"entries": [{"seq": 1, "line": "a\\ud800b"}]}'
    async with _client(diag_pool) as c:
        # Act
        res = await c.post(URL, content=raw, headers={"Content-Type": "application/json"})

    # Assert
    assert res.status_code == 200
    [row] = await _rows(diag_pool)
    assert row["line"] == "a?b"
    assert row["app_version"] == "1.0�"
