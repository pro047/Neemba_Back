import asyncio
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from typing import Dict, Any


class WebSocketHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.client: WebSocket | None = None

    async def attach(self, ws: WebSocket) -> None:
        async with self._lock:
            if self.client and self.client is not ws:
                await self._safe_close(self.client)
        await ws.accept()
        self.client = ws
        print('curr ws :', self.client)

    async def detach(self) -> None:
        async with self._lock:
            if self.client:
                await self._safe_close(self.client)
                self.client = None
                print('hub: detached')

    async def _safe_close(self, ws: WebSocket) -> None:
        try:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.close()
        except Exception:
            pass

    async def broadcast_to_session(self, payload: Dict[str, Any]) -> None:
        raw_text = payload.get('sentence')
        if raw_text is None:
            print('hub: skip send, sentence is None')
            return

        text = str(raw_text)

        async with self._lock:
            ws = self.client
            if not ws:
                print('hub: No client connected :', ws)
                return

            if ws.application_state != WebSocketState.CONNECTED:
                print('hub: skip send, state:', ws.application_state)
                await self.detach()
                return

            try:
                await ws.send_text(text)
                print('hub: broadcast:', text)

            except Exception as e:
                print('hub: send failed:', e)
                await self._safe_close(ws)
                self.client = None
