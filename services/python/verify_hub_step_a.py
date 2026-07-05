"""
Step A 허브 단위 검증 스크립트 (docker/NATS/DeepL 불필요).
가짜 WebSocket 으로 attach→broadcast→detach 경쟁을 유발해 에러 A·B·C 와
세션 게이팅(교차 종료/교차 전송 차단)을 확인한다.

실행: python3 verify_hub_step_a.py
의존성: fastapi, starlette 만 필요 (허브가 deepl/nats 를 import 하지 않으므로).
"""
import asyncio

from starlette.websockets import WebSocketState
from src.ws.websocket import WebSocketHub


class FakeWS:
    """허브가 호출하는 최소 인터페이스만 구현한 가짜 소켓."""

    def __init__(self) -> None:
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
        if self.application_state != WebSocketState.CONNECTED:
            raise RuntimeError(
                "Unexpected ASGI message 'websocket.send', "
                "after sending 'websocket.close'")
        # REV-4: send 를 ASGI 채널로 흘리는 동안 yield → 이 틈에 close 가 끼어들면
        # 실제 starlette 처럼 'close 이후 send' 가 된다. _send_gate 직렬화가 없으면 재현.
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


results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))


async def pump(n: int = 5) -> None:
    """스케줄된 fire-and-forget task 들이 돌도록 이벤트 루프를 양보."""
    for _ in range(n):
        await asyncio.sleep(0)


async def test_error_A_toctou() -> None:
    """에러A: broadcast 가 _send_text task 를 예약한 직후 우리 쪽이 close.
    게이트 안 재확인이 닫힌 소켓으로의 send 를 막아야 한다."""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "A")
    await pump()
    ws.sent_text.clear()  # attach 시점의 flush 등 정리

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
    await hub.attach(ws, "A")
    await pump()

    await hub.detach("B")  # 엉뚱한 세션 stop → 무시되어야 함
    check("분리: detach(B) 가 A 소켓 안 끊음",
          hub.client is ws and hub._session_id == "A" and not ws.closed)

    await hub.detach("A")  # 올바른 세션 stop → 정리
    check("분리: detach(A) 가 슬롯 비움",
          hub.client is None and hub._session_id is None and ws.closed)


async def test_broadcast_stale_drop() -> None:
    """에러B: 슬롯 주인 아닌 세션 번역은 큐잉 없이 drop."""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "A")
    await pump()
    ws.sent_text.clear()

    await hub.broadcast_to_session("OLD", {"sentence": "stale"})
    await pump()
    check("B: stale 세션 번역 drop (전송X·큐잉X)",
          ws.sent_text == [] and len(hub._pending) == 0,
          f"sent={ws.sent_text} pending={len(hub._pending)}")
    await hub.detach("A")


async def test_attach_clears_pending_on_session_change() -> None:
    """에러B: 세션 교체 시 이전 세션의 미전송 pending 이 새 클라로 안 샌다."""
    hub = WebSocketHub()
    ws1 = FakeWS()
    await hub.attach(ws1, "A")
    await pump()
    # ws1 끊긴 상태에서 같은 세션 번역 → pending 누적
    ws1.application_state = WebSocketState.DISCONNECTED
    await hub.broadcast_to_session("A", {"sentence": "for-A"})
    await pump()
    had_pending = len(hub._pending) == 1

    # 새 세션 B 로 attach → 이전 pending 비워져야 함
    ws2 = FakeWS()
    await hub.attach(ws2, "B")
    await pump()
    check("B: 세션 교체 시 이전 pending 비움",
          had_pending and len(hub._pending) == 0 and "for-A" not in ws2.sent_text,
          f"had_pending={had_pending} pending_after={len(hub._pending)} ws2={ws2.sent_text}")
    await hub.detach("B")


async def test_keepalive_sends_first_ping() -> None:
    """에러C: 첫 pong 을 받기 전에도 서버가 첫 ping 을 무조건 보내야 한다.
    (기존 버그: first_pong 전엔 ping 을 안 보내 데드락.)"""
    orig_sleep = asyncio.sleep

    async def fast_sleep(d):
        # keepalive 의 긴 sleep(30/5) 만 단축, 나머지는 그대로.
        await orig_sleep(0.01 if d >= 5 else d)

    asyncio.sleep = fast_sleep  # type: ignore
    try:
        hub = WebSocketHub()
        ws = FakeWS()
        await hub.attach(ws, "A")
        # 첫 pong 을 일부러 주지 않는다 (_first_pong_received=False 유지).
        await orig_sleep(0.2)  # keepalive 가 최소 1회 돌 시간
        first_ping = any(m.get("type") == "ping" for m in ws.sent_json)
        check("C: 첫 pong 전에도 첫 ping 발사 (데드락 해소)",
              first_ping and not hub._first_pong_received,
              f"sent_json={ws.sent_json} first_pong={hub._first_pong_received}")
        await hub.detach("A")
        await pump()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore


async def test_REV1_no_cross_send_under_attach_race() -> None:
    """REV-1 회귀: A 가 끊긴 채 A 번역이 다수 쏟아지는 동안 B 가 attach 한다.
    broadcast 의 주인검사~pending append 가 한 락으로 묶였으므로(원자적),
    이전 세션 A 의 텍스트가 새 클라 B 로 새면 안 된다(교차전송 차단)."""
    hub = WebSocketHub()
    ws_a = FakeWS()
    await hub.attach(ws_a, "A")
    await pump()
    # A 끊김 → 이후 broadcast(A) 는 pending 행이 된다.
    ws_a.application_state = WebSocketState.DISCONNECTED

    ws_b = FakeWS()

    async def spam_A() -> None:
        for i in range(50):
            await hub.broadcast_to_session("A", {"sentence": f"A-{i}"})
            await asyncio.sleep(0)  # attach 와 인터리브 유도

    async def attach_B() -> None:
        await asyncio.sleep(0)  # spam 시작 후 끼어들기
        await hub.attach(ws_b, "B")

    await asyncio.gather(spam_A(), attach_B())
    await pump(30)

    leaked = [t for t in ws_b.sent_text if t.startswith("A-")]
    check("REV-1: attach 경쟁下 B 가 A 텍스트 안 받음(교차전송 0)",
          leaked == [], f"leaked={leaked}")
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
        await hub.attach(ws, "A")
        # 첫 ping 을 아주 오래전에 보낸 것처럼 위조(첫 pong 은 영영 안 줌).
        # time.time() - 1.0 ≈ 1.7e9초 > 60 → 다음 keepalive 루프에서 끊겨야 함.
        hub._first_ping_sent_time = 1.0
        await orig_sleep(0.1)
        check("REV-3: 첫 pong 없는 초기 half-open 끊김",
              ws.closed and hub.client is None and hub._reconnect_waiting,
              f"closed={ws.closed} client={hub.client} waiting={hub._reconnect_waiting}")
        await hub.detach("A")
        await pump()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore


async def test_REV4_close_during_send() -> None:
    """REV-4(에러A): send 가 ASGI 로 흘러가는 '도중' close 가 끼어드는 실제 경쟁.
    _safe_close 가 _send_gate 로 직렬화되면 overlap(=에러A)이 0 이어야 한다.
    (수정 전: _safe_close 게이트 밖 → send 진행 중 close → overlap 발생.)"""
    hub = WebSocketHub()
    ws = FakeWS()
    await hub.attach(ws, "A")
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
    await test_attach_clears_pending_on_session_change()
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
