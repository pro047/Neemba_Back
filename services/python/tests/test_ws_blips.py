"""§4-7 (handover-2026-07-25): /ws 순단(blip) 계측.

2026-07-26 예배에서 /ws 순단 3회의 원인(기기 절전 vs 네트워크)을 판별할
데이터가 없었다. 순단을 ``app.ws_blips`` 에 기록한다:

- 끊김 시 insert (close code/reason, detected_by) → 재연결 시 update
  (reconnected_at·duration·flushed/lost 건수). 재연결 없이 끝나면
  reconnected_at NULL 잔존 = "미복귀" (사용자 결정 2).
- close code 1001(클라 정상 종료: 절전/백그라운드) vs 1006(비정상 단절:
  네트워크) 구분이 계측의 핵심.
- DB 기록은 hub hot path 와 격리 — recorder 가 예외를 삼키고, hub 는
  fire-and-forget task 로만 호출한다.

repository 는 Phase 5 와 같은 fake-pool 패턴(SQL·바인드 파라미터 검증,
실 DB 없음), hub 라이프사이클은 FakeRecorder 로 결정적으로 재현한다.
"""
import asyncio
import contextlib

from starlette.websockets import WebSocketState

from src.repository.implementation import ws_blip_repository as wb
from src.ws.blip_recorder import WsBlipRecorder
from src.ws.websocket import WebSocketHub

# --- fake asyncpg pool (test_monitor_query_repository 패턴) -----------------


class _CapturingConn:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row
        self.calls = []  # list of (method, sql, args)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.row

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows=None, row=None):
        self.conn = _CapturingConn(rows=rows, row=row)

    def acquire(self):
        return _Acquire(self.conn)

    @property
    def last_sql(self):
        return self.conn.calls[-1][1]

    @property
    def last_args(self):
        return self.conn.calls[-1][2]


def _row(**kw):
    return kw


# ruff TRY003 회피용 상수 (raise 에 문자열 리터럴 직접 전달 금지)
_SEND_AFTER_CLOSE_MSG = "send after close"
_DB_DOWN_MSG = "db down"


# --- repository: insert_blip ------------------------------------------------


async def test_insert_blip_binds_params_and_returns_id():
    """끊김 insert 는 값 전부를 바인드 파라미터로 넘기고 생성 id 를 반환해야 한다."""
    pool = _FakePool(row={"id": 7})
    blip_id = await wb.insert_blip(
        pool,
        session_id="s1",
        close_code=1006,
        close_reason="net down",
        detected_by="client_disconnect",
    )
    assert blip_id == 7
    assert "INSERT INTO app.ws_blips" in pool.last_sql
    assert "RETURNING id" in pool.last_sql
    assert pool.last_args == ("s1", 1006, "net down", "client_disconnect")


async def test_insert_blip_allows_null_close_code():
    """서버발 끊음(keepalive)은 close frame 이 없으므로 code/reason 없이 기록돼야 한다."""
    pool = _FakePool(row={"id": 1})
    blip_id = await wb.insert_blip(
        pool, session_id="s1", detected_by="keepalive_timeout"
    )
    assert blip_id == 1
    assert pool.last_args == ("s1", None, None, "keepalive_timeout")


# --- repository: close_blip -------------------------------------------------


async def test_close_blip_stamps_reconnect_and_counts():
    """재연결 update 는 reconnected_at·duration 을 DB 시계로 찍고
    flushed/lost 건수를 바인드해야 한다."""
    pool = _FakePool()
    await wb.close_blip(pool, blip_id=7, flushed_count=21, lost_count=0)
    sql = pool.last_sql
    assert "UPDATE app.ws_blips" in sql
    assert "reconnected_at = now()" in sql
    assert "duration_ms" in sql
    # 이미 종결된 blip 을 다시 종결하지 않는다 (멱등)
    assert "reconnected_at IS NULL" in sql
    assert pool.last_args == (7, 21, 0)


# --- repository: list_blips -------------------------------------------------


async def test_list_blips_orders_newest_first():
    """조회는 disconnected_at 최신순이어야 한다."""
    pool = _FakePool(rows=[_row(id=1)])
    await wb.list_blips(pool, limit=50, offset=0)
    sql = pool.last_sql
    assert "FROM app.ws_blips" in sql
    assert "ORDER BY disconnected_at DESC, id DESC" in sql
    assert pool.last_args == (50, 0)


async def test_list_blips_filters_by_session():
    """sessionId 필터는 바인드 파라미터로 적용돼야 한다."""
    pool = _FakePool(rows=[])
    await wb.list_blips(pool, session_id="s1", limit=10, offset=5)
    assert "WHERE session_id = $1" in pool.last_sql
    assert pool.last_args == ("s1", 10, 5)


async def test_list_blips_next_offset_on_full_page():
    """꽉 찬 페이지면 next_offset, 아니면 None 이어야 한다 (sessions 와 동일 규약)."""
    pool = _FakePool(rows=[_row(id=i) for i in range(2)])
    _, next_offset = await wb.list_blips(pool, limit=2, offset=0)
    assert next_offset == 2
    pool2 = _FakePool(rows=[_row(id=1)])
    _, next_offset2 = await wb.list_blips(pool2, limit=2, offset=0)
    assert next_offset2 is None


# --- recorder isolation -----------------------------------------------------


class _BrokenPool:
    def acquire(self):
        raise RuntimeError(_DB_DOWN_MSG)


async def test_recorder_swallows_db_errors():
    """DB 가 죽어도 recorder 는 예외를 hub 로 올리지 않아야 한다 (격리)."""
    rec = WsBlipRecorder(_BrokenPool())
    blip_id = await rec.record_disconnect("s1", detected_by="client_disconnect")
    assert blip_id is None
    await rec.record_reconnect(1, flushed_count=0, lost_count=0)  # must not raise


# --- hub lifecycle ----------------------------------------------------------


class FakeWS:
    """starlette WebSocket 의 client/application 두-상태 semantics 모사."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTING
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        if (
            self.client_state != WebSocketState.CONNECTED
            or self.application_state != WebSocketState.CONNECTED
        ):
            raise RuntimeError(_SEND_AFTER_CLOSE_MSG)
        self.sent.append(text)

    async def send_json(self, data) -> None:
        await self.send_text(str(data))

    async def close(self, code: int = 1000) -> None:
        self.application_state = WebSocketState.DISCONNECTED

    def client_disconnect(self) -> None:
        self.client_state = WebSocketState.DISCONNECTED


class FakeRecorder:
    """WsBlipRecorder 인터페이스 모사 — 호출 기록만 남긴다."""

    def __init__(self) -> None:
        self.disconnects: list[dict] = []
        self.reconnects: list[dict] = []
        self._next_id = 0

    async def record_disconnect(
        self, session_id, *, close_code=None, close_reason=None, detected_by
    ):
        self._next_id += 1
        self.disconnects.append({
            "id": self._next_id,
            "session_id": session_id,
            "close_code": close_code,
            "close_reason": close_reason,
            "detected_by": detected_by,
        })
        return self._next_id

    async def record_reconnect(self, blip_id, *, flushed_count, lost_count):
        self.reconnects.append({
            "blip_id": blip_id,
            "flushed_count": flushed_count,
            "lost_count": lost_count,
        })


async def _drain(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _teardown(hub: WebSocketHub) -> None:
    task = hub._keepalive_task
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_client_disconnect_records_blip_with_close_code():
    """클라 끊김이면 close code/reason 과 detected_by=client_disconnect 로
    blip 이 열려야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect(
        "s1", ws, close_code=1001, close_reason="going away"
    )
    await _drain()

    assert rec.disconnects == [{
        "id": 1,
        "session_id": "s1",
        "close_code": 1001,
        "close_reason": "going away",
        "detected_by": "client_disconnect",
    }]
    await _teardown(hub)


async def test_reconnect_closes_blip_with_flush_and_lost_counts():
    """같은 세션이 재접속하면 blip 이 flushed(=방류 예정 pending 건수)·lost
    건수와 함께 종결돼야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    # 끊김 동안 번역 2건이 pending 에 쌓임
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await hub.broadcast_to_session("s1", {"sentence": "m2"})

    ws2 = FakeWS()
    await hub.attach(ws2, "s1")
    await _drain()

    assert rec.reconnects == [{"blip_id": 1, "flushed_count": 2, "lost_count": 0}]
    await _teardown(hub)


async def test_pending_overflow_during_blip_counts_lost():
    """blip 동안 pending 캡 초과로 버린 건수는 lost_count 로 집계돼야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    hub._max_pending = 2
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    for i in range(4):  # 캡 2 → 2건 드롭
        await hub.broadcast_to_session("s1", {"sentence": f"m{i}"})

    ws2 = FakeWS()
    await hub.attach(ws2, "s1")
    await _drain()

    assert rec.reconnects == [{"blip_id": 1, "flushed_count": 2, "lost_count": 2}]
    await _teardown(hub)


async def test_different_session_leaves_blip_unreconnected():
    """다른 세션이 슬롯을 차지하면 이전 blip 은 종결 없이 미복귀로 남아야 한다
    (결정 2: reconnected_at NULL 잔존)."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    ws2 = FakeWS()
    await hub.attach(ws2, "s2")
    await _drain()

    assert rec.reconnects == []
    await _teardown(hub)


async def test_keepalive_detected_blip_has_no_close_code():
    """서버발 끊음(keepalive 타임아웃)은 detected_by=keepalive_timeout,
    close code 없음으로 기록돼야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    # keepalive 루프가 pong 타임아웃 시 호출하는 중앙 경로
    await hub._mark_waiting_for_reconnect()
    await _drain()

    assert rec.disconnects == [{
        "id": 1,
        "session_id": "s1",
        "close_code": None,
        "close_reason": None,
        "detected_by": "keepalive_timeout",
    }]
    await _teardown(hub)


async def test_stale_disconnect_does_not_record_blip():
    """이미 새 소켓으로 교체된 뒤 도착한 늦은 끊김 통지는 blip 을 열지 않아야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws2 = FakeWS()
    await hub.attach(ws2, "s1")
    await _drain()

    await hub.handle_client_disconnect("s1", ws, close_code=1006)  # stale ws
    await _drain()

    assert rec.disconnects == []
    await _teardown(hub)


async def test_hub_without_recorder_still_works():
    """recorder 미주입(테스트·구버전 조립)이어도 hub 동작은 그대로여야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)

    ws2 = FakeWS()
    await hub.attach(ws2, "s1")
    await _drain()

    assert hub.client is ws2
    await _teardown(hub)
