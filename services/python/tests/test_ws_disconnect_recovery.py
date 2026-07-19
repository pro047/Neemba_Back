"""§4-3 (handover-2026-07-18): /ws 끊김 인지 지연으로 인한 번역 유실 방지.

2026-07-19 예배 장애의 3중 결함을 각각 고정한다:
  1) main.py 의 ``except WebSocketDisconnect`` 가 hub 에 통지하지 않아
     다음 keepalive 틱(최대 30s)까지 죽은 소켓이 슬롯에 남음
     → ``handle_client_disconnect`` 즉시 통지 (pending 보존).
  2) starlette 는 클라 주도 끊김 시 ``client_state`` 만 DISCONNECTED 로
     바꾸고 ``application_state`` 는 CONNECTED 로 남김 → 한쪽만 보는
     연결 검사가 전부 통과해 죽은 소켓에 send 반복 시도
     → 연결 검사에 client_state 병용.
  3) send 실패 문장을 pending 에 재적재하지 않아 즉시 유실
     → 실패 문장 재적재 후 재접속 ``_flush_pending`` 으로 방류.

FakeWS 가 starlette 의 두-상태 semantics 를 그대로 모사하므로 실제
서버/브라우저 없이 세 결함을 결정적으로 재현한다.
"""
import asyncio
import contextlib

from starlette.websockets import WebSocketState

from src.ws.websocket import WebSocketHub


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
    """fire-and-forget create_task(_send_text/_flush_pending) 소진용 양보."""
    for _ in range(n):
        await asyncio.sleep(0)


async def _teardown(hub: WebSocketHub) -> None:
    task = hub._keepalive_task
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_disconnect_notifies_hub_and_queues_after():
    """원인 1: 끊김 통지 즉시 슬롯이 비고(세션·pending 은 유지),
    이후 번역은 send 시도 없이 pending 으로 큐잉되어야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws)

    assert hub.client is None
    assert hub._session_id == "s1"  # detach 가 아니므로 세션은 유지
    assert hub._reconnect_waiting is True

    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    assert list(hub._pending) == ["m1"]
    assert ws.sent == []
    await _teardown(hub)


async def test_broadcast_checks_client_state():
    """원인 2: client_state 만 죽고 application_state 가 CONNECTED 로 남은
    소켓(통지가 아직 안 온 창)에는 send 시도 대신 pending 큐잉해야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.client_disconnect()  # hub 통지 전의 지연 창을 모사
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    assert "m1" in hub._pending
    assert ws.sent == []
    await _teardown(hub)


async def test_failed_send_requeued():
    """원인 3: 상태 검사는 통과했는데 send 자체가 실패한 문장(순단)은
    유실 대신 pending 재적재되어야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "s1")
    await _drain()

    ws.fail_sends = True
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    assert "m1" in hub._pending
    await _teardown(hub)


async def test_reconnect_flushes_pending_in_order():
    """끊김~재접속 사이에 쌓인 번역이 새 소켓으로 순서대로 방류된다."""
    hub = WebSocketHub()
    ws1 = FakeWS()
    await hub.attach(ws1, "s1")
    await _drain()

    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1)
    for i in range(3):
        await hub.broadcast_to_session("s1", {"sentence": f"m{i}"})
    await _drain()
    assert len(hub._pending) == 3

    ws2 = FakeWS()
    await hub.attach(ws2, "s1")
    await _drain(30)

    assert ws2.sent == ["m0", "m1", "m2"]
    assert not hub._pending
    await _teardown(hub)


async def test_stale_disconnect_notice_ignored():
    """이미 새 소켓으로 교체된 뒤 도착한 옛 소켓의 끊김 통지는 무시된다."""
    hub = WebSocketHub()
    ws1 = FakeWS()
    await hub.attach(ws1, "s1")
    await _drain()
    ws2 = FakeWS()
    await hub.attach(ws2, "s1")  # 재접속으로 소켓 교체
    await _drain()

    await hub.handle_client_disconnect("s1", ws1)
    assert hub.client is ws2
    await _teardown(hub)


async def test_detach_still_clears_pending():
    """회귀 가드: 세션 종료(detach)는 여전히 pending 을 비우고,
    종료 후 늦게 도착한 실패-재적재도 큐를 되살리지 못한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "s1")
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
