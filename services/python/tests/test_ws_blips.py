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
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocketState

from src.repository.implementation import monitor_query_repository as mq, ws_blip_repository as wb
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
        client_id="c1",
        close_code=1006,
        close_reason="net down",
        detected_by="client_disconnect",
    )
    assert blip_id == 7
    assert "INSERT INTO app.ws_blips" in pool.last_sql
    assert "RETURNING id" in pool.last_sql
    # P1 D5: client_id 가 session_id 다음 자리다. 순서가 밀리면 close_reason 에
    # detected_by 가 들어가는 조용한 오염이 된다.
    assert pool.last_args == ("s1", "c1", 1006, "net down", "client_disconnect")


async def test_insert_blip_allows_null_close_code():
    """서버발 끊음(keepalive)은 close frame 이 없으므로 code/reason 없이 기록돼야 한다."""
    pool = _FakePool(row={"id": 1})
    blip_id = await wb.insert_blip(
        pool, session_id="s1", detected_by="keepalive_timeout"
    )
    assert blip_id == 1
    # client_id 미지정도 legal — P1 이전에 기록된 행과 같은 모양(NULL)이다.
    assert pool.last_args == ("s1", None, None, None, "keepalive_timeout")


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


# --- repository: abandon_blip -----------------------------------------------


async def test_abandon_blip_stamps_loss_without_reconnect():
    """세션이 먼저 끝난 blip 은 lost_count 만 찍고 reconnected_at 은 NULL 로
    남아야 한다 (§4-7 결정 2: NULL = 미복귀)."""
    pool = _FakePool()
    await wb.abandon_blip(pool, blip_id=3, lost_count=5)
    sql = pool.last_sql
    assert "UPDATE app.ws_blips" in sql
    assert "reconnected_at = now()" not in sql
    # 이미 종결된 blip 의 집계를 덮어쓰지 않는다
    assert "reconnected_at IS NULL" in sql
    assert pool.last_args == (3, 5)


# --- repository: list_blips -------------------------------------------------


_T0 = datetime(2026, 8, 9, 10, 0, 0, tzinfo=UTC)


async def test_list_blips_orders_newest_first():
    """조회는 disconnected_at 최신순이어야 한다."""
    pool = _FakePool(rows=[_row(id=1)])
    await wb.list_blips(pool, limit=50)
    sql = pool.last_sql
    assert "FROM app.ws_blips" in sql
    assert "ORDER BY disconnected_at DESC, id DESC" in sql
    # D5: 조회에도 실려야 세션당 N행을 사람이 구분한다 (쓰기 전용이면 무용지물)
    assert "client_id" in sql
    # OFFSET 이 남아 있으면 커서와 위치 기반이 섞여 페이지가 또 밀린다
    assert "OFFSET" not in sql
    assert pool.last_args == (50,)


async def test_list_blips_filters_by_session():
    """sessionId 필터는 바인드 파라미터로 적용돼야 한다."""
    pool = _FakePool(rows=[])
    await wb.list_blips(pool, session_id="s1", limit=10)
    assert "WHERE session_id = $1" in pool.last_sql
    assert pool.last_args == ("s1", 10)


async def test_list_blips_cursor_is_a_tuple_comparison_alongside_the_session_filter():
    """커서는 (disconnected_at, id) 튜플 비교여야 하고 sessionId 필터와 AND 로
    함께 걸려야 한다 (한쪽만 걸리면 남의 세션 행이 섞여 들어온다)."""
    pool = _FakePool(rows=[])
    await wb.list_blips(pool, session_id="s1", limit=10, cursor=(_T0, 42))
    sql = pool.last_sql
    assert "WHERE session_id = $1 AND (disconnected_at, id) < ($2, $3)" in sql
    assert pool.last_args == ("s1", _T0, 42, 10)


async def test_list_blips_next_cursor_on_full_page():
    """꽉 찬 페이지면 마지막 행 위치를 인코딩한 커서를, 아니면 None 을 줘야 한다."""
    rows = [_row(id=i, disconnected_at=_T0 + timedelta(seconds=i)) for i in range(2)]
    pool = _FakePool(rows=rows)
    _, next_cursor = await wb.list_blips(pool, limit=2)
    assert next_cursor is not None
    # opaque 토큰이지만 왕복은 서버 몫이다 — 디코드가 마지막 행을 정확히 가리켜야
    # 다음 페이지가 그 지점부터 이어진다.
    assert mq.decode_search_cursor(next_cursor) == (rows[-1]["disconnected_at"], 1)

    pool2 = _FakePool(rows=[_row(id=1, disconnected_at=_T0)])
    _, next_cursor2 = await wb.list_blips(pool2, limit=2)
    assert next_cursor2 is None


# --- pagination against a real DB (conftest pg_pool) ------------------------


async def _seed_blip(pool, *, session_id: str, at: datetime) -> int:
    """disconnected_at 을 명시해 정렬을 결정적으로 만든다 (server_default now() 는
    같은 마이크로초에 겹칠 수 있어 순서가 흔들린다)."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO app.ws_blips (session_id, disconnected_at, detected_by) "
            "VALUES ($1, $2, 'client_disconnect') RETURNING id",
            session_id,
            at,
        )


async def test_blip_recorded_between_pages_does_not_duplicate_or_skip_rows(pg_pool):
    """1페이지를 보는 사이에 새 순단이 기록돼도 2페이지는 1페이지 행을 되풀이하거나
    건너뛰어서는 안 된다.

    페이지 사이의 INSERT 가 이 테스트의 대조군이다 — 그게 없으면 OFFSET 구현도
    똑같이 통과해서 아무것도 증명하지 못한다. 순단은 장애 중에 몰려 발생하므로
    이 삽입은 인위적인 상황이 아니라 이력을 볼 이유가 있는 상황 그 자체다.
    """
    ids = [
        await _seed_blip(pg_pool, session_id="s1", at=_T0 + timedelta(seconds=i))
        for i in range(4)
    ]

    page1, next_cursor = await wb.list_blips(pg_pool, limit=2)
    await _seed_blip(pg_pool, session_id="s1", at=_T0 + timedelta(seconds=99))
    page2, _ = await wb.list_blips(
        pg_pool, limit=2, cursor=mq.decode_search_cursor(next_cursor)
    )

    seen = [r["id"] for r in page1] + [r["id"] for r in page2]
    assert len(seen) == len(set(seen)), f"중복: {seen}"
    # 삽입 시점 기준 스냅샷 — 커서 뒤의 4행이 정확히 한 번씩, 최신순으로.
    assert seen == list(reversed(ids))


async def test_blips_sharing_a_timestamp_survive_the_page_boundary(pg_pool):
    """disconnected_at 이 같은 행들이 페이지 경계에 걸려도 하나도 빠지지 않아야 한다.

    P1 D5 이후 세션 하나에 소켓이 N개다 — 네트워크가 끊기면 그 소켓들이 같은
    순간에 blip 을 남기고, disconnected_at 의 기본값 now() 는 트랜잭션 시각이라
    값이 정확히 겹칠 수 있다. 커서가 시각만 보면 그 동률 묶음이 통째로 사라진다.
    """
    ids = [await _seed_blip(pg_pool, session_id="s1", at=_T0) for _ in range(4)]

    page1, next_cursor = await wb.list_blips(pg_pool, limit=2)
    page2, _ = await wb.list_blips(
        pg_pool, limit=2, cursor=mq.decode_search_cursor(next_cursor)
    )

    seen = [r["id"] for r in page1] + [r["id"] for r in page2]
    assert seen == list(reversed(ids)), f"동률 타임스탬프에서 유실: {seen}"


async def test_last_page_reports_no_next_cursor(pg_pool):
    """더 볼 것이 없으면 next_cursor 가 None 이어야 한다 ([더 보기] 를 숨기는 근거)."""
    for i in range(3):
        await _seed_blip(pg_pool, session_id="s1", at=_T0 + timedelta(seconds=i))
    _, next_cursor = await wb.list_blips(pg_pool, limit=2)
    _, last_cursor = await wb.list_blips(
        pg_pool, limit=2, cursor=mq.decode_search_cursor(next_cursor)
    )
    assert last_cursor is None


# --- route: cursor contract -------------------------------------------------


async def test_route_rejects_a_malformed_cursor_with_422():
    """깨진 커서는 422 여야 한다 — 조용히 1페이지로 되감으면 사용자는 목록이 왜
    처음으로 돌아갔는지 알 수 없다 (translations 검색과 같은 규약)."""
    import main

    # 라우트를 직접 부르므로 인자를 전부 명시한다 — 생략하면 FastAPI 가 채우는
    # 대신 Query 객체가 그대로 함수 본문에 들어간다 (test_monitor_status 와 달리
    # 이 라우트는 Query 기본값을 쓴다).
    with pytest.raises(HTTPException) as exc:
        await main.monitor_ws_blips(
            session_id=None, limit=None, cursor="not-a-cursor", pool=None
        )
    assert exc.value.status_code == 422


async def test_route_returns_next_cursor_not_next_offset(pg_pool):
    """응답 계약은 nextCursor 다. nextOffset 이 남아 있으면 프런트가 옛 키를 읽어
    조용히 1페이지만 반복해 보여준다."""
    import main

    for i in range(2):
        await _seed_blip(pg_pool, session_id="s1", at=_T0 + timedelta(seconds=i))
    res = await main.monitor_ws_blips(
        session_id=None, limit=1, cursor=None, pool=pg_pool
    )
    body = res.model_dump(by_alias=True)
    assert "nextOffset" not in body
    assert "offset" not in body
    assert isinstance(body["nextCursor"], str)


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
        self.abandons: list[dict] = []
        self._next_id = 0

    async def record_disconnect(
        self,
        session_id,
        *,
        client_id=None,
        close_code=None,
        close_reason=None,
        detected_by,
    ):
        self._next_id += 1
        self.disconnects.append({
            "id": self._next_id,
            "session_id": session_id,
            "client_id": client_id,
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

    async def record_abandon(self, blip_id, *, lost_count):
        self.abandons.append({"blip_id": blip_id, "lost_count": lost_count})


async def _drain(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _attach(hub: WebSocketHub, ws, session_id: str, **kwargs):
    """라이브 세션에 청취자를 붙인다 — 프로덕션의 start → /ws 순서 그대로.

    ``attach`` 는 화이트리스트라 ``register_session`` 되지 않은 sessionId 를
    거절한다(라이브가 아닌 세션이 재접속만으로 되살아나는 것을 막는다). 순단
    시나리오는 전부 '방송 중인 세션' 위에서 벌어지므로 등록을 먼저 한다.
    거절 자체를 보는 테스트는 ``hub.attach`` 를 직접 부른다.
    """
    await hub.register_session(session_id, kwargs.get("target_lang"))
    return await hub.attach(ws, session_id, **kwargs)


async def _teardown(hub: WebSocketHub) -> None:
    # P1: keepalive 는 허브당 1개가 아니라 소켓당 1개다.
    for conn in list(hub._conns.values()):
        task = conn.keepalive_task
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
    client_id = await _attach(hub, ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect(
        "s1", ws, close_code=1001, close_reason="going away"
    )
    await _drain()

    # D5: 행이 '어느 소켓의 순단인지' 를 들고 있어야 세션당 N행이 구분된다.
    assert rec.disconnects == [{
        "id": 1,
        "session_id": "s1",
        "client_id": client_id,
        "close_code": 1001,
        "close_reason": "going away",
        "detected_by": "client_disconnect",
    }]
    await _teardown(hub)


async def test_reconnect_closes_blip_with_lost_count():
    """재접속하면 blip 이 종결되고, 순단 동안의 자막은 lost 로 집계돼야 한다.

    P1 D3 이전에는 이 문장들이 pending 에 쌓였다가 방류돼 ``flushed_count``
    였다. 백로그를 들어낸 지금은 같은 문장이 그대로 유실이므로 같은 수가
    ``lost_count`` 로 넘어온다 — 계측이 사라진 게 아니라 칸이 바뀐 것이다.
    ``flushed_count`` 는 방류할 백로그가 없어 항상 0 이다.
    """
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    # 끊김 동안 번역 2건 — 받을 소켓이 없으므로 이 순단의 유실이다
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await hub.broadcast_to_session("s1", {"sentence": "m2"})

    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()

    assert rec.reconnects == [{"blip_id": 1, "flushed_count": 0, "lost_count": 2}]
    await _teardown(hub)


async def test_listener_still_attached_does_not_count_as_lost():
    """청취자가 남아 자막을 받고 있으면 다른 소켓의 순단에 유실을 적으면 안 된다.

    P1 에서 새로 생긴 구분이다 — 소켓이 1개일 때는 '끊김 = 아무도 못 받음'
    이었지만, 이제 한 명이 끊겨도 나머지가 받고 있으면 그 문장은 유실이 아니다.
    """
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()

    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1, close_code=1006)
    await _drain()

    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    assert ws2.sent == ["m1"]

    ws3 = FakeWS()
    await _attach(hub, ws3, "s1")
    await _drain()

    assert rec.reconnects == [{"blip_id": 1, "flushed_count": 0, "lost_count": 0}]
    await _teardown(hub)


@pytest.mark.skip(
    reason="P1 D3: 백로그(_pending) 제거로 캡 자체가 없어짐. P3(백로그 커서)에서 "
           "링버퍼가 돌아오면 복원 — 계획 docs/multi-listener-p1-plan.md §9"
)
async def test_pending_overflow_during_blip_counts_lost():
    """blip 동안 pending 캡 초과로 버린 건수는 lost_count 로 집계돼야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    hub._max_pending = 2
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    for i in range(4):  # 캡 2 → 2건 드롭
        await hub.broadcast_to_session("s1", {"sentence": f"m{i}"})

    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()

    assert rec.reconnects == [{"blip_id": 1, "flushed_count": 2, "lost_count": 2}]
    await _teardown(hub)


async def test_different_session_never_closes_another_sessions_blip():
    """다른 세션의 접속은 남의 열린 blip 을 건드리면 안 된다.

    P1 이전에는 슬롯이 하나라 s2 의 접속이 s1 의 슬롯을 빼앗고, 그 과정에서
    s1 의 blip 이 미복귀로 마감됐다. 이제 두 세션이 공존하므로 s1 의 blip 은
    **열린 채로 남아** 자기 세션의 재접속이나 종료를 기다려야 한다.
    (결정 2: reconnected_at NULL 잔존 = 미복귀)
    """
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    ws2 = FakeWS()
    await _attach(hub, ws2, "s2")
    await _drain()

    # s2 의 접속은 s1 의 blip 을 닫지도(reconnect) 마감하지도(abandon) 않는다
    assert rec.reconnects == []
    assert rec.abandons == []

    # s1 이 끝나야 비로소 미복귀로 마감된다
    await hub.detach("s1")
    await _drain()
    assert rec.reconnects == []
    assert rec.abandons == [{"blip_id": 1, "lost_count": 0}]
    await _teardown(hub)


async def test_detach_releases_slot_so_next_session_records_its_blip():
    """세션이 순단 상태로 종료되면 슬롯이 반납돼, 다음 세션의 순단도
    기록돼야 한다 (§11 F-1 — 2026-08-02 순단 3회 중 2회 누락의 근인)."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    await hub.detach("s1")  # 재접속 없이 종료 (stop 호출)
    ws2 = FakeWS()
    await _attach(hub, ws2, "s2")
    await _drain()
    ws2.client_disconnect()
    await hub.handle_client_disconnect("s2", ws2, close_code=1006)
    await _drain()

    assert [d["session_id"] for d in rec.disconnects] == ["s1", "s2"]
    await _teardown(hub)


async def test_detach_marks_open_blip_as_never_recovered():
    """미복귀 종료 시 열린 행은 reconnected_at NULL 로 두되, 다시는 방류되지
    않을 pending 을 lost_count 로 마감해야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await hub.broadcast_to_session("s1", {"sentence": "m2"})

    await hub.detach("s1")
    await _drain()

    assert rec.reconnects == []
    assert rec.abandons == [{"blip_id": 1, "lost_count": 2}]
    await _teardown(hub)


async def test_next_session_reconnect_does_not_close_previous_blip():
    """다음 세션의 재접속이 이전 세션의 행을 닫으면 안 된다
    (§11 F-1 — prod 행 2 가 3.6일·타 세션으로 찍힌 경로)."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()
    await hub.detach("s1")
    await _drain()

    ws2 = FakeWS()
    await _attach(hub, ws2, "s2")  # 새 세션 첫 접속
    await _drain()
    ws2.client_disconnect()  # s2 자신의 순단
    await hub.handle_client_disconnect("s2", ws2, close_code=1006)
    await _drain()
    ws3 = FakeWS()
    await _attach(hub, ws3, "s2")  # s2 의 재접속
    await _drain()

    # s2 의 재접속은 자기 blip(2)만 닫는다 — s1 의 blip 1 은 미복귀로 남는다
    assert [r["blip_id"] for r in rec.reconnects] == [2]
    assert rec.abandons == [{"blip_id": 1, "lost_count": 0}]
    await _teardown(hub)


async def test_multiple_blips_in_one_session_each_get_a_row():
    """한 세션에서 순단이 여러 번 나면 행도 그만큼 생겨야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    for _ in range(3):
        ws = FakeWS()
        await _attach(hub, ws, "s1")
        await _drain()
        ws.client_disconnect()
        await hub.handle_client_disconnect("s1", ws, close_code=1006)
        await _drain()

    assert len(rec.disconnects) == 3
    assert [r["blip_id"] for r in rec.reconnects] == [1, 2]
    await _teardown(hub)


async def test_reconnect_closes_blip_after_keepalive_loop_is_gone():
    """살피던 keepalive 가 이미 끝난 뒤에 온 재접속도 blip 을 종결해야 한다.

    P1 이전에는 keepalive 가 300초 뒤 재연결 대기를 포기하는 상태가 있었고,
    그 뒤의 재접속이 blip 을 닫는지가 §11 F-1 의 쟁점이었다. 소켓별 _Conn 이
    된 지금은 대기 상태 자체가 없고 죽은 소켓의 루프는 그냥 끝난다 — 그래도
    '열린 blip 은 세션이 살아있는 한 재접속을 기다린다' 는 약속은 같다.
    """
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    # 그 소켓을 살피던 루프가 완전히 끝난 상태 재현
    assert all(c.keepalive_task is None or c.keepalive_task.done()
               for c in hub._conns.values())

    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()

    assert rec.reconnects == [{"blip_id": 1, "flushed_count": 0, "lost_count": 0}]
    await _teardown(hub)


async def test_keepalive_detected_blip_has_no_close_code():
    """서버발 끊음(keepalive 타임아웃)은 detected_by=keepalive_timeout,
    close code 없음으로 기록돼야 한다."""
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    client_id = await _attach(hub, ws, "s1")
    await _drain()

    # keepalive 루프가 pong 타임아웃 시 호출하는 중앙 경로
    await hub._drop_conn(hub._conns[ws], detected_by="keepalive_timeout")
    await _drain()

    assert rec.disconnects == [{
        "id": 1,
        "session_id": "s1",
        "client_id": client_id,
        "close_code": None,
        "close_reason": None,
        "detected_by": "keepalive_timeout",
    }]
    await _teardown(hub)


async def test_duplicate_disconnect_notice_does_not_record_a_second_blip():
    """이미 정리된 소켓의 늦은/중복 끊김 통지는 blip 을 또 열면 안 된다.

    P1 이전에는 '새 소켓으로 교체된 뒤의 옛 소켓 통지' 가 이 경로였다. 이제
    소켓이 공존하므로 옛 소켓의 통지는 stale 이 아니라 정당한 통지다 —
    stale 인 것은 **같은 소켓의 두 번째 통지**뿐이다 (main.py 의 끊김 통지와
    keepalive 감지가 겹칠 수 있다).
    """
    rec = FakeRecorder()
    hub = WebSocketHub(blip_recorder=rec)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()

    assert len(rec.disconnects) == 1
    await _teardown(hub)


async def test_hub_without_recorder_still_works():
    """recorder 미주입(테스트·구버전 조립)이어도 hub 동작은 그대로여야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)

    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()

    assert hub.listener_count() == 1
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    assert ws2.sent == ["m1"]
    await _teardown(hub)
