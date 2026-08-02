"""WU2 (monitor-page-v2): 캡처 경로 개편 + 세션 정합성 — 실 DB 검증.

결정 D3/D4-a 를 실제 Postgres(테스트 컨테이너, conftest ``pg_pool``)로 검증한다:

- ``insert_translation`` 이 DB 가 부여한 ``(id, created_at)`` 을 반환하고,
  ``Pusher._capture`` 가 **insert 후** 그 값을 monitor payload 에 실어
  broadcast 한다 (이력↔라이브 dedup·gap fill 의 전제).
- insert 실패 시 id 없이(null) broadcast 하는 폴백 — 라이브가 DB 장애에
  볼모 잡히지 않는다 (D3).
- ``ensure_session_with_retry``: 일시 장애는 재시도로 흡수, 실패는
  ``neemba_ensure_session_failed_total`` 카운터에 적산.
- ``close_stale_sessions``: 기동 시 ``ended_at IS NULL`` 유령 세션 일괄 종료
  + translation_count 재계산 (D4-a).
- 세션 목록의 ``last_translation_at`` (STALE 배지·미니 통계용, D4-b 의 데이터).

장애 주입은 실 pool 을 감싸는 래퍼로만 한다 — 성공 경로는 전부 실 DB 를 탄다.
"""
from __future__ import annotations

from prometheus_client import REGISTRY

from src.pushClient.pusher import Pusher
from src.repository.implementation import monitor_query_repository as mq
from src.repository.implementation.translation_repository import (
    close_stale_sessions,
    end_session,
    ensure_session,
    ensure_session_with_retry,
    insert_translation,
)

# --- helpers ----------------------------------------------------------------

_BOOM_MSG = "injected db failure"


def _ensure_failed_total() -> float:
    return REGISTRY.get_sample_value("neemba_ensure_session_failed_total") or 0.0


class RecordingMonitorHub:
    """MonitorHub 대역: broadcast payload 를 순서대로 기록만 한다."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, dict]] = []

    async def broadcast(self, session_id: str, payload: dict) -> None:
        self.broadcasts.append((session_id, payload))


class FailingAcquirePool:
    """처음 ``fail_times`` 번의 acquire() 만 실패하고 이후는 실 pool 에 위임."""

    def __init__(self, pool, fail_times: int) -> None:
        self._pool = pool
        self._remaining = fail_times
        self.attempts = 0

    def acquire(self):
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise ConnectionError(_BOOM_MSG)
        return self._pool.acquire()


class _ExecFailConn:
    """conn.execute 만 실패시키는 프록시 (ensure 는 execute, insert 는 fetchrow)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, *args, **kwargs):
        raise ConnectionError(_BOOM_MSG)


class ExecFailPool:
    """acquire 는 정상, 커넥션의 execute 만 실패하는 실-pool 래퍼."""

    def __init__(self, pool) -> None:
        self._pool = pool

    def acquire(self):
        return _WrapAcquire(self._pool)


class _WrapAcquire:
    def __init__(self, pool) -> None:
        self._ctx = pool.acquire()

    async def __aenter__(self):
        conn = await self._ctx.__aenter__()
        return _ExecFailConn(conn)

    async def __aexit__(self, *exc):
        return await self._ctx.__aexit__(*exc)


async def _fetch_translation_rows(pool, session_id: str) -> list:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM app.translations WHERE session_id = $1 ORDER BY id",
            session_id,
        )


def _capture_kwargs(session_id: str = "sess-1") -> dict:
    return {
        "session_id": session_id,
        "segment_id": 7,
        "sequence": 3,
        "source_text": "안녕하세요",
        "translated_text": "Hello",
        "source_lang": "ko",
        "target_lang": "en",
        "confidence": 0.9,
    }


# --- insert_translation: RETURNING id, created_at ---------------------------


async def test_insert_returns_db_assigned_id_and_created_at(pg_pool):
    await ensure_session(pg_pool, "sess-1", "ko", "en")

    persisted = await insert_translation(
        pg_pool,
        session_id="sess-1",
        source_text="안녕",
        translated_text="hi",
    )

    assert persisted is not None
    row_id, created_at = persisted
    rows = await _fetch_translation_rows(pg_pool, "sess-1")
    assert [r["id"] for r in rows] == [row_id]
    assert rows[0]["created_at"] == created_at
    assert created_at.tzinfo is not None


# --- Pusher._capture: insert 후 broadcast (D3) ------------------------------


async def test_capture_broadcasts_db_id_and_created_at_after_insert(pg_pool):
    hub = RecordingMonitorHub()
    pusher = Pusher(hub=None, monitor_hub=hub, db_pool=pg_pool)

    await pusher._capture(**_capture_kwargs())

    rows = await _fetch_translation_rows(pg_pool, "sess-1")
    assert len(rows) == 1
    (session_id, payload), = hub.broadcasts
    assert session_id == "sess-1"
    # broadcast 가 insert 이후임을 DB 부여 값으로 증명.
    assert payload["id"] == rows[0]["id"]
    assert payload["createdAt"] == rows[0]["created_at"].isoformat()
    assert payload["sourceText"] == "안녕하세요"
    assert payload["translatedText"] == "Hello"


async def test_capture_falls_back_to_null_id_when_insert_fails(pg_pool):
    hub = RecordingMonitorHub()
    # ensure 는 이미 끝난 것으로 표시해 acquire 실패가 insert 에만 꽂히게 한다.
    failing = Pusher(
        hub=None, monitor_hub=hub, db_pool=FailingAcquirePool(pg_pool, fail_times=1)
    )
    failing._ensured_sessions.add("sess-1")

    await failing._capture(**_capture_kwargs())

    # insert 는 실패했지만 broadcast 는 안정된 shape(null id)로 나간다.
    (_, payload), = hub.broadcasts
    assert payload["id"] is None
    assert payload["createdAt"] is None
    assert payload["sourceText"] == "안녕하세요"
    assert await _fetch_translation_rows(pg_pool, "sess-1") == []


async def test_capture_without_pool_broadcasts_null_id(pg_pool):
    hub = RecordingMonitorHub()
    pusher = Pusher(hub=None, monitor_hub=hub, db_pool=None)

    await pusher._capture(**_capture_kwargs())

    (_, payload), = hub.broadcasts
    assert payload["id"] is None
    assert payload["createdAt"] is None


async def test_capture_still_inserts_when_ensure_fails(pg_pool):
    # start 핸들러가 이미 세션 행을 만든 상황에서 ensure 안전망만 실패해도
    # insert 는 시도되어야 한다.
    await ensure_session(pg_pool, "sess-1", "ko", "en")
    hub = RecordingMonitorHub()
    pusher = Pusher(hub=None, monitor_hub=hub, db_pool=ExecFailPool(pg_pool))

    await pusher._capture(**_capture_kwargs())

    rows = await _fetch_translation_rows(pg_pool, "sess-1")
    assert len(rows) == 1
    (_, payload), = hub.broadcasts
    assert payload["id"] == rows[0]["id"]


# --- ensure_session_with_retry ----------------------------------------------


async def test_ensure_retry_recovers_from_transient_failure(pg_pool):
    flaky = FailingAcquirePool(pg_pool, fail_times=1)
    before = _ensure_failed_total()

    await ensure_session_with_retry(
        flaky, "sess-r", "ko", "en", retry_delay=0.01
    )

    async with pg_pool.acquire() as conn:
        found = await conn.fetchval(
            "SELECT count(*) FROM app.sessions WHERE session_id = 'sess-r'"
        )
    assert found == 1
    assert flaky.attempts == 2                      # 실패 1회 + 성공 1회
    assert _ensure_failed_total() == before + 1     # 실패 시도만 적산


async def test_ensure_retry_exhausts_and_raises(pg_pool):
    always_failing = FailingAcquirePool(pg_pool, fail_times=99)
    before = _ensure_failed_total()

    try:
        await ensure_session_with_retry(
            always_failing, "sess-x", retries=2, retry_delay=0.01
        )
        raised = False
    except ConnectionError:
        raised = True

    assert raised
    assert always_failing.attempts == 3             # 최초 1회 + 재시도 2회
    assert _ensure_failed_total() == before + 3


# --- close_stale_sessions (D4-a) --------------------------------------------


async def test_startup_closes_stale_sessions_and_recounts(pg_pool):
    # 유령 후보 2개(번역 2건/0건) + 정상 종료 1개.
    await ensure_session(pg_pool, "ghost-a", "ko", "en")
    await insert_translation(pg_pool, session_id="ghost-a", source_text="a", translated_text="b")
    await insert_translation(pg_pool, session_id="ghost-a", source_text="c", translated_text="d")
    await ensure_session(pg_pool, "ghost-b")
    await ensure_session(pg_pool, "done-1")
    await end_session(pg_pool, "done-1")
    async with pg_pool.acquire() as conn:
        ended_before = await conn.fetchval(
            "SELECT ended_at FROM app.sessions WHERE session_id = 'done-1'"
        )

    closed = await close_stale_sessions(pg_pool)

    assert sorted(closed) == ["ghost-a", "ghost-b"]
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT session_id, ended_at, translation_count "
            "FROM app.sessions ORDER BY session_id"
        )
    by_id = {r["session_id"]: r for r in rows}
    assert by_id["ghost-a"]["ended_at"] is not None
    assert by_id["ghost-a"]["translation_count"] == 2   # 재계산됨
    assert by_id["ghost-b"]["ended_at"] is not None
    assert by_id["ghost-b"]["translation_count"] == 0
    assert by_id["done-1"]["ended_at"] == ended_before  # 기존 종료 세션 불변


async def test_startup_reconciliation_is_noop_on_clean_state(pg_pool):
    await ensure_session(pg_pool, "done-1")
    await end_session(pg_pool, "done-1")

    assert await close_stale_sessions(pg_pool) == []


# --- 세션 목록의 last_translation_at ----------------------------------------


async def test_session_list_includes_last_translation_at(pg_pool):
    await ensure_session(pg_pool, "sess-old", "ko", "en")
    await insert_translation(pg_pool, session_id="sess-old", source_text="a", translated_text="b")
    last = await insert_translation(pg_pool, session_id="sess-old", source_text="c", translated_text="d")
    await ensure_session(pg_pool, "sess-empty")

    rows, _ = await mq.list_sessions(pg_pool)

    by_id = {r["session_id"]: r for r in rows}
    assert last is not None
    assert by_id["sess-old"]["last_translation_at"] == last[1]  # MAX(created_at)
    assert by_id["sess-empty"]["last_translation_at"] is None   # 번역 0건 → NULL
