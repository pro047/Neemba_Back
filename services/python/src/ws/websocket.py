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
        self._first_pong_received = False  # 첫 pong을 받았는지 추적
        self._reconnect_waiting = False
        self._reconnect_waiting_since = 0

    async def attach(self, ws: WebSocket) -> None:
        async with self._lock:
            if self.client and self.client is not ws:
                await self._safe_close(self.client)
        await ws.accept()
        was_reconnecting = self._reconnect_waiting
        self.client = ws
        self._last_pong_time = 0  # 초기값은 0 (첫 pong 받기 전까지는 타임아웃 체크 안 함)
        self._first_pong_received = False  # 첫 pong 아직 받지 않음
        self._reconnect_waiting = False
        self._reconnect_waiting_since = 0
        if was_reconnecting:
            print('curr ws : reconnected successfully!', self.client)
        else:
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
            async with self._send_gate:
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

    async def _mark_waiting_for_reconnect(self) -> None:
        async with self._lock:
            self.client = None
            self._reconnect_waiting = True
            self._reconnect_waiting_since = time.time()
            self._first_pong_received = False
            self._last_pong_time = 0

    async def _send_ping(self, ws: WebSocket) -> bool:
        """ping 전송 (클라이언트는 자동으로 pong 응답해야 함)"""
        try:
            # ping 메시지를 JSON으로 전송
            # 클라이언트는 이를 받으면 자동으로 {"type": "pong"}을 보내야 함
            async with self._send_gate:
                if ws.application_state != WebSocketState.CONNECTED:
                    return False
                await ws.send_json({"type": "ping"})
            return True
        except Exception as e:
            msg = str(e)
            # 연결이 이미 종료된 뒤 ping을 보내려 하면 Starlette가 RuntimeError를 발생시킴
            if isinstance(e, RuntimeError) and ("websocket.send" in msg or "close" in msg.lower()):
                print('ping skipped: websocket already closed')
                return False
            print(f'ping send error: {e}')
            raise

    async def _reconnect_ws(self, ws: WebSocket) -> None:
        pass

    async def _keepalive_loop(self) -> None:
        """주기적으로 ping을 보내서 연결을 유지"""
        try:
            while True:
                await asyncio.sleep(30)  # 30초마다 ping

                async with self._lock:
                    ws = self.client
                    reconnect_waiting = self._reconnect_waiting
                    reconnect_waiting_since = self._reconnect_waiting_since

                if ws is None or ws.application_state != WebSocketState.CONNECTED:
                    if reconnect_waiting:
                        # 재연결 대기 시간 확인 (5분 제한)
                        wait_time = time.time() - reconnect_waiting_since
                        if wait_time > 300:  # 5분
                            print(
                                f'keepalive: reconnection timeout after {wait_time:.1f}s, giving up')
                            async with self._lock:
                                self._reconnect_waiting = False
                                self._reconnect_waiting_since = 0
                            break
                        print(
                            f'keepalive: waiting for reconnection... ({wait_time:.0f}s / 300s)')
                        # 재연결 대기 중 - 클라이언트가 재연결할 때까지 기다림
                        await asyncio.sleep(5)
                        continue
                    else:
                        print('keepalive: no client, exiting')
                        break

                try:
                    # 첫 pong을 받은 후에만 타임아웃 체크
                    if self._first_pong_received and self._last_pong_time > 0:
                        # 60초 이상 pong이 없으면 연결 끊고 재연결 준비
                        time_since_last_pong = time.time() - self._last_pong_time
                        if time_since_last_pong > 60:
                            print(
                                f'keepalive: no pong for {time_since_last_pong:.1f}s, closing and preparing for reconnect')
                            await self._safe_close(ws)
                            await self._mark_waiting_for_reconnect()
                            print(
                                'keepalive: connection closed, waiting for client to reconnect...')
                            # 재연결 대기 루프로 전환
                            await asyncio.sleep(5)
                            continue
                    else:
                        # 첫 pong을 아직 받지 않았으면 타임아웃 체크 안 함
                        print('keepalive: waiting for first pong...')

                    # ping 전송
                    # ping 직전에 현재 클라이언트인지와 상태를 다시 확인
                    async with self._lock:
                        current = self.client

                    if current is not ws:
                        print('keepalive: client replaced before ping, skipping this round')
                        continue

                    # 클라이언트는 이를 받으면 자동으로 {"type": "pong"}을 보내야 함
                    # 클라이언트 코드에서 ping을 받으면 pong만 보내고 무시하도록 처리 필요
                    sent = await self._send_ping(ws)
                    if not sent:
                        raise ConnectionError('ping not sent; websocket closed')
                    print('keepalive: sent ping')
                except Exception as e:
                    print(f'keepalive: error {e}, preparing for reconnect')
                    await self._safe_close(ws)
                    await self._mark_waiting_for_reconnect()
                    print(
                        'keepalive: connection error, waiting for client to reconnect...')
                    # 재연결 대기 루프로 전환
                    await asyncio.sleep(5)
                    continue
        except asyncio.CancelledError:
            print('keepalive: cancelled')
        except Exception as e:
            print(f'keepalive: unexpected error {e}')

    async def on_pong(self) -> None:
        """클라이언트로부터 pong을 받으면 호출"""
        self._last_pong_time = time.time()
        if not self._first_pong_received:
            self._first_pong_received = True
            print('keepalive: first pong received!')
