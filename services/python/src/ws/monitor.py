"""Monitor-only WebSocket hub (``/ws/monitor?sessionId=``).

Separate from the client-facing :class:`WebSocketHub`. Where the client hub is
single-consumer and replays a backlog, this hub is a simple live fan-out:
multiple monitor dashboards may subscribe to the same ``sessionId`` and each
receives the full payload (masked 원문 + 번역문 + meta) as it is produced.

No backlog/replay here — monitors only see traffic while connected; history
is served separately by the Phase 5 query API.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState


class MonitorHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # sessionId -> set of live monitor sockets
        self._subscribers: dict[str, set[WebSocket]] = {}

    async def attach(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._subscribers.setdefault(session_id, set()).add(ws)
        print(f"monitor: attached session={session_id} subs={self._subscriber_count(session_id)}")

    async def detach(self, session_id: str, ws: WebSocket) -> None:
        async with self._lock:
            subs = self._subscribers.get(session_id)
            if subs is not None:
                subs.discard(ws)
                if not subs:
                    self._subscribers.pop(session_id, None)

    def _subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))

    async def broadcast(self, session_id: str, payload: dict[str, Any]) -> None:
        """Send ``payload`` to every live subscriber of ``session_id``.

        Dead/closed sockets are dropped silently; a single failing subscriber
        never blocks the others or the caller (this runs on the fire-and-forget
        capture path).
        """
        async with self._lock:
            subs = list(self._subscribers.get(session_id, ()))
        if not subs:
            return
        dead: list[WebSocket] = []
        for ws in subs:
            try:
                if ws.application_state == WebSocketState.CONNECTED:
                    await ws.send_json(payload)
                else:
                    dead.append(ws)
            except Exception as e:
                print("monitor: send failed (dropping subscriber):", repr(e))
                dead.append(ws)
        for ws in dead:
            await self.detach(session_id, ws)

    async def close_session(
        self, session_id: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Emit a final close event to all subscribers and drop the session.

        Called once from the idempotent stop handler. Pops the subscriber set
        first so a concurrent/duplicate stop finds nothing left to close
        (no duplicate close events).
        """
        async with self._lock:
            subs = list(self._subscribers.pop(session_id, ()))
        if not subs:
            return
        for ws in subs:
            try:
                if ws.application_state == WebSocketState.CONNECTED:
                    if payload is not None:
                        await ws.send_json(payload)
                    await ws.close()
            except Exception as e:
                print("monitor: close failed (ignored):", repr(e))
        print(f"monitor: closed session={session_id} subs={len(subs)}")
