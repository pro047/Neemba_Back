"""Listener event alerts (docs/prd/listener-event-alert.md, DESIGN §6).

Pure classification/formatting is tested directly. Hub wiring uses a
RecordingSink plus the FakeWS/FakeRecorder pattern from test_ws_blips.py.
The Discord sender is tested with httpx.MockTransport — no network.
"""
import asyncio
import contextlib
import json

import httpx
import pytest
from starlette.websockets import WebSocketState

from src.ws.listener_alert import (
    CONTENT_LIMIT,
    DiscordListenerAlerter,
    classify_drop,
    format_attach,
    format_drop,
    format_duration,
    format_reconnect,
    format_session_end,
    short_id,
    webhook_url_from_env,
)
from src.ws.websocket import WebSocketHub

WEBHOOK = "https://discord.test/api/webhooks/1/secret-token-value"
DROP_PREFIXES = ("🔴", "🟠", "⚪")


# --- fakes (test_ws_blips.py pattern, kept local for test independence) ----


class FakeWS:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTING
        self.sent: list[str] = []
        self.close_codes: list[int] = []

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        if (
            self.client_state != WebSocketState.CONNECTED
            or self.application_state != WebSocketState.CONNECTED
        ):
            raise RuntimeError("send after close")
        self.sent.append(text)

    async def send_json(self, data) -> None:
        await self.send_text(str(data))

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)
        self.application_state = WebSocketState.DISCONNECTED

    def client_disconnect(self) -> None:
        self.client_state = WebSocketState.DISCONNECTED


class DyingOnSendWS(FakeWS):
    """Socket that dies mid-send — drives the send_failed path."""

    async def send_text(self, text: str) -> None:
        self.client_state = WebSocketState.DISCONNECTED
        raise RuntimeError("connection reset")


class FakeRecorder:
    def __init__(self) -> None:
        self._next_id = 0
        self.reconnects: list[dict] = []
        self.abandons: list[dict] = []

    async def record_disconnect(self, session_id, *, client_id=None,
                                close_code=None, close_reason=None,
                                detected_by):
        self._next_id += 1
        return self._next_id

    async def record_reconnect(self, blip_id, *, flushed_count, lost_count):
        self.reconnects.append({"blip_id": blip_id, "lost_count": lost_count})

    async def record_abandon(self, blip_id, *, lost_count):
        self.abandons.append({"blip_id": blip_id, "lost_count": lost_count})


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def notify(self, text: str) -> None:
        self.texts.append(text)


class RaisingSink:
    def __init__(self) -> None:
        self.calls = 0

    def notify(self, text: str) -> None:
        self.calls += 1
        raise RuntimeError("sink exploded")


async def _drain(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _attach(hub: WebSocketHub, ws, session_id: str, **kwargs):
    await hub.register_session(session_id, kwargs.get("target_lang"))
    return await hub.attach(ws, session_id, **kwargs)


async def _teardown(hub: WebSocketHub) -> None:
    for conn in list(hub._conns.values()):
        task = conn.keepalive_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _drops(texts: list[str]) -> list[str]:
    return [t for t in texts if t.startswith(DROP_PREFIXES)]


# --- classify_drop ----------------------------------------------------------


# AC-1
def test_ac1_network_and_keepalive_drops_classify_as_blip():
    # Arrange / Act
    results = [
        classify_drop(1006, "client_disconnect"),
        classify_drop(1001, "client_disconnect"),
        classify_drop(None, "keepalive_timeout"),
    ]
    # Assert
    assert results == ["blip", "blip", "blip"]


# AC-1 (boundary): keepalive wins regardless of close code
def test_ac1_keepalive_with_any_close_code_is_blip():
    assert classify_drop(1000, "keepalive_timeout") == "blip"
    assert classify_drop(1005, "keepalive_timeout") == "blip"


# AC-2
def test_ac2_code_1005_from_client_classifies_as_app_closed():
    assert classify_drop(1005, "client_disconnect") == "app_closed"


# AC-3
def test_ac3_code_1000_from_client_classifies_as_app_closed_normal():
    assert classify_drop(1000, "client_disconnect") == "app_closed_normal"


# AC-4
def test_ac4_send_failed_is_blip_and_unknowns_are_other():
    # Arrange / Act
    send_failed = classify_drop(None, "send_failed")
    others = [
        classify_drop(None, "ws_error"),
        classify_drop(1011, "client_disconnect"),
        classify_drop(None, "client_disconnect"),
    ]
    # Assert
    assert send_failed == "blip"
    assert others == ["other", "other", "other"]


# AC-4 (boundary): ws_error with a normal-looking code is still "other"
def test_ac4_ws_error_with_close_code_is_still_other():
    assert classify_drop(1006, "ws_error") == "other"
    assert classify_drop(4410, "client_disconnect") == "other"


# --- short_id / format_duration --------------------------------------------


def test_short_id_truncates_to_8_and_keeps_short_values():
    assert short_id("0123456789ab") == "01234567"
    assert short_id("abc") == "abc"
    assert short_id("") == ""


# AC-7 (format_duration part)
def test_ac7_format_duration_floors_and_uses_minutes_only_when_needed():
    assert format_duration(9.9) == "9초"
    assert format_duration(0.4) == "0초"
    assert format_duration(75.4) == "1분 15초"
    assert format_duration(60.0) == "1분 0초"


# --- format_drop -----------------------------------------------------------


# AC-5
def test_ac5_blip_drop_text_has_short_ids_code_and_remaining():
    # Act
    text = format_drop(
        "blip", session_id="0123456789ab", client_id="fedcba9876543210",
        close_code=1006, detected_by="client_disconnect", listeners=2,
    )
    # Assert
    assert text.startswith("🔴 순단")
    for part in ("01234567", "fedcba98", "code=1006", "남은 2명"):
        assert part in text
    assert "0123456789ab" not in text
    assert "fedcba9876543210" not in text


# AC-5
def test_ac5_blip_drop_cause_for_keepalive_and_send_failed():
    # Act
    keepalive = format_drop(
        "blip", session_id="s1", client_id="c1", close_code=None,
        detected_by="keepalive_timeout", listeners=0,
    )
    send_failed = format_drop(
        "blip", session_id="s1", client_id="c1", close_code=None,
        detected_by="send_failed", listeners=0,
    )
    # Assert
    assert keepalive.startswith("🔴 순단")
    assert "keepalive 무응답" in keepalive
    assert "code=" not in keepalive
    assert send_failed.startswith("🔴 순단")
    assert "전송 실패" in send_failed
    assert "code=" not in send_failed


# AC-6
def test_ac6_app_closed_normal_and_other_texts():
    # Act
    app_closed = format_drop(
        "app_closed", session_id="0123456789ab", client_id="fedcba9876543210",
        close_code=1005, detected_by="client_disconnect", listeners=1,
    )
    normal = format_drop(
        "app_closed_normal", session_id="0123456789ab",
        client_id="fedcba9876543210", close_code=1000,
        detected_by="client_disconnect", listeners=1,
    )
    other = format_drop(
        "other", session_id="0123456789ab", client_id="fedcba9876543210",
        close_code=None, detected_by="ws_error", listeners=1,
    )
    # Assert
    assert app_closed.startswith("🟠 앱이 닫음")
    assert "구분 불가" in app_closed
    assert "code=1005" in app_closed
    assert "남은 1명" in app_closed
    assert normal.startswith("🟠 앱이 닫음 (code=1000)")
    assert "남은 1명" in normal
    assert other.startswith("⚪ 기타 끊김")
    assert "code=없음/ws_error" in other
    assert "남은 1명" in other


# AC-6 (boundary): unknown code is shown as-is in "other"
def test_ac6_other_with_unknown_code_shows_code_and_detector():
    text = format_drop(
        "other", session_id="s1", client_id="c1", close_code=1011,
        detected_by="client_disconnect", listeners=0,
    )
    assert "code=1011/client_disconnect" in text
    assert "남은 0명" in text


# --- format_reconnect / format_attach / format_session_end ----------------


# AC-7
def test_ac7_reconnect_text_has_duration_lost_dropped_socket_and_count():
    # Act
    text = format_reconnect(
        session_id="0123456789ab", client_id="fedcba9876543210",
        dropped_client_id="aaaaaaaabbbb", duration_s=75.4, lost_count=3,
        listeners=1,
    )
    # Assert
    assert text.startswith("🟢 복귀")
    for part in ("1분 15초", "유실 3건", "aaaaaaaa", "현재 1명",
                 "01234567", "fedcba98"):
        assert part in text
    assert "aaaaaaaabbbb" not in text


# AC-8
def test_ac8_attach_text_starts_with_marker_and_has_count():
    text = format_attach(session_id="s1", client_id="c1", listeners=1)
    assert text.startswith("🟢 청취자 접속")
    assert "현재 1명" in text


# AC-26
def test_ac26_session_end_text_has_short_session_and_listener_count():
    # Act
    text = format_session_end(session_id="0123456789ab", listeners=3)
    zero = format_session_end(session_id="0123456789ab", listeners=0)
    # Assert
    assert text.startswith("🔚 세션 종료")
    assert "01234567" in text
    assert "청취자 3명 연결 종료" in text
    assert "0123456789ab" not in text
    assert "청취자 0명 연결 종료" in zero


# --- hub wiring -------------------------------------------------------------


# AC-9
async def test_ac9_attach_without_open_blip_sends_one_attach_alert():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = FakeWS()
    # Act
    cid = await _attach(hub, ws, "0123456789ab")
    await _drain()
    # Assert
    assert len(sink.texts) == 1
    assert sink.texts[0].startswith("🟢 청취자 접속")
    assert "현재 1명" in sink.texts[0]
    assert cid[:8] in sink.texts[0]
    assert cid not in sink.texts[0]
    assert "01234567" in sink.texts[0]
    await _teardown(hub)


# AC-9 (boundary): count is per session, not global
async def test_ac9_attach_count_is_per_session_listener_count():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    # Act
    await _attach(hub, FakeWS(), "s1")
    await _attach(hub, FakeWS(), "s1")
    await _attach(hub, FakeWS(), "s2")
    await _drain()
    # Assert
    assert [t.startswith("🟢 청취자 접속") for t in sink.texts] == [True] * 3
    assert "현재 1명" in sink.texts[0]
    assert "현재 2명" in sink.texts[1]
    assert "현재 1명" in sink.texts[2]
    await _teardown(hub)


# AC-9 (boundary): idempotent duplicate attach sends nothing extra
async def test_ac9_duplicate_attach_of_same_socket_sends_no_alert():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    # Act
    await hub.attach(ws, "s1")
    await _drain()
    # Assert
    assert len(sink.texts) == 1
    await _teardown(hub)


# AC-10
async def test_ac10_drop_then_reattach_sends_blip_then_reconnect_with_lost():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(blip_recorder=FakeRecorder(), alert_sink=sink)
    ws = FakeWS()
    first_cid = await _attach(hub, ws, "s1")
    await _drain()
    # Act
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await hub.broadcast_to_session("s1", {"sentence": "m2"})
    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()
    # Assert
    assert len(sink.texts) == 3
    assert sink.texts[0].startswith("🟢 청취자 접속")
    assert sink.texts[1].startswith("🔴 순단")
    assert "code=1006" in sink.texts[1]
    assert "남은 0명" in sink.texts[1]
    assert sink.texts[2].startswith("🟢 복귀")
    assert "유실 2건" in sink.texts[2]
    assert "현재 1명" in sink.texts[2]
    assert first_cid[:8] in sink.texts[2]
    await _teardown(hub)


# AC-10 (boundary): remaining count reflects the other listener still attached
async def test_ac10_drop_with_other_listener_reports_remaining_one():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()
    # Act
    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1, close_code=1006)
    await _drain()
    # Assert
    drops = _drops(sink.texts)
    assert len(drops) == 1
    assert "남은 1명" in drops[0]
    await _teardown(hub)


# AC-11
async def test_ac11_duplicate_disconnect_notice_sends_one_drop_alert():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    # Act
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await _drain()
    # Assert
    assert len(_drops(sink.texts)) == 1
    await _teardown(hub)


# AC-11 (boundary): disconnect notice then keepalive drop of same conn -> one alert
async def test_ac11_disconnect_then_keepalive_drop_sends_one_drop_alert():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    conn = hub._conns[ws]
    # Act
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1006)
    await hub._drop_conn(conn, detected_by="keepalive_timeout")
    await _drain()
    # Assert
    drops = _drops(sink.texts)
    assert len(drops) == 1
    assert "code=1006" in drops[0]
    await _teardown(hub)


# AC-12
async def test_ac12_keepalive_drop_sends_blip_with_keepalive_cause():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    # Act
    await hub._drop_conn(hub._conns[ws], detected_by="keepalive_timeout")
    await _drain()
    # Assert
    drops = _drops(sink.texts)
    assert len(drops) == 1
    assert drops[0].startswith("🔴 순단")
    assert "keepalive 무응답" in drops[0]
    await _teardown(hub)


# AC-12 (boundary): send failure on a dying socket alerts as blip "전송 실패"
async def test_ac12_send_failure_on_dead_socket_sends_blip_send_failed():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = DyingOnSendWS()
    await _attach(hub, ws, "s1")
    await _drain()
    # Act
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    # Assert
    drops = _drops(sink.texts)
    assert len(drops) == 1
    assert drops[0].startswith("🔴 순단")
    assert "전송 실패" in drops[0]
    await _teardown(hub)


# AC-13
async def test_ac13_code_1005_disconnect_sends_app_closed_alert():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = FakeWS()
    await _attach(hub, ws, "s1")
    await _drain()
    # Act
    ws.client_disconnect()
    await hub.handle_client_disconnect("s1", ws, close_code=1005)
    await _drain()
    # Assert
    drops = _drops(sink.texts)
    assert len(drops) == 1
    assert drops[0].startswith("🟠 앱이 닫음")
    assert "code=1005" in drops[0]
    await _teardown(hub)


# AC-13 (boundary): 1000 and ws_error through the hub
async def test_ac13_code_1000_and_ws_error_through_hub():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")
    await _drain()
    # Act
    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1, close_code=1000)
    await hub.handle_client_disconnect("s1", ws2, detected_by="ws_error")
    await _drain()
    # Assert
    drops = _drops(sink.texts)
    assert len(drops) == 2
    assert drops[0].startswith("🟠 앱이 닫음 (code=1000)")
    assert drops[1].startswith("⚪ 기타 끊김")
    assert "code=없음/ws_error" in drops[1]
    await _teardown(hub)


# AC-14
async def test_ac14_detach_sends_one_session_end_and_no_drop_alerts():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(blip_recorder=FakeRecorder(), alert_sink=sink)
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "0123456789ab")
    await _attach(hub, ws2, "0123456789ab")
    await _drain()
    before = len(sink.texts)
    # Act
    await hub.detach("0123456789ab")
    await _drain()
    after_detach = sink.texts[before:]
    for ws in (ws1, ws2):
        ws.client_disconnect()
        await hub.handle_client_disconnect("0123456789ab", ws, close_code=4410)
    await _drain()
    # Assert
    assert len(after_detach) == 1
    assert after_detach[0].startswith("🔚 세션 종료")
    assert "청취자 2명 연결 종료" in after_detach[0]
    assert "01234567" in after_detach[0]
    assert _drops(sink.texts) == []
    assert len(sink.texts) == before + 1
    assert ws1.close_codes == [4410]
    assert ws2.close_codes == [4410]
    await _teardown(hub)


# AC-14 (boundary): live session with zero listeners still reports 0명
async def test_ac14_detach_of_live_session_without_listeners_reports_zero():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    await hub.register_session("s1")
    # Act
    await hub.detach("s1")
    await _drain()
    # Assert
    assert len(sink.texts) == 1
    assert sink.texts[0].startswith("🔚 세션 종료")
    assert "청취자 0명 연결 종료" in sink.texts[0]


# AC-14 (boundary): detach counts only its own session's sockets
async def test_ac14_detach_counts_only_its_session_listeners():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    await _attach(hub, FakeWS(), "s1")
    await _attach(hub, FakeWS(), "s2")
    await _attach(hub, FakeWS(), "s2")
    await _drain()
    before = len(sink.texts)
    # Act
    await hub.detach("s1")
    await _drain()
    # Assert
    assert len(sink.texts) == before + 1
    assert "청취자 1명 연결 종료" in sink.texts[-1]
    await _teardown(hub)


# AC-27
async def test_ac27_detach_of_non_live_session_sends_nothing():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    # Act
    await hub.detach("never-registered")
    await _drain()
    # Assert
    assert sink.texts == []


# AC-27
async def test_ac27_double_detach_sends_one_session_end():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    await _attach(hub, FakeWS(), "s1")
    await _drain()
    # Act
    await hub.detach("s1")
    await hub.detach("s1")
    await _drain()
    # Assert
    ends = [t for t in sink.texts if t.startswith("🔚 세션 종료")]
    assert len(ends) == 1
    await _teardown(hub)


# AC-15
async def test_ac15_rejected_attach_to_non_live_session_sends_nothing():
    # Arrange
    sink = RecordingSink()
    hub = WebSocketHub(alert_sink=sink)
    ws = FakeWS()
    # Act
    result = await hub.attach(ws, "not-live")
    await _drain()
    # Assert
    assert result is None
    assert ws.close_codes == [4404]
    assert sink.texts == []


# AC-16
async def test_ac16_raising_sink_does_not_break_hub_flow():
    # Arrange
    sink = RaisingSink()
    hub = WebSocketHub(blip_recorder=FakeRecorder(), alert_sink=sink)
    ws1 = FakeWS()
    # Act
    await _attach(hub, ws1, "s1")
    await _drain()
    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1, close_code=1006)
    await _drain()
    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()
    count_after_reattach = hub.listener_count()
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    await hub.detach("s1")
    await _drain()
    # Assert
    assert sink.calls >= 4
    assert count_after_reattach == 1
    assert ws2.sent == ["m1"]
    assert ws1.close_codes == [1000]
    assert ws2.close_codes == [4410]
    assert hub.listener_count() == 0
    await _teardown(hub)


# AC-17
async def test_ac17_hanging_webhook_post_does_not_block_hub():
    # Arrange
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(hang))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    hub = WebSocketHub(alert_sink=alerter)
    worker = asyncio.create_task(alerter.run())
    try:
        ws = FakeWS()
        await hub.register_session("s1")
        # Act / Assert
        await asyncio.wait_for(hub.attach(ws, "s1"), 1.0)
        await asyncio.wait_for(started.wait(), 1.0)
        ws.client_disconnect()
        await asyncio.wait_for(
            hub.handle_client_disconnect("s1", ws, close_code=1006), 1.0)
        await asyncio.wait_for(hub.attach(FakeWS(), "s1"), 1.0)
        await asyncio.wait_for(hub.detach("s1"), 1.0)
        assert not worker.done()
        assert hub.listener_count() == 0
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await client.aclose()
        await _teardown(hub)


# --- DiscordListenerAlerter -------------------------------------------------


class _Capture:
    """MockTransport handler that records POST bodies and replays responses."""

    def __init__(self, responses=None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(204)

    @property
    def contents(self) -> list[str]:
        return [json.loads(r.content)["content"] for r in self.requests]


async def _wait_until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# AC-18
async def test_ac18_log_only_mode_prints_and_never_posts(capsys):
    # Arrange
    cap = _Capture()
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(None, client=client)
    # Act
    alerter.notify("x-log-only-line")
    await asyncio.wait_for(alerter.run(), 1.0)
    await _drain()
    # Assert
    out = capsys.readouterr().out
    assert "x-log-only-line" in out
    assert cap.requests == []
    await client.aclose()


# AC-18 (boundary): webhook mode also prints every notify
async def test_ac18_webhook_mode_also_prints_notify_text(capsys):
    cap = _Capture()
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    alerter.notify("printed-line")
    assert "printed-line" in capsys.readouterr().out
    await client.aclose()


# AC-19
async def test_ac19_queued_notifies_are_merged_into_one_post():
    # Arrange
    cap = _Capture()
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    alerter.notify("a")
    alerter.notify("b")
    alerter.notify("c")
    # Act
    worker = asyncio.create_task(alerter.run())
    try:
        await _wait_until(lambda: len(cap.requests) >= 1)
        await asyncio.sleep(0.05)
        # Assert
        assert len(cap.requests) == 1
        assert cap.requests[0].method == "POST"
        assert str(cap.requests[0].url) == WEBHOOK
        assert json.loads(cap.requests[0].content) == {
            "content": "[neemba] a\nb\nc"}
    finally:
        await _stop(worker)
        await client.aclose()


# AC-20
async def test_ac20_429_retries_same_content_once_and_worker_continues():
    # Arrange
    cap = _Capture([httpx.Response(429, json={"retry_after": 0.01})])
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    alerter.notify("a")
    worker = asyncio.create_task(alerter.run())
    try:
        # Act
        await _wait_until(lambda: len(cap.requests) >= 2)
        alerter.notify("b")
        await _wait_until(lambda: len(cap.requests) >= 3)
        await asyncio.sleep(0.05)
        # Assert
        assert cap.contents == ["[neemba] a", "[neemba] a", "[neemba] b"]
    finally:
        await _stop(worker)
        await client.aclose()


# AC-20 (boundary): two 429s in a row drop the chunk and move on
async def test_ac20_second_429_drops_chunk_and_worker_continues():
    # Arrange
    cap = _Capture([
        httpx.Response(429, json={"retry_after": 0.01}),
        httpx.Response(429, json={"retry_after": 0.01}),
    ])
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    alerter.notify("a")
    worker = asyncio.create_task(alerter.run())
    try:
        # Act
        await _wait_until(lambda: len(cap.requests) >= 2)
        await asyncio.sleep(0.05)
        alerter.notify("b")
        await _wait_until(lambda: len(cap.requests) >= 3)
        await asyncio.sleep(0.05)
        # Assert
        assert cap.contents == ["[neemba] a", "[neemba] a", "[neemba] b"]
        assert not worker.done()
    finally:
        await _stop(worker)
        await client.aclose()


# AC-21
async def test_ac21_connect_error_does_not_kill_worker():
    # Arrange
    cap = _Capture([httpx.ConnectError("boom")])
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    alerter.notify("a")
    worker = asyncio.create_task(alerter.run())
    try:
        # Act
        await _wait_until(lambda: len(cap.requests) >= 1)
        await asyncio.sleep(0.05)
        alive = not worker.done()
        alerter.notify("b")
        await _wait_until(lambda: len(cap.requests) >= 2)
        # Assert
        assert alive
        assert cap.contents[-1] == "[neemba] b"
        assert not worker.done()
    finally:
        await _stop(worker)
        await client.aclose()


# AC-21 (boundary): non-2xx (500) is logged and the worker continues
async def test_ac21_server_error_response_does_not_kill_worker():
    cap = _Capture([httpx.Response(500)])
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    alerter.notify("a")
    worker = asyncio.create_task(alerter.run())
    try:
        await _wait_until(lambda: len(cap.requests) >= 1)
        await asyncio.sleep(0.05)
        alerter.notify("b")
        await _wait_until(lambda: len(cap.requests) >= 2)
        assert cap.contents == ["[neemba] a", "[neemba] b"]
        assert not worker.done()
    finally:
        await _stop(worker)
        await client.aclose()


# AC-22
def test_ac22_full_queue_drops_extra_notifies_without_raising():
    # Arrange
    alerter = DiscordListenerAlerter(WEBHOOK, queue_max=2)
    # Act
    for i in range(5):
        alerter.notify(f"m{i}")
    # Assert
    assert alerter.dropped == 3


# AC-22 (boundary): log-only mode never queues, so nothing is dropped
def test_ac22_log_only_mode_never_counts_drops():
    alerter = DiscordListenerAlerter(None, queue_max=2)
    for i in range(5):
        alerter.notify(f"m{i}")
    assert alerter.dropped == 0


# AC-23
async def test_ac23_oversized_batch_is_split_into_chunks_within_limit():
    # Arrange
    cap = _Capture()
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    lines = [f"{i:03d}" + "y" * 97 for i in range(30)]  # 30 x 100 chars
    for line in lines:
        alerter.notify(line)
    worker = asyncio.create_task(alerter.run())
    try:
        # Act
        await _wait_until(
            lambda: all(any(l in c for c in cap.contents) for l in lines))
        await asyncio.sleep(0.05)
        # Assert
        assert len(cap.requests) >= 2
        assert all(len(c) <= CONTENT_LIMIT for c in cap.contents)
    finally:
        await _stop(worker)
        await client.aclose()


# AC-23 (boundary): a single overlong line is cut to fit, worker survives
async def test_ac23_single_overlong_line_is_cut_within_limit():
    cap = _Capture()
    client = httpx.AsyncClient(transport=httpx.MockTransport(cap))
    alerter = DiscordListenerAlerter(WEBHOOK, client=client)
    alerter.notify("z" * 5000)
    worker = asyncio.create_task(alerter.run())
    try:
        await _wait_until(lambda: len(cap.requests) >= 1)
        await asyncio.sleep(0.05)
        assert all(len(c) <= CONTENT_LIMIT for c in cap.contents)
        assert not worker.done()
    finally:
        await _stop(worker)
        await client.aclose()


# AC-24
def test_ac24_webhook_url_from_env_handles_blank_and_hides_value(
        monkeypatch, capsys):
    # Arrange / Act
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    unset = webhook_url_from_env()
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    empty = webhook_url_from_env()
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "  ")
    blank = webhook_url_from_env()
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    value = webhook_url_from_env()
    # Assert
    assert unset is None
    assert empty is None
    assert blank is None
    assert value == WEBHOOK
    captured = capsys.readouterr()
    assert "secret-token-value" not in captured.out
    assert "secret-token-value" not in captured.err


# AC-25
async def test_ac25_made_client_sends_neemba_hub_user_agent():
    # Arrange
    cap = _Capture()
    alerter = DiscordListenerAlerter(WEBHOOK)
    client = alerter._make_client(transport=httpx.MockTransport(cap))
    # Act
    await client.post(WEBHOOK, json={"content": "x"})
    await client.aclose()
    # Assert
    assert cap.requests[0].headers["User-Agent"] == "neemba-hub/1.0"
    assert client.timeout.read == 5.0
