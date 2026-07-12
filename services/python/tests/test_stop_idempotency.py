"""Idempotency test for the session-stop path (repository.end_session).

docs §3 weakness 1: Node may call ``/internal/sessions/stop`` more than once
(e.g. a retried teardown), which re-hits Python's stop handler. ``end_session``
must stamp ``ended_at`` exactly once and report ``ended=True`` only on that
first transition. This is proven here against a fake asyncpg pool that models
the ``UPDATE ... WHERE ended_at IS NULL`` semantics — no real DB required.
"""
import pytest

from src.repository.implementation.translation_repository import end_session


class _FakeConn:
    """Minimal asyncpg-connection stand-in keyed off SQL substrings."""

    def __init__(self, store: dict) -> None:
        self.store = store

    async def fetchrow(self, sql: str, *args):
        if "UPDATE app.sessions SET ended_at" in sql and "ended_at IS NULL" in sql:
            session_id = args[0]
            if self.store["ended_at"] is None:
                self.store["ended_at"] = "now()"
                self.store["ended_at_writes"] += 1
                return {"session_id": session_id}
            return None  # already ended → zero rows affected
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args):
        if "count(*) FROM app.translations" in sql:
            return self.store["translations_count"]
        if "COALESCE(translation_count" in sql:
            return self.store["translation_count"]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql: str, *args):
        if "SET translation_count" in sql:
            self.store["translation_count"] = args[1]
            self.store["set_count_writes"] += 1
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, store: dict) -> None:
        self._conn = _FakeConn(store)

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.fixture
def store():
    return {
        "ended_at": None,
        "ended_at_writes": 0,
        "set_count_writes": 0,
        "translations_count": 3,
        "translation_count": 0,
    }


async def test_first_stop_ends_and_counts(store):
    pool = _FakePool(store)
    ended, count = await end_session(pool, "sess-1")
    assert ended is True
    assert count == 3
    assert store["ended_at"] == "now()"
    assert store["ended_at_writes"] == 1
    assert store["set_count_writes"] == 1


async def test_second_stop_is_noop(store):
    pool = _FakePool(store)

    first = await end_session(pool, "sess-1")
    second = await end_session(pool, "sess-1")

    assert first == (True, 3)
    # Second call: no transition, no further writes, count read back.
    assert second == (False, 3)
    assert store["ended_at_writes"] == 1      # ended_at stamped exactly once
    assert store["set_count_writes"] == 1     # count rewritten exactly once


async def test_many_stops_stay_idempotent(store):
    pool = _FakePool(store)
    results = [await end_session(pool, "sess-1") for _ in range(5)]

    assert results[0] == (True, 3)
    assert all(r == (False, 3) for r in results[1:])
    assert store["ended_at_writes"] == 1
