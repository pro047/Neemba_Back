import asyncio
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from typing import Dict, Any, Optional
import time


class WebSocketHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.client: Optional[WebSocket] = None
        self._send_gate = asyncio.Semaphore(1)
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_pong_time = 0

    async def attach(self, ws: WebSocket) -> None:
        async with self._lock:
            if self.client and self.client is not ws:
                await self._safe_close(self.client)
        await ws.accept()
        self.client = ws
        self._last_pong_time = time.time()
        print('curr ws :', self.client)
        
        # 기존 keepalive 태스크 취소
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            
        # 새로운 keepalive 태스크 시작
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def detach(self) -> None:
        async with self._lock:
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
            if self.client:
                await self._safe_close(self.client)
                self.client = None
                print('hub: detached')

    async def broadcast_to_session(self, payload: Dict[str, Any]) -> None:
        raw_text = payload.get('sentence')
        if raw_text is None:
            print('hub: skip send, sentence is None')
            return

        text = str(raw_text)

        async with self._lock:
            ws = self.client

        if ws is None or ws.application_state != WebSocketState.CONNECTED:
            return

        asyncio.create_task(self._send_text(ws, text))

    async def _send_text(self, ws: WebSocket, text: str) -> None:
        try:
            await ws.send_text(text)
            print('hub: broadcast:', text)

        except Exception as e:
            print('hub: send failed:', e)
            # 연결이 끊어진 경우에만 클라이언트 초기화
            if ws.application_state != WebSocketState.CONNECTED:
                await self._safe_close(ws)
                async with self._lock:
                    self.client = None

    async def _safe_close(self, ws: WebSocket) -> None:
        try:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.close()
        except Exception:
            pass

    async def _reconnect_ws(self, ws: WebSocket) -> None:
        pass
    
    async def _keepalive_loop(self) -> None:
        """주기적으로 ping을 보내서 연결을 유지"""
        try:
            while True:
                await asyncio.sleep(30)  # 30초마다 ping
                
                async with self._lock:
                    ws = self.client
                
                if ws is None or ws.application_state != WebSocketState.CONNECTED:
                    print('keepalive: no client, exiting')
                    break
                
                try:
                    # 60초 이상 pong이 없으면 연결 끊김
                    if time.time() - self._last_pong_time > 60:
                        print('keepalive: no pong for 60s, closing')
                        await self._safe_close(ws)
                        async with self._lock:
                            self.client = None
                        break
                    
                    # ping 전송
                    await ws.send_json({"type": "ping"})
                    print('keepalive: sent ping')
                except Exception as e:
                    print(f'keepalive: error {e}')
                    await self._safe_close(ws)
                    async with self._lock:
                        self.client = None
                    break
        except asyncio.CancelledError:
            print('keepalive: cancelled')
        except Exception as e:
            print(f'keepalive: unexpected error {e}')
    
    async def on_pong(self) -> None:
        """클라이언트로부터 pong을 받으면 호출"""
        self._last_pong_time = time.time()
