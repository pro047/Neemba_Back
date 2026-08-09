"""§4-3 (handover-2026-07-18): /ws 끊김 인지 지연으로 인한 번역 유실 방지.

2026-07-19 예배 장애의 3중 결함을 각각 고정한다:
  1) main.py 의 ``except WebSocketDisconnect`` 가 hub 에 통지하지 않아
     다음 keepalive 틱(최대 30s)까지 죽은 소켓이 슬롯에 남음
     → ``handle_client_disconnect`` 즉시 통지.
  2) starlette 는 클라 주도 끊김 시 ``client_state`` 만 DISCONNECTED 로
     바꾸고 ``application_state`` 는 CONNECTED 로 남김 → 한쪽만 보는
     연결 검사가 전부 통과해 죽은 소켓에 send 반복 시도
     → 연결 검사에 client_state 병용.
  3) send 실패 문장을 pending 에 재적재하지 않아 즉시 유실
     → 실패 문장 재적재 후 재접속 ``_flush_pending`` 으로 방류.

FakeWS 가 starlette 의 두-상태 semantics 를 그대로 모사하므로 실제
서버/브라우저 없이 세 결함을 결정적으로 재현한다.

**P1(멀티 청취자) 이후**: 결함 3 의 방어책이던 백로그(``_pending``)를
계획 D3 으로 들어냈다. 커서 없이 소켓 N개에 백로그를 태우면 붙어 있던
청취자에게 중복 전송이 되고, 커서를 넣으면 전송 포맷이 JSON 으로 바뀌어
앱 배포가 필요해지기 때문이다.

그래서 아래 백로그 4건은 **지우지 않고 skip 으로 보존한다** — 이 테스트들이
지키던 것은 '순단 구간 자막을 잃지 않는다' 이고, 그건 포기가 아니라 P3
(백로그 커서)로 이월된 약속이다. P3 에서 ``SessionBacklog`` 가 들어오면
skip 마커를 떼고 되살린다. 결함 1·2 의 회귀 방지는 백로그와 무관하므로
새 API 로 그대로 살아 있다.
"""
import asyncio
import contextlib

import pytest
from starlette.websockets import WebSocketState

from src.ws.websocket import WebSocketHub

# P3 에서 이 마커를 떼면 아래 4건이 되살아난다 (계획 §7·§9).
_BACKLOG_DEFERRED = pytest.mark.skip(
    reason="P1 D3: 백로그(_pending) 제거로 무효. P3(백로그 커서)에서 복원 — "
           "계획 docs/multi-listener-p1-plan.md §9"
)


class FakeWS:
    """starlette WebSocket 의 client/application 두-상태 semantics 모사."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTING
        self.sent: list[str] = []
        self.fail_sends = False

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        if (
            self.fail_sends
            or self.client_state != WebSocketState.CONNECTED
            or self.application_state != WebSocketState.CONNECTED
        ):
            raise RuntimeError(
                'Unexpected ASGI message "websocket.send", after sending '
                '"websocket.close" or response already completed.'
            )
        self.sent.append(text)

    async def send_json(self, data) -> None:
        await self.send_text(str(data))

    async def close(self, code: int = 1000) -> None:
        self.application_state = WebSocketState.DISCONNECTED

    def client_disconnect(self) -> None:
        # 클라 주도 끊김: starlette 는 client_state 만 바꾼다 (§4-3 원인 2)
        self.client_state = WebSocketState.DISCONNECTED


async def _drain(n: int = 10) -> None:
    """fire-and-forget create_task(_send_text 등) 소진용 양보."""
    for _ in range(n):
        await asyncio.sleep(0)


async def _attach(hub: WebSocketHub, ws, session_id: str, **kwargs):
    """라이브 세션에 청취자를 붙인다 — 프로덕션의 start → /ws 순서 그대로.

    ``attach`` 는 화이트리스트라 ``register_session`` 되지 않은 sessionId 를
    거절한다. 여기의 순단·재접속 시나리오는 전부 '세션은 살아 있는데 소켓만
    끊겼다' 이므로 등록을 먼저 한다 — 그게 detach 와의 차이를 재는 전제다.
    """
    await hub.register_session(session_id, kwargs.get("target_lang"))
    return await hub.attach(ws, session_id, **kwargs)


async def _teardown(hub: WebSocketHub) -> None:
    for conn in list(hub._conns.values()):
        task = conn.keepalive_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_disconnect_notice_drops_the_socket_but_keeps_the_session():
    """끊김을 통지하면 그 소켓만 명부에서 빠지고 세션은 살아 있어야 한다.

    §4-3 원인 1 의 회귀 방지. 백로그와 무관한 절반이라 D3 이후에도 유효하다.
    detach 와 달리 세션을 끝내지 않는다는 것이 핵심 — P1 에서는 나머지
    청취자가 계속 들어야 하므로 더 중요해졌다.
    """
    # Arrange
    hub = WebSocketHub()
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    assert hub.listener_count() == 1

    # Act
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws)
    await _drain()

    # Assert
    assert hub.listener_count() == 0
    assert hub.is_client_connected() is False
    # 세션이 살아 있어야 재접속이 stale drop 되지 않는다.
    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    assert ws2.sent == ["m1"]
    await _teardown(hub)


async def test_broadcast_checks_client_state():
    """원인 2: client_state 만 죽고 application_state 가 CONNECTED 로 남은
    소켓(통지가 아직 안 온 창)에는 send 를 시도하지 않아야 한다."""
    # Arrange
    hub = WebSocketHub()
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    # Act
    ws.client_disconnect()  # hub 통지 전의 지연 창을 모사
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    # Assert — send 를 시도했다면 FakeWS 가 RuntimeError 를 냈을 것이다.
    assert ws.sent == []
    await _teardown(hub)


async def test_stale_disconnect_notice_ignored():
    """이미 정리된 소켓의 끊김 통지가 뒤늦게 와도 남은 청취자를 건드리면 안 된다.

    P1 이전에는 '새 소켓으로 교체된 뒤' 였지만, 이제 두 소켓은 공존한다 —
    통지의 주인만 정확히 내려가야 한다.
    """
    # Arrange
    hub = WebSocketHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()

    # Act
    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1)
    await hub.handle_client_disconnect("s1", ws1)  # 중복 통지
    await _drain()

    # Assert
    assert hub.listener_count() == 1
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    assert ws2.sent == ["m1"]
    await _teardown(hub)


@_BACKLOG_DEFERRED
async def test_disconnect_notifies_hub_and_queues_after():
    """원인 1: 끊김 통지 이후의 번역이 pending 으로 큐잉되어야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws)

    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    assert list(hub._pending) == ["m1"]
    assert ws.sent == []
    await _teardown(hub)


@_BACKLOG_DEFERRED
async def test_failed_send_requeued():
    """원인 3: 상태 검사는 통과했는데 send 자체가 실패한 문장(순단)은
    유실 대신 pending 재적재되어야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()

    ws.fail_sends = True
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    assert "m1" in hub._pending
    await _teardown(hub)


@_BACKLOG_DEFERRED
async def test_reconnect_flushes_pending_in_order():
    """끊김~재접속 사이에 쌓인 번역이 새 소켓으로 순서대로 방류된다."""
    hub = WebSocketHub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1")
    await _drain()

    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1)
    for i in range(3):
        await hub.broadcast_to_session("s1", {"sentence": f"m{i}"})
    await _drain()
    assert len(hub._pending) == 3

    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain(30)

    assert ws2.sent == ["m0", "m1", "m2"]
    assert not hub._pending
    await _teardown(hub)


@_BACKLOG_DEFERRED
async def test_detach_still_clears_pending():
    """회귀 가드: 세션 종료(detach)는 여전히 pending 을 비우고,
    종료 후 늦게 도착한 실패-재적재도 큐를 되살리지 못한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws)
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    assert hub._pending

    await hub.detach("s1")
    assert not hub._pending
    assert hub._session_id is None

    await hub._requeue("s1", "late")
    assert not hub._pending
    await _teardown(hub)
