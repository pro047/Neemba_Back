"""세션 종료·거부 시 close code 계약 (클라이언트 재연결 예산의 근거).

클라이언트는 close code 로 '재시도해야 하는 끊김' 과 '멈춰야 하는 만료' 를
가른다. 기본값 1000 은 둘을 구분하지 못해 반드시 한 번 붙어봐야 했다.
"""
import asyncio

import pytest
from starlette.websockets import WebSocketState

from src.ws.websocket import (
    CLOSE_SESSION_ENDED,
    CLOSE_SESSION_NOT_FOUND,
    WebSocketHub,
)


class RecordingWS:
    """close code 를 기록하는 최소 소켓 스텁."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTING
        self.close_codes: list[int] = []

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        pass

    async def send_json(self, data) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)
        self.application_state = WebSocketState.DISCONNECTED


async def _attached_hub(session_id: str = "s1"):
    hub = WebSocketHub()
    await hub.register_session(session_id, "en-US")
    ws = RecordingWS()
    client_id = await hub.attach(ws, session_id)
    assert client_id is not None
    return hub, ws


@pytest.mark.asyncio
async def test_세션이_종료되면_소켓을_만료_코드로_닫아야_한다():
    hub, ws = await _attached_hub()

    await hub.detach("s1")

    assert ws.close_codes == [CLOSE_SESSION_ENDED]


@pytest.mark.asyncio
async def test_소켓_하나만_떨어질_때는_만료_코드를_쓰지_않아야_한다():
    # 대조군. keepalive 로 소켓 하나가 떨어져도 세션은 살아 있으므로 그
    # 청취자는 재접속을 **해야 한다** — 만료 코드를 주면 정반대로 멈춘다.
    hub, ws = await _attached_hub()
    conn = hub._conns[ws]

    await hub._drop_conn(conn, detected_by="keepalive_timeout")

    assert ws.close_codes != [CLOSE_SESSION_ENDED]
    assert ws.close_codes == [1000]


@pytest.mark.asyncio
async def test_소켓이_떨어져도_세션은_살아_있어야_한다():
    # 위 대조군의 전제 확인 — 세션이 죽는다면 만료 코드가 맞는 게 된다.
    hub, ws = await _attached_hub()
    conn = hub._conns[ws]

    await hub._drop_conn(conn, detected_by="keepalive_timeout")

    second = RecordingWS()
    assert await hub.attach(second, "s1") is not None
    assert second.close_codes == []


@pytest.mark.asyncio
async def test_종료된_세션에_다시_붙으면_세션없음_코드로_거부해야_한다():
    hub, _ = await _attached_hub()
    await hub.detach("s1")

    late = RecordingWS()

    assert await hub.attach(late, "s1") is None
    assert late.close_codes == [CLOSE_SESSION_NOT_FOUND]


@pytest.mark.asyncio
async def test_두_코드는_서로_달라야_한다():
    # 같은 값이면 로그에서 '처음부터 없던 세션' 과 '방금 끝난 세션' 이 섞인다.
    assert CLOSE_SESSION_ENDED != CLOSE_SESSION_NOT_FOUND


@pytest.mark.asyncio
async def test_여러_청취자가_붙어도_전원이_만료_코드를_받아야_한다():
    hub, first = await _attached_hub()
    second = RecordingWS()
    assert await hub.attach(second, "s1") is not None

    await hub.detach("s1")
    await asyncio.sleep(0)

    assert first.close_codes == [CLOSE_SESSION_ENDED]
    assert second.close_codes == [CLOSE_SESSION_ENDED]
