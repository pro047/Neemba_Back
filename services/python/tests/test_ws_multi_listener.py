"""멀티 청취자 P1 (`docs/multi-listener-p1-plan.md`): 세션당 소켓 N개.

P1 이전의 허브는 소켓 슬롯이 1개라, 두 번째 청취자가 attach 하면 첫 번째
소켓을 닫았다 (`websocket.py:64-65`). 이 파일은 그 전제를 뒤집은 뒤에도
기존 불변식이 소켓 '집합' 위에서 그대로 성립하는지 고정한다.

계획 §5-2 의 대조군: P1 구현 전에는 아래 테스트가 실패해야 하고,
실패하지 않으면 계획 §1 의 원인 분석이 틀린 것이다.

허브의 공개 API(attach/detach/broadcast_to_session/handle_client_disconnect)
로만 쓴다 — 내부 자료구조는 P1 에서 갈아엎히므로 여기에 묶지 않는다.
"""
import asyncio
import contextlib
from types import SimpleNamespace

from starlette.websockets import WebSocketState

from src.monitoring import metrics
from src.ws.websocket import CLOSE_SESSION_NOT_FOUND, WebSocketHub

# ruff TRY003 회피용 상수 (raise 에 문자열 리터럴 직접 전달 금지)
_WS_BOOM = "receive failed after the socket died"


class FakeWS:
    """starlette WebSocket 의 client/application 두-상태 semantics 모사."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTING
        self.sent: list[str] = []
        # 거절 경로가 어떤 close code 로 닫았는지 — 1008(파라미터 오류)과
        # 4404(세션 없음)가 갈리는지 보려면 값을 들고 있어야 한다.
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        if (
            self.client_state != WebSocketState.CONNECTED
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
        self.close_code = code
        self.application_state = WebSocketState.DISCONNECTED

    def client_disconnect(self) -> None:
        # 클라 주도 끊김: starlette 는 client_state 만 바꾼다 (§4-3 원인 2)
        self.client_state = WebSocketState.DISCONNECTED


class BlockingWS(FakeWS):
    """send 가 ``release`` 될 때까지 매달리는 소켓 — 느린 청취자 모사.

    소켓별 send gate 불변식(계획 §7)의 반증 장치다. 게이트가 전역이면 이
    소켓이 게이트를 쥔 동안 다른 청취자의 자막도 함께 멈춘다.
    """

    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def send_text(self, text: str) -> None:
        self.entered.set()
        await self._gate.wait()
        await super().send_text(text)

    def release(self) -> None:
        self._gate.set()


async def _drain(n: int = 10) -> None:
    """fire-and-forget create_task(_send_text 등) 소진용 양보."""
    for _ in range(n):
        await asyncio.sleep(0)


async def _attach(hub: WebSocketHub, ws, session_id: str, **kwargs):
    """라이브 세션에 청취자를 붙인다 — 프로덕션의 start → /ws 순서 그대로.

    ``attach`` 는 화이트리스트라 ``register_session`` 되지 않은 sessionId 를
    거절한다. 여기서 재현하려는 것은 '방송 중인 세션에 청취자가 붙는다' 이므로
    등록을 먼저 한다 (프로덕션에서는 node 가 `/internal/sessions/start` 로 먼저
    친다 — `main.py:522`).

    **거절 자체를 보는 테스트는 이 헬퍼를 쓰지 말고 ``hub.attach`` 를 직접
    부른다.** 그게 이 헬퍼의 존재 이유다 — 안전장치를 무르는 것이 아니라
    테스트마다 '이 세션은 라이브인가' 를 명시하게 만드는 것.
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


async def test_second_listener_joins_without_evicting_the_first():
    """같은 세션에 소켓 2개가 붙으면 브로드캐스트 1회를 둘 다 받아야 한다.

    계획 §5-2 대조군 — P1 이전에는 두 번째 attach 가 첫 소켓을 닫으므로
    ws1 이 아무것도 못 받고 application_state 도 DISCONNECTED 가 된다.
    """
    # Arrange
    hub = WebSocketHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _drain()
    await _attach(hub, ws2, "s1")
    await _drain()

    # Act
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    # Assert
    assert ws1.application_state == WebSocketState.CONNECTED, (
        "두 번째 청취자의 attach 가 첫 소켓을 닫으면 안 된다"
    )
    assert ws1.sent == ["m1"]
    assert ws2.sent == ["m1"]
    await _teardown(hub)


async def test_one_listener_dropping_does_not_stop_the_others():
    """한 청취자가 끊겨도 남은 청취자는 계속 자막을 받아야 한다."""
    # Arrange
    hub = WebSocketHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()

    # Act
    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1)
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    # Assert
    assert ws2.sent == ["m1"]
    assert ws1.sent == []
    await _teardown(hub)


async def test_slow_listener_does_not_block_the_others():
    """느린 청취자가 send 에 매달려도 다른 청취자는 먼저 받아야 한다.

    계획 §7: send gate 는 소켓별이어야 한다. 전역 세마포어면 여기서
    head-of-line blocking 이 나 ws_fast 가 굶는다.
    """
    # Arrange
    hub = WebSocketHub()
    slow, fast = BlockingWS(), FakeWS()
    await _attach(hub, slow, "s1")
    await _attach(hub, fast, "s1")
    await _drain()

    # Act
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    # Assert
    assert slow.application_state == WebSocketState.CONNECTED, (
        "느린 청취자가 attach 단계에서 쫓겨나면 이 테스트는 의미가 없다"
    )
    assert slow.entered.is_set(), "느린 소켓의 send 가 진입은 해야 한다"
    assert fast.sent == ["m1"], "느린 청취자가 다른 청취자를 막으면 안 된다"

    slow.release()
    await _drain()
    await _teardown(hub)


async def test_other_sessions_translation_never_leaks_to_listeners():
    """다른 세션의 번역은 어느 청취자에게도 가면 안 된다 (에러B stale drop).

    소켓이 집합이 돼도 계획 §7 의 stale drop 규약은 그대로다.
    """
    # Arrange
    hub = WebSocketHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()

    # Act
    await hub.broadcast_to_session("other", {"sentence": "leak"})
    await _drain()

    # Assert
    assert ws1.sent == []
    assert ws2.sent == []
    await _teardown(hub)


async def test_ws_error_on_one_socket_does_not_take_the_session_down():
    """한 소켓에서 예외가 나도 나머지 청취자와 세션은 살아 있어야 한다.

    ``/ws`` 의 일반 예외 경로(끊긴 뒤의 ``receive_text`` RuntimeError 등)가
    ``detach`` 를 부르면 그 세션의 **모든** 소켓이 닫히고 세션이 지워진다 —
    이후 번역은 전부 stale drop 이고, 살아나려면 OBS 를 껐다 켜야 한다.
    P1 이 없애려던 증상이 에러 경로에 그대로 남아 있던 자리다.

    엔드포인트 함수를 직접 부른다 — 이 분기는 클라 쪽에서 유발할 수 없다.
    """
    # Arrange
    import main

    hub = WebSocketHub()
    healthy = FakeWS()
    await _attach(hub, healthy, "s1")
    await _drain()

    class _ExplodingWS(FakeWS):
        """접속 인사 뒤 첫 receive 에서 터지는 소켓."""

        app = SimpleNamespace(state=SimpleNamespace(hub=hub))
        query_params = {"sessionId": "s1"}

        async def receive_text(self) -> str:
            raise RuntimeError(_WS_BOOM)

    broken = _ExplodingWS()

    # Act
    await main.websocket_endpoint(broken)
    await _drain()
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()

    # Assert
    # 접속 인사는 붙은 소켓에만 간다 — 그래서 healthy 에는 자막만 남는다.
    assert broken.sent == ["Connect!"]
    assert healthy.sent == ["m1"], (
        "한 소켓의 예외가 다른 청취자의 자막을 끊으면 안 된다"
    )
    assert healthy.application_state == WebSocketState.CONNECTED
    assert hub._sessions == {"s1": "en-us"}, "세션이 살아 있어야 한다"
    assert hub.listener_count() == 1
    await _teardown(hub)


async def test_attach_to_a_session_that_ended_is_rejected():
    """종료된 세션으로 재접속하면 등재하지 말고 4404 로 거절해야 한다.

    앱은 ``onDone`` 에서 같은 webSocketUrl(= 죽은 sessionId 포함)로 12회
    자동 재연결한다 (`mvp/lib/ws_client.dart:10`). 소켓이 자기를 라이브로
    등재할 수 있으면 **매 예배 종료가** 세션을 되살리고, 그때부터
    ① 종료된 세션 앞으로 늦게 온 번역이 그 재접속자에게 배달되며(§7 에러B)
    ② 대응하는 detach 가 영영 오지 않아 active_session 이 안 떨어진다.
    """
    # Arrange
    hub = WebSocketHub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1")
    await _drain()
    await hub.detach("s1")
    await _drain()

    # Act — 앱의 자동 재연결. 헬퍼를 쓰지 않는다: 등재 정책 자체가 대상이다.
    ws2 = FakeWS()
    client_id = await hub.attach(ws2, "s1")
    await _drain()
    await hub.broadcast_to_session("s1", {"sentence": "late"})
    await _drain()

    # Assert
    assert client_id is None
    assert ws2.close_code == CLOSE_SESSION_NOT_FOUND
    assert hub._sessions == {}, "거절된 소켓이 세션을 되살리면 안 된다"
    assert ws2.sent == [], "종료된 세션의 늦은 번역이 배달되면 안 된다"
    assert hub.listener_count() == 0
    await _teardown(hub)


async def test_rejected_attach_does_not_pin_the_active_session_gauge():
    """유령 세션 접속이 있어도 라이브 세션이 끝나면 게이지가 0 이 돼야 한다.

    ``neemba_hub_active_session`` 이 0 으로 떨어지는지가 D4(수동 stop 제거)의
    유일한 반증 조건이다 (계획 §10-2). 등재를 소켓에 허용하면 그 계기판이
    구조적으로 0 에 못 가고, D4 가 안전한지 확인할 방법 자체가 없어진다.
    """
    # Arrange
    hub = WebSocketHub()
    live, ghost = FakeWS(), FakeWS()
    await _attach(hub, live, "s_live")
    await _drain()

    # Act
    assert await hub.attach(ghost, "s_dead") is None
    await hub.detach("s_live")
    await _drain()

    # Assert
    assert metrics.get_snapshot()["active_session"] is False
    await _teardown(hub)


async def test_broadcast_keeps_a_strong_reference_to_its_send_tasks():
    """브로드캐스트 태스크는 완료 전까지 허브가 참조를 들고 있어야 한다.

    ``create_task`` 의 반환값을 아무도 안 잡으면 이벤트 루프는 약한 참조만
    남기고, GC 가 그 태스크를 거둬가면 그 청취자의 자막 1건이 조용히 사라진다.
    D3 으로 재적재가 없어져 복구 경로도 없다 (`pushClient/pusher.py:49` 가
    같은 이유로 같은 관용구를 쓴다). GC 자체는 결정적으로 재현할 수 없으므로
    '참조를 보관한다' 는 계약만 고정한다.
    """
    # Arrange
    hub = WebSocketHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()
    assert hub._tasks == set(), "선행 태스크가 남아 있으면 아래 수를 못 믿는다"

    # Act
    await hub.broadcast_to_session("s1", {"sentence": "m1"})

    # Assert — 소켓마다 1개, 아직 아무것도 안 돌았다
    assert len(hub._tasks) == 2
    await _drain()
    assert ws1.sent == ["m1"] and ws2.sent == ["m1"]
    # 끝난 태스크는 스스로 빠진다 — 안 그러면 예배 내내 set 이 자란다
    assert hub._tasks == set()
    await _teardown(hub)


async def test_detach_closes_every_socket_of_the_session():
    """세션이 끝나면 그 세션의 모든 소켓이 닫혀야 한다."""
    # Arrange
    hub = WebSocketHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()

    # Act
    await hub.detach("s1")
    await _drain()

    # Assert
    assert ws1.application_state == WebSocketState.DISCONNECTED
    assert ws2.application_state == WebSocketState.DISCONNECTED
    await hub.broadcast_to_session("s1", {"sentence": "after"})
    await _drain()
    assert ws1.sent == []
    assert ws2.sent == []
    await _teardown(hub)
