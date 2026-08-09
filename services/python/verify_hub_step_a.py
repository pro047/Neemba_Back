"""
허브 단위 검증 스크립트 (docker/NATS/DeepL 불필요).
가짜 WebSocket 으로 attach→broadcast→detach 경쟁을 유발해 에러 A·B·C 와
세션 게이팅(교차 종료/교차 전송 차단), 그리고 멀티 청취자 P1 의 소켓 격리를
확인한다.

실행: uv run python verify_hub_step_a.py
의존성: fastapi, starlette 만 필요 (허브가 deepl/nats 를 import 하지 않으므로).

pytest 스위트(tests/)와 역할이 다르다: 여기는 **실시간 경쟁**을 일부러 만드는
곳이라 실제 이벤트 루프에서 sleep 을 태운다. 결정적 단위 검증은 tests/ 가 하고,
이 파일은 '락·게이트가 진짜 경쟁에서 버티나' 를 손으로 볼 때 쓴다.

이력:
- 2026-07-18(§4-3) 두-상태 연결 검사가 들어오면서 FakeWS 에 client_state 가
  없어 **이 스크립트는 그때부터 실행 즉시 AttributeError 로 죽어 있었다.**
  P1 갱신과 함께 그 결함도 고쳤다(FakeWS 가 두 상태를 모두 든다).
- 2026-08-08(P1) 허브가 소켓 집합이 되면서 hub.client / _session_id /
  _pending / _reconnect_waiting 이 사라졌다. 그 셋에 붙어 있던 검사는
  **소켓 격리·유실 집계로 옮겼다** — 지운 게 아니다.
"""
import asyncio

from starlette.websockets import WebSocketState
from src.ws.websocket import WebSocketHub


class FakeWS:
    """허브가 호출하는 최소 인터페이스만 구현한 가짜 소켓."""

    def __init__(self) -> None:
        # §4-3: 허브의 연결 검사는 두 상태를 함께 본다. 하나만 두면 검사가
        # AttributeError 로 터진다 — 이 스크립트가 오래 죽어 있던 이유다.
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTING
        self.sent_text: list[str] = []
        self.sent_json: list[dict] = []
        self.closed = False
        # REV-4: send 가 ASGI 로 흘러가는 '도중' close 가 끼어든 횟수(=에러A).
        self.overlap_errors = 0

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        # starlette 가 close 이후 send 시 내는 에러를 모사.
        if (
            self.client_state != WebSocketState.CONNECTED
            or self.application_state != WebSocketState.CONNECTED
        ):
            raise RuntimeError(
                "Unexpected ASGI message 'websocket.send', "
                "after sending 'websocket.close'")
        # REV-4: send 를 ASGI 채널로 흘리는 동안 yield → 이 틈에 close 가 끼어들면
        # 실제 starlette 처럼 'close 이후 send' 가 된다. 게이트 직렬화가 없으면 재현.
        await asyncio.sleep(0)
        if self.closed:
            self.overlap_errors += 1
            raise RuntimeError(
                "Unexpected ASGI message 'websocket.send', "
                "after sending 'websocket.close'")
        self.sent_text.append(text)

    async def send_json(self, obj: dict) -> None:
        if self.application_state != WebSocketState.CONNECTED:
            raise RuntimeError("Unexpected ASGI message 'websocket.send'")
        self.sent_json.append(obj)

    async def close(self, code: int = 1000) -> None:
        # 내부 await 없음(원자적): close 가 시작되면 같은 틱에 상태를 확정한다.
        self.application_state = WebSocketState.DISCONNECTED
        self.closed = True

    def client_disconnect(self) -> None:
        # 클라 주도 끊김: starlette 는 client_state 만 바꾼다 (§4-3 원인 2)
        self.client_state = WebSocketState.DISCONNECTED


class BlockingWS(FakeWS):
    """send 가 release 될 때까지 매달리는 소켓 — 느린 청취자 모사 (P1)."""

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


results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))


async def pump(n: int = 5) -> None:
    """스케줄된 fire-and-forget task 들이 돌도록 이벤트 루프를 양보."""
    for _ in range(n):
        await asyncio.sleep(0)


def listeners(hub: WebSocketHub, session_id: str) -> list:
    """그 세션에 등재된 소켓들 (P1: hub.client 를 대신하는 관찰 지점)."""
    return [c.ws for c in hub._conns.values() if c.session_id == session_id]


async def attach(hub: WebSocketHub, ws, session_id: str, **kwargs):
    """라이브 세션에 청취자를 붙인다 — 프로덕션의 start → /ws 순서 그대로.

    ``hub.attach`` 는 화이트리스트다: ``register_session`` 되지 않은 sessionId
    는 등재하지 않고 4404 로 닫는다. 이 스크립트가 재는 것은 경쟁 조건이지
    등재 정책이 아니므로 세션을 먼저 라이브로 만든다.
    """
    await hub.register_session(session_id, kwargs.get("target_lang"))
    return await hub.attach(ws, session_id, **kwargs)


async def test_error_A_toctou() -> None:
    """에러A: broadcast 가 _send_text task 를 예약한 직후 우리 쪽이 close.
    게이트 안 재확인이 닫힌 소켓으로의 send 를 막아야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await attach(hub, ws, "A")
    await pump()
    ws.sent_text.clear()

    # broadcast 는 CONNECTED 를 보고 _send_text task 를 예약한다.
    await hub.broadcast_to_session("A", {"sentence": "race"})
    # task 가 실행되기 전에 우리 쪽이 소켓을 닫음 (detach/keepalive 와 동일 상황).
    ws.application_state = WebSocketState.DISCONNECTED
    await pump()

    check("A: closed 소켓에 send 안 함", ws.sent_text == [],
          f"sent_text={ws.sent_text}")
    await hub.detach("A")


async def test_detach_cross_session() -> None:
    """분리: 다른 세션의 stop 이 현재 소켓을 끊으면 안 된다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await attach(hub, ws, "A")
    await pump()

    await hub.detach("B")  # 엉뚱한 세션 stop → 무시되어야 함
    check("분리: detach(B) 가 A 소켓 안 끊음",
          listeners(hub, "A") == [ws] and not ws.closed)

    await hub.detach("A")  # 올바른 세션 stop → 정리
    check("분리: detach(A) 가 세션을 비움",
          listeners(hub, "A") == [] and ws.closed)


async def test_broadcast_stale_drop() -> None:
    """에러B: 라이브가 아닌 세션의 번역은 큐잉 없이 drop."""
    hub = WebSocketHub()
    ws = FakeWS()
    await attach(hub, ws, "A")
    await pump()
    ws.sent_text.clear()

    await hub.broadcast_to_session("OLD", {"sentence": "stale"})
    await pump()
    # D3 으로 pending 이 사라졌으므로 '큐잉 안 함' 은 곧 '전송 안 함' 이다.
    check("B: stale 세션 번역 drop (전송X)",
          ws.sent_text == [], f"sent={ws.sent_text}")
    await hub.detach("A")


async def test_other_session_socket_never_receives() -> None:
    """에러B: 다른 세션의 소켓은 A 의 번역을 절대 받지 않는다.

    P1 이전에는 '세션 교체 시 이전 pending 을 비운다' 로 이 성질을 지켰다.
    소켓이 세션별 채널에 들어가는 지금은 자료구조가 그것을 보장한다 — 그래도
    회귀가 나면 여기서 잡힌다.
    """
    hub = WebSocketHub()
    ws_a, ws_b = FakeWS(), FakeWS()
    await attach(hub, ws_a, "A")
    await attach(hub, ws_b, "B")
    await pump()
    ws_a.sent_text.clear()
    ws_b.sent_text.clear()

    await hub.broadcast_to_session("A", {"sentence": "for-A"})
    await pump()

    check("B: 다른 세션 소켓이 A 텍스트 안 받음",
          ws_a.sent_text == ["for-A"] and ws_b.sent_text == [],
          f"a={ws_a.sent_text} b={ws_b.sent_text}")
    await hub.detach("A")
    await hub.detach("B")


async def test_P1_fanout_to_every_listener() -> None:
    """P1: 같은 세션의 소켓 전부가 브로드캐스트 1회를 받는다.

    이 스크립트의 대조군이다 — P1 이전에는 두 번째 attach 가 첫 소켓을 닫았다.
    """
    hub = WebSocketHub()
    ws1, ws2, ws3 = FakeWS(), FakeWS(), FakeWS()
    for ws in (ws1, ws2, ws3):
        await attach(hub, ws, "A")
    await pump()
    for ws in (ws1, ws2, ws3):
        ws.sent_text.clear()

    await hub.broadcast_to_session("A", {"sentence": "hello"})
    await pump(20)

    check("P1: 청취자 3명 모두 수신 + 아무도 안 닫힘",
          all(ws.sent_text == ["hello"] and not ws.closed
              for ws in (ws1, ws2, ws3)),
          f"1={ws1.sent_text} 2={ws2.sent_text} 3={ws3.sent_text}")
    await hub.detach("A")


async def test_P1_slow_listener_does_not_block_others() -> None:
    """P1: send gate 가 소켓별이라 느린 청취자가 남을 굶기지 않는다.

    게이트를 전역 세마포어로 되돌리면 여기서 head-of-line blocking 이 난다.
    """
    hub = WebSocketHub()
    slow, fast = BlockingWS(), FakeWS()
    await attach(hub, slow, "A")
    await attach(hub, fast, "A")
    await pump()
    fast.sent_text.clear()

    await hub.broadcast_to_session("A", {"sentence": "m1"})
    await pump(20)

    check("P1: 느린 청취자가 다른 청취자를 막지 않음",
          fast.sent_text == ["m1"] and slow.entered.is_set(),
          f"fast={fast.sent_text} slow_entered={slow.entered.is_set()}")
    slow.release()
    await pump(10)
    await hub.detach("A")


async def test_P1_one_drop_does_not_take_the_session_down() -> None:
    """P1: 한 청취자가 끊겨도 세션과 나머지 청취자는 살아 있어야 한다."""
    hub = WebSocketHub()
    ws1, ws2 = FakeWS(), FakeWS()
    await attach(hub, ws1, "A")
    await attach(hub, ws2, "A")
    await pump()
    ws2.sent_text.clear()

    ws1.client_disconnect()
    await hub.handle_client_disconnect("A", ws1)
    await hub.broadcast_to_session("A", {"sentence": "still-here"})
    await pump(20)

    check("P1: 1명 이탈 후에도 나머지 수신",
          ws2.sent_text == ["still-here"] and listeners(hub, "A") == [ws2],
          f"ws2={ws2.sent_text} listeners={len(listeners(hub, 'A'))}")
    await hub.detach("A")


async def test_keepalive_sends_first_ping() -> None:
    """에러C: 첫 pong 을 받기 전에도 서버가 첫 ping 을 무조건 보내야 한다.
    (기존 버그: first_pong 전엔 ping 을 안 보내 데드락.)"""
    orig_sleep = asyncio.sleep

    async def fast_sleep(d):
        # keepalive 의 긴 sleep(30) 만 단축, 나머지는 그대로.
        await orig_sleep(0.01 if d >= 5 else d)

    asyncio.sleep = fast_sleep  # type: ignore
    try:
        hub = WebSocketHub()
        ws = FakeWS()
        await attach(hub, ws, "A")
        # 첫 pong 을 일부러 주지 않는다 (conn.first_pong_received=False 유지).
        await orig_sleep(0.2)  # keepalive 가 최소 1회 돌 시간
        conn = hub._conns.get(ws)
        first_ping = any(m.get("type") == "ping" for m in ws.sent_json)
        check("C: 첫 pong 전에도 첫 ping 발사 (데드락 해소)",
              first_ping and conn is not None and not conn.first_pong_received,
              f"sent_json={ws.sent_json}")
        await hub.detach("A")
        await pump()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore


async def test_REV1_no_cross_send_under_attach_race() -> None:
    """REV-1 회귀: A 의 번역이 다수 쏟아지는 동안 B 가 attach 한다.
    이전 세션 A 의 텍스트가 새 세션 B 의 클라로 새면 안 된다(교차전송 차단)."""
    hub = WebSocketHub()
    ws_a = FakeWS()
    await attach(hub, ws_a, "A")
    await pump()

    ws_b = FakeWS()

    async def spam_A() -> None:
        for i in range(50):
            await hub.broadcast_to_session("A", {"sentence": f"A-{i}"})
            await asyncio.sleep(0)  # attach 와 인터리브 유도

    async def attach_B() -> None:
        await asyncio.sleep(0)  # spam 시작 후 끼어들기
        await attach(hub, ws_b, "B")

    await asyncio.gather(spam_A(), attach_B())
    await pump(30)

    leaked = [t for t in ws_b.sent_text if t.startswith("A-")]
    check("REV-1: attach 경쟁下 B 가 A 텍스트 안 받음(교차전송 0)",
          leaked == [], f"leaked={leaked}")
    await hub.detach("A")
    await hub.detach("B")
    await pump()


async def test_REV3_initial_half_open_closes() -> None:
    """REV-3: 첫 pong 을 한 번도 못 받는 초기 half-open 은 결국 끊겨야 한다.
    (에러C 수정으로 데드락은 풀렸지만, 그 부작용으로 ping 만 무한 전송하던 사각.)"""
    orig_sleep = asyncio.sleep

    async def fast_sleep(d):
        await orig_sleep(0.01 if d >= 5 else d)

    asyncio.sleep = fast_sleep  # type: ignore
    try:
        hub = WebSocketHub()
        ws = FakeWS()
        await attach(hub, ws, "A")
        conn = hub._conns[ws]
        # 첫 ping 을 아주 오래전에 보낸 것처럼 위조(첫 pong 은 영영 안 줌).
        # time.time() - 1.0 ≈ 1.7e9초 > 60 → 다음 keepalive 루프에서 끊겨야 함.
        conn.first_ping_sent_time = 1.0
        await orig_sleep(0.1)
        # P1: '재연결 대기(_reconnect_waiting)' 상태는 없어졌다 — 죽은 소켓은
        # 명부에서 내려가고 재접속은 그냥 새 소켓으로 등재된다.
        check("REV-3: 첫 pong 없는 초기 half-open 끊김",
              ws.closed and listeners(hub, "A") == [],
              f"closed={ws.closed} listeners={len(listeners(hub, 'A'))}")
        await hub.detach("A")
        await pump()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore


async def test_REV4_close_during_send() -> None:
    """REV-4(에러A): send 가 ASGI 로 흘러가는 '도중' close 가 끼어드는 실제 경쟁.
    _safe_close 가 소켓의 send gate 로 직렬화되면 overlap(=에러A)이 0 이어야 한다.
    (수정 전: _safe_close 게이트 밖 → send 진행 중 close → overlap 발생.)"""
    hub = WebSocketHub()
    ws = FakeWS()
    await attach(hub, ws, "A")
    await pump()
    ws.sent_text.clear()
    ws.overlap_errors = 0

    # _send_text 가 게이트 안에서 send_text(→sleep0) 에 진입하도록 예약 후 한 틱 양보,
    # 그와 동시에 detach(_safe_close) 로 close 를 경쟁시킨다.
    await hub.broadcast_to_session("A", {"sentence": "during"})
    await asyncio.sleep(0)  # _send_text 가 send_text await 지점에 진입
    await hub.detach("A")
    await pump(10)

    check("REV-4: close-during-send 경쟁에서 ASGI overlap 0(에러A)",
          ws.overlap_errors == 0,
          f"overlap_errors={ws.overlap_errors} sent={ws.sent_text}")


async def main() -> int:
    await test_error_A_toctou()
    await test_REV4_close_during_send()
    await test_detach_cross_session()
    await test_broadcast_stale_drop()
    await test_other_session_socket_never_receives()
    await test_P1_fanout_to_every_listener()
    await test_P1_slow_listener_does_not_block_others()
    await test_P1_one_drop_does_not_take_the_session_down()
    await test_REV1_no_cross_send_under_attach_race()
    await test_keepalive_sends_first_ping()
    await test_REV3_initial_half_open_closes()

    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name)
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
