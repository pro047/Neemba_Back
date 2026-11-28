"""Listener reconnect pairing by exact device id only
(docs/prd/listener-alert-exact-match.md, .pipeline DESIGN §5).

- A reconnect closes an open blip only when its client_id equals the dropped
  socket's client_id. No oldest-open fallback.
- When the same client_id attaches again while its old socket is still listed
  (loss not yet detected), the hub retires the old socket: opens a
  detected_by="replaced" blip and closes it immediately with the new socket.

FakeWS / FakeRecorder / RecordingSink follow test_listener_alert.py:36-106,
kept local for test independence.
"""
import asyncio
import contextlib
from collections import deque

from prometheus_client import REGISTRY
from starlette.websockets import WebSocketState

from src.ws.websocket import WebSocketHub, _OpenBlip

DROP_PREFIXES = ("🔴", "🟠", "⚪")
DEV_X = "devxxxxx-0001-aaaa"
DEV_Y = "devyyyyy-0002-bbbb"
DEV_Z = "devzzzzz-0003-cccc"


# --- fakes -------------------------------------------------------------------


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


class FakeRecorder:
    """WsBlipRecorder stand-in that records every call's arguments."""

    def __init__(self) -> None:
        self._next_id = 0
        self.disconnects: list[dict] = []
        self.reconnects: list[dict] = []
        self.abandons: list[dict] = []

    async def record_disconnect(self, session_id, *, client_id=None,
                                close_code=None, close_reason=None,
                                detected_by):
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
        self.reconnects.append({"blip_id": blip_id, "lost_count": lost_count})

    async def record_abandon(self, blip_id, *, lost_count):
        self.abandons.append({"blip_id": blip_id, "lost_count": lost_count})


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def notify(self, text: str) -> None:
        self.texts.append(text)


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


async def _drop_1006(hub: WebSocketHub, ws, session_id: str) -> None:
    ws.client_disconnect()
    await hub.handle_client_disconnect(session_id, ws, close_code=1006)
    await _drain()


def _new_hub():
    rec, sink = FakeRecorder(), RecordingSink()
    return WebSocketHub(blip_recorder=rec, alert_sink=sink), rec, sink


# --- AC-1 --------------------------------------------------------------------


# AC-1
async def test_ac1_same_client_id_reconnect_after_1006_sends_reconnect_alert():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1", client_id=DEV_X)
    await _drain()
    await _drop_1006(hub, ws1, "s1")
    # Act
    ws2 = FakeWS()
    cid = await _attach(hub, ws2, "s1", client_id=DEV_X)
    await _drain()
    # Assert
    assert cid == DEV_X
    assert len(sink.texts) == 3
    assert sink.texts[2].startswith("🟢 복귀")
    assert "현재 1명" in sink.texts[2]
    assert f"기기 {DEV_X[:8]}" in sink.texts[2]
    assert f"(끊긴 소켓 {DEV_X[:8]})" in sink.texts[2]
    assert rec.reconnects == [{"blip_id": 1, "lost_count": 0}]
    await _teardown(hub)


# --- AC-2 --------------------------------------------------------------------


# AC-2
async def test_ac2_other_client_id_does_not_take_dropped_devices_blip():
    # Arrange
    hub, rec, sink = _new_hub()
    ws_x = FakeWS()
    await _attach(hub, ws_x, "s1", client_id=DEV_X)
    await _drain()
    await _drop_1006(hub, ws_x, "s1")
    # Act
    ws_y = FakeWS()
    await _attach(hub, ws_y, "s1", client_id=DEV_Y)
    await _drain()
    # Assert
    assert sink.texts[-1].startswith("🟢 청취자 접속")
    assert rec.reconnects == []
    await _teardown(hub)


# AC-2 (continued): X's blip stayed open and X itself closes it later
async def test_ac2_dropped_device_still_closes_its_blip_after_other_attach():
    # Arrange
    hub, rec, sink = _new_hub()
    ws_x = FakeWS()
    await _attach(hub, ws_x, "s1", client_id=DEV_X)
    await _drain()
    await _drop_1006(hub, ws_x, "s1")
    ws_y = FakeWS()
    await _attach(hub, ws_y, "s1", client_id=DEV_Y)
    await _drain()
    # Act
    ws_x2 = FakeWS()
    await _attach(hub, ws_x2, "s1", client_id=DEV_X)
    await _drain()
    # Assert
    assert rec.reconnects == [{"blip_id": 1, "lost_count": 0}]
    assert sink.texts[-1].startswith("🟢 복귀")
    assert "현재 2명" in sink.texts[-1]
    # Y is a different device: it is neither replaced nor dropped
    assert ws_y in hub._conns
    assert ws_y.close_codes == []
    assert len(rec.disconnects) == 1
    await _teardown(hub)


# --- AC-3 --------------------------------------------------------------------


# AC-3
async def test_ac3_no_client_id_reconnect_is_attach_not_reconnect():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1")
    await _drain()
    await _drop_1006(hub, ws1, "s1")
    # Act
    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()
    # Assert
    assert sink.texts[-1].startswith("🟢 청취자 접속")
    assert rec.reconnects == []
    await _teardown(hub)


# AC-3 (continued): the unmatched blip is abandoned at session end
async def test_ac3_unmatched_blip_without_client_id_is_abandoned_on_detach():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1")
    await _drain()
    await _drop_1006(hub, ws1, "s1")
    ws2 = FakeWS()
    await _attach(hub, ws2, "s1")
    await _drain()
    # Act
    await hub.detach("s1")
    await _drain()
    # Assert
    assert rec.reconnects == []
    assert rec.abandons == [{"blip_id": 1, "lost_count": 0}]
    await _teardown(hub)


# --- AC-4 --------------------------------------------------------------------


# AC-4
async def test_ac4_server_issued_id_does_not_take_supplied_devices_blip():
    # Arrange
    hub, rec, sink = _new_hub()
    ws_x = FakeWS()
    await _attach(hub, ws_x, "s1", client_id=DEV_X)
    await _drain()
    await _drop_1006(hub, ws_x, "s1")
    # Act
    ws_anon = FakeWS()
    await _attach(hub, ws_anon, "s1")
    await _drain()
    # Assert
    assert sink.texts[-1].startswith("🟢 청취자 접속")
    assert rec.reconnects == []
    await _teardown(hub)


# AC-4 (boundary): empty clientId= is "not supplied" and takes nothing either
async def test_ac4_empty_client_id_does_not_take_supplied_devices_blip():
    # Arrange
    hub, rec, sink = _new_hub()
    ws_x = FakeWS()
    await _attach(hub, ws_x, "s1", client_id=DEV_X)
    await _drain()
    await _drop_1006(hub, ws_x, "s1")
    # Act
    ws_empty = FakeWS()
    await _attach(hub, ws_empty, "s1", client_id="")
    await _drain()
    # Assert
    assert sink.texts[-1].startswith("🟢 청취자 접속")
    assert rec.reconnects == []
    await _teardown(hub)


# --- AC-5 --------------------------------------------------------------------


# AC-5
async def test_ac5_same_client_id_attach_replaces_still_listed_socket():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1", client_id=DEV_X)
    await _drain()
    conn1 = hub._conns[ws1]
    # Act
    ws2 = FakeWS()
    cid = await _attach(hub, ws2, "s1", client_id=DEV_X)
    await _drain()
    # Assert
    assert cid == DEV_X
    assert rec.disconnects == [{
        "id": 1,
        "session_id": "s1",
        "client_id": DEV_X,
        "close_code": None,
        "close_reason": None,
        "detected_by": "replaced",
    }]
    assert rec.reconnects == [{"blip_id": 1, "lost_count": 0}]
    assert len(sink.texts) == 2
    assert sink.texts[0].startswith("🟢 청취자 접속")
    assert sink.texts[1].startswith("🟢 복귀")
    assert "현재 1명" in sink.texts[1]
    assert "끊긴 시간 0초" in sink.texts[1]
    assert _drops(sink.texts) == []
    assert ws1.close_codes == [1000]
    assert ws1 not in hub._conns
    assert ws2 in hub._conns
    assert hub.listener_count() == 1
    # The retired socket's keepalive loop is cancelled, not left pinging
    assert conn1.keepalive_task is not None and conn1.keepalive_task.done()
    await _teardown(hub)


# AC-5 (continued): the replaced blip is already closed, so detach abandons nothing
async def test_ac5_detach_after_replacement_abandons_nothing():
    # Arrange
    hub, rec, sink = _new_hub()
    await _attach(hub, FakeWS(), "s1", client_id=DEV_X)
    await _attach(hub, FakeWS(), "s1", client_id=DEV_X)
    await _drain()
    # Act
    await hub.detach("s1")
    await _drain()
    # Assert
    assert rec.abandons == []
    assert rec.reconnects == [{"blip_id": 1, "lost_count": 0}]
    assert "청취자 1명 연결 종료" in sink.texts[-1]
    await _teardown(hub)


# AC-5 (boundary): only the same device is retired; the reconnect count is
# taken after the replacement (ws_y + new ws_x = 2, not 3)
async def test_ac5_replacement_keeps_other_devices_and_counts_after_retire():
    # Arrange
    hub, rec, sink = _new_hub()
    ws_x1, ws_y = FakeWS(), FakeWS()
    await _attach(hub, ws_x1, "s1", client_id=DEV_X)
    await _attach(hub, ws_y, "s1", client_id=DEV_Y)
    await _drain()
    # Act
    ws_x2 = FakeWS()
    await _attach(hub, ws_x2, "s1", client_id=DEV_X)
    await _drain()
    # Assert
    assert [d["client_id"] for d in rec.disconnects] == [DEV_X]
    assert sink.texts[-1].startswith("🟢 복귀")
    assert "현재 2명" in sink.texts[-1]
    assert ws_y in hub._conns
    assert ws_y.close_codes == []
    assert ws_x1 not in hub._conns
    assert hub.listener_count() == 2
    await _teardown(hub)


# AC-5 (boundary): the retired socket leaves the language channel, so
# broadcasts reach only the new socket and are not counted as lost
async def test_ac5_broadcast_after_replacement_reaches_only_new_socket():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1, ws2 = FakeWS(), FakeWS()
    await _attach(hub, ws1, "s1", client_id=DEV_X)
    await _attach(hub, ws2, "s1", client_id=DEV_X)
    await _drain()
    # Act
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await _drain()
    # Assert
    assert ws2.sent == ["m1"]
    assert ws1.sent == []
    assert all(ws1 not in peers for peers in hub._channels.values())
    await _teardown(hub)


# AC-5 (boundary): per-session gauge reflects the count after replacement
async def test_ac5_session_listener_gauge_is_one_after_replacement():
    # Arrange
    sid = "gauge-replace-s1"
    hub, rec, sink = _new_hub()
    await _attach(hub, FakeWS(), sid, client_id=DEV_X)
    # Act
    await _attach(hub, FakeWS(), sid, client_id=DEV_X)
    await _drain()
    # Assert
    value = REGISTRY.get_sample_value(
        "neemba_hub_session_listeners", {"sessionId": sid})
    assert value == 1.0
    await _teardown(hub)


# --- AC-6 --------------------------------------------------------------------


# AC-6
async def test_ac6_late_notices_for_replaced_socket_send_no_drop_alert():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1", client_id=DEV_X)
    await _drain()
    conn1 = hub._conns[ws1]
    ws2 = FakeWS()
    await _attach(hub, ws2, "s1", client_id=DEV_X)
    await _drain()
    # Act
    ws1.client_disconnect()
    await hub.handle_client_disconnect("s1", ws1, close_code=1006)
    await hub._drop_conn(conn1, detected_by="keepalive_timeout")
    await _drain()
    # Assert
    assert _drops(sink.texts) == []
    assert len(rec.disconnects) == 1
    assert rec.disconnects[0]["detected_by"] == "replaced"
    assert ws1.close_codes == [1000]
    assert ws2 in hub._conns
    assert hub.listener_count() == 1
    await _teardown(hub)


# --- AC-7 --------------------------------------------------------------------


# AC-7
async def test_ac7_attach_without_client_id_never_replaces():
    # Arrange
    hub, rec, sink = _new_hub()
    sockets = [FakeWS() for _ in range(4)]
    # Act
    ids = [
        await _attach(hub, sockets[0], "s1", client_id=None),
        await _attach(hub, sockets[1], "s1", client_id=None),
        await _attach(hub, sockets[2], "s1", client_id=""),
        await _attach(hub, sockets[3], "s1", client_id=""),
    ]
    await _drain()
    # Assert
    assert hub.listener_count() == 4
    assert rec.disconnects == []
    assert len(set(ids)) == 4
    assert "" not in ids
    assert None not in ids
    assert all(ws.close_codes == [] for ws in sockets)
    await _teardown(hub)


# --- AC-8 --------------------------------------------------------------------


async def _resolved(value: int) -> int:
    return value


def _fill_queue(hub: WebSocketHub, session_id: str, entries):
    """Put _OpenBlip(client_id) items into the session queue in order."""
    blips = [
        _OpenBlip(asyncio.create_task(_resolved(blip_id)), client_id)
        for client_id, blip_id in entries
    ]
    hub._open_blips[session_id] = deque(blips)
    return blips


# AC-8
async def test_ac8_match_returns_oldest_same_id_and_keeps_others_in_order():
    # Arrange
    hub = WebSocketHub()
    b1, b2, b3 = _fill_queue(hub, "s1", [(DEV_X, 1), (DEV_Y, 2), (DEV_X, 3)])
    # Act
    matched = hub._match_open_blip_locked("s1", DEV_X)
    # Assert
    assert matched is b1
    assert list(hub._open_blips["s1"]) == [b2, b3]
    await _drain()


# AC-8 (no fallback): an unknown id gets None and the queue is untouched
async def test_ac8_match_with_unknown_id_returns_none_and_leaves_queue():
    # Arrange
    hub = WebSocketHub()
    b1, b2, b3 = _fill_queue(hub, "s1", [(DEV_X, 1), (DEV_Y, 2), (DEV_X, 3)])
    # Act
    matched = hub._match_open_blip_locked("s1", DEV_Z)
    # Assert
    assert matched is None
    assert list(hub._open_blips["s1"]) == [b1, b2, b3]
    await _drain()


# AC-8 (empty queue): the session key is removed once the last blip is taken
async def test_ac8_session_key_removed_after_queue_is_emptied():
    # Arrange
    hub = WebSocketHub()
    b1, b2, b3 = _fill_queue(hub, "s1", [(DEV_X, 1), (DEV_Y, 2), (DEV_X, 3)])
    hub._match_open_blip_locked("s1", DEV_X)
    # Act
    got_y = hub._match_open_blip_locked("s1", DEV_Y)
    got_x = hub._match_open_blip_locked("s1", DEV_X)
    # Assert
    assert got_y is b2
    assert got_x is b3
    assert "s1" not in hub._open_blips
    assert hub._match_open_blip_locked("s1", DEV_X) is None
    await _drain()


# AC-8 (boundary): matching only looks at its own session's queue
async def test_ac8_match_ignores_other_sessions_queue():
    # Arrange
    hub = WebSocketHub()
    (b1,) = _fill_queue(hub, "s2", [(DEV_X, 1)])
    # Act
    matched = hub._match_open_blip_locked("s1", DEV_X)
    # Assert
    assert matched is None
    assert list(hub._open_blips["s2"]) == [b1]
    await _drain()


# --- AC-9 --------------------------------------------------------------------


# AC-9
async def test_ac9_same_client_id_in_other_session_is_not_replaced():
    # Arrange
    hub, rec, sink = _new_hub()
    ws_s1 = FakeWS()
    await _attach(hub, ws_s1, "s1", client_id=DEV_X)
    await _drain()
    # Act
    ws_s2 = FakeWS()
    await _attach(hub, ws_s2, "s2", client_id=DEV_X)
    await _drain()
    # Assert
    assert ws_s1 in hub._conns
    assert ws_s1.close_codes == []
    assert ws_s2 in hub._conns
    assert rec.disconnects == []
    assert rec.reconnects == []
    assert sink.texts[-1].startswith("🟢 청취자 접속")
    await _teardown(hub)


# --- other boundaries --------------------------------------------------------


# Boundary: an idempotent duplicate attach of the same socket never retires it
async def test_duplicate_attach_of_same_socket_with_client_id_is_not_replacement():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1", client_id=DEV_X)
    await _drain()
    # Act
    cid = await hub.attach(ws1, "s1", client_id=DEV_X)
    await _drain()
    # Assert
    assert cid == DEV_X
    assert ws1 in hub._conns
    assert ws1.close_codes == []
    assert rec.disconnects == []
    assert len(sink.texts) == 1
    await _teardown(hub)


# Boundary: a rejected attach (non-live session) never retires a live socket
async def test_rejected_attach_with_same_client_id_does_not_replace():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1", client_id=DEV_X)
    await _drain()
    # Act
    ws_rejected = FakeWS()
    result = await hub.attach(ws_rejected, "s1-not-live", client_id=DEV_X)
    await _drain()
    # Assert
    assert result is None
    assert ws_rejected.close_codes == [4404]
    assert ws1 in hub._conns
    assert ws1.close_codes == []
    assert rec.disconnects == []
    await _teardown(hub)


# Boundary: blip loss counted while dropped is carried by the exact-match close
async def test_lost_count_is_carried_to_exact_match_reconnect():
    # Arrange
    hub, rec, sink = _new_hub()
    ws1 = FakeWS()
    await _attach(hub, ws1, "s1", client_id=DEV_X)
    await _drain()
    await _drop_1006(hub, ws1, "s1")
    await hub.broadcast_to_session("s1", {"sentence": "m1"})
    await hub.broadcast_to_session("s1", {"sentence": "m2"})
    # Act
    ws_y = FakeWS()
    await _attach(hub, ws_y, "s1", client_id=DEV_Y)
    await hub.broadcast_to_session("s1", {"sentence": "m3"})
    ws_x2 = FakeWS()
    await _attach(hub, ws_x2, "s1", client_id=DEV_X)
    await _drain()
    # Assert
    assert rec.reconnects == [{"blip_id": 1, "lost_count": 2}]
    assert "유실 2건" in sink.texts[-1]
    await _teardown(hub)
