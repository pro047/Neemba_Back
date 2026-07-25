"""monitor-page-v2 WU1: MonitorHub 전역 이벤트 채널(fan-out) 단위 테스트.

``/ws/monitor/events`` 는 sessionId 무관 전역 구독 채널이다. 세션 단위
구독(``attach``/``broadcast``)과 완전히 격리되어야 하고, 죽은 소켓은
기존 세션 브로드캐스트와 동일하게 조용히 드롭되어야 한다 (백로그 없음).

FakeWS 는 test_ws_disconnect_recovery.py 의 starlette 두-상태 semantics
모사를 따르되, 모니터 허브는 send_json 만 쓰므로 payload(dict)를 그대로
기록한다.
"""
from starlette.websockets import WebSocketState

from src.ws.monitor import MonitorHub


class FakeWS:
    """starlette WebSocket 의 client/application 두-상태 semantics 모사."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTING
        self.sent: list[dict] = []
        self.fail_sends = False

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def send_json(self, data: dict) -> None:
        if (
            self.fail_sends
            or self.application_state != WebSocketState.CONNECTED
        ):
            raise RuntimeError(
                'Unexpected ASGI message "websocket.send", after sending '
                '"websocket.close" or response already completed.'
            )
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.application_state = WebSocketState.DISCONNECTED


EVENT = {"type": "session_started", "sessionId": "s1"}


async def test_broadcast_global_reaches_all_global_subscribers():
    """전역 구독자가 여럿이면 broadcast_global 은 전원에게 도달해야 한다."""
    hub = MonitorHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await hub.attach_global(ws1)
    await hub.attach_global(ws2)

    await hub.broadcast_global(EVENT)

    assert (ws1.sent, ws2.sent) == ([EVENT], [EVENT])


async def test_detached_global_subscriber_stops_receiving():
    """detach_global 하면 이후 broadcast_global 을 받지 않아야 한다."""
    hub = MonitorHub()
    ws = FakeWS()
    await hub.attach_global(ws)
    await hub.detach_global(ws)

    await hub.broadcast_global(EVENT)

    assert ws.sent == []


async def test_dead_global_subscriber_is_dropped_silently():
    """send 실패 구독자는 드롭되고 나머지 구독자 전달은 계속되어야 한다."""
    hub = MonitorHub()
    dead, alive = FakeWS(), FakeWS()
    dead.fail_sends = True
    await hub.attach_global(dead)
    await hub.attach_global(alive)

    await hub.broadcast_global(EVENT)

    assert alive.sent == [EVENT]


async def test_dropped_dead_subscriber_not_retried_on_next_broadcast():
    """죽어서 드롭된 구독자는 다음 broadcast_global 에서 send 시도조차
    없어야 한다 (구독자 셋에서 제거됨)."""
    hub = MonitorHub()
    dead = FakeWS()
    dead.fail_sends = True
    await hub.attach_global(dead)
    await hub.broadcast_global(EVENT)  # 1차: 실패 → 드롭

    dead.fail_sends = False  # 재시도가 있었다면 성공했을 상태
    await hub.broadcast_global(EVENT)

    assert dead.sent == []


async def test_broadcast_global_without_subscribers_is_noop():
    """구독자가 없으면 broadcast_global 은 조용히 무시되어야 한다."""
    hub = MonitorHub()

    await hub.broadcast_global(EVENT)  # 예외 없이 반환되면 통과


async def test_global_broadcast_does_not_leak_to_session_subscribers():
    """broadcast_global 은 세션 단위 구독자에게 전달되지 않아야 한다."""
    hub = MonitorHub()
    session_ws = FakeWS()
    await hub.attach("s1", session_ws)

    await hub.broadcast_global(EVENT)

    assert session_ws.sent == []


async def test_session_broadcast_does_not_leak_to_global_subscribers():
    """세션 단위 broadcast 는 전역 구독자에게 전달되지 않아야 한다."""
    hub = MonitorHub()
    global_ws = FakeWS()
    await hub.attach_global(global_ws)

    await hub.broadcast("s1", {"type": "translation", "sessionId": "s1"})

    assert global_ws.sent == []
