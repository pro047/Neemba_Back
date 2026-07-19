import asyncio
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from typing import Deque, Dict, Any, Optional
from collections import deque
import time


class WebSocketHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.client: Optional[WebSocket] = None
        # 동시 1세션 전제: 풀 dict 맵 대신 '지금 슬롯의 주인' sessionId 1개만 추적
        self._session_id: Optional[str] = None
        self._send_gate = asyncio.Semaphore(1)
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_pong_time = 0
        self._first_pong_received = False  # 첫 pong을 받았는지 추적
        self._first_ping_sent_time = 0  # REV-3: 첫 ping 송신 시각(초기 pong 타임아웃 기준)
        self._reconnect_waiting = False
        self._reconnect_waiting_since = 0
        self._pending: Deque[str] = deque()
        self._max_pending = 100

    @staticmethod
    def _is_connected(ws: WebSocket) -> bool:
        # §4-3(원인 2): 클라 주도 끊김 시 starlette 는 client_state 만
        # DISCONNECTED 로 바꾸고 application_state 는 CONNECTED 로 남긴다.
        # application_state 만 보면 죽은 소켓에 send 를 반복 시도하게 되므로
        # 연결 검사는 반드시 두 상태를 함께 본다.
        return (
            ws.client_state == WebSocketState.CONNECTED
            and ws.application_state == WebSocketState.CONNECTED
        )

    async def attach(self, ws: WebSocket, session_id: str) -> None:
        async with self._lock:
            if self.client and self.client is not ws:
                await self._safe_close(self.client)
            # 세션 주인이 바뀌면 이전 세션의 미전송 큐를 비운다 (에러B: 교차 전송 방지)
            if self._session_id is not None and self._session_id != session_id:
                self._pending.clear()
            self._session_id = session_id
        await ws.accept()
        was_reconnecting = self._reconnect_waiting
        self.client = ws
        self._last_pong_time = 0  # 초기값은 0 (첫 pong 받기 전까지는 타임아웃 체크 안 함)
        self._first_pong_received = False  # 첫 pong 아직 받지 않음
        self._first_ping_sent_time = 0  # REV-3: 새 연결마다 초기 pong 타임아웃 기준점 리셋
        self._reconnect_waiting = False
        self._reconnect_waiting_since = 0
        if was_reconnecting:
            print('curr ws : reconnected successfully!', self.client)
        else:
            print('curr ws :', self.client, 'session:', session_id)

        # 기존 keepalive 태스크 취소
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()

        # 새로운 keepalive 태스크 시작
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        asyncio.create_task(self._flush_pending(ws))

    async def detach(self, session_id: str) -> None:
        async with self._lock:
            # 다른 세션의 stop은 현재 소켓을 끊지 못한다 (mic/rtmp 교차 종료 방지의 핵심)
            if session_id != self._session_id:
                print('hub: detach ignored, not current session',
                      session_id, 'current=', self._session_id)
                return
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
            if self.client:
                await self._safe_close(self.client)
                self.client = None
            self._session_id = None
            self._pending.clear()
            print('hub: detached', session_id)

    async def broadcast_to_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        raw_text = payload.get('sentence')
        if raw_text is None:
            print('hub: skip send, sentence is None')
            return

        text = str(raw_text)

        async with self._lock:
            # 슬롯 주인이 아닌 세션의 번역은 stale → drop (큐잉하지 않음, 에러B)
            if session_id != self._session_id:
                print('hub: drop stale broadcast', session_id,
                      'current=', self._session_id)
                return
            ws = self.client

            # REV-1: 주인검사·연결검사·pending append 를 같은 락 안에서 처리한다.
            # 둘로 쪼개면 두 락 사이에 새 세션 attach(_pending.clear + _session_id 교체)가
            # 끼어들어, 이전 세션 텍스트가 pending 에 들어가 새 클라로 새는 교차전송(에러B)
            # 창이 남는다. 한 락으로 묶으면 attach 와 직렬화되어 그 창이 닫힌다.
            if ws is None or not self._is_connected(ws):
                if len(self._pending) >= self._max_pending:
                    self._pending.popleft()
                self._pending.append(text)
                state = (ws.client_state, ws.application_state) if ws is not None else None
                print("hub: queued send, ws not connected", state, f"pending={len(self._pending)}")
                return

        asyncio.create_task(self._send_text(ws, text, session_id))

    async def _requeue(self, session_id: str, text: str) -> None:
        # §4-3(원인 3): 전송하지 못한 문장은 버리지 않고 pending 앞쪽에 되돌려
        # 재접속 _flush_pending 이 방류하게 한다. 슬롯 주인이 바뀌었거나
        # 세션이 이미 끝났으면(detach 로 None) stale → drop.
        async with self._lock:
            if session_id is None or session_id != self._session_id:
                return
            if len(self._pending) >= self._max_pending:
                self._pending.popleft()
            self._pending.appendleft(text)
            print('hub: re-queued unsent text', f"pending={len(self._pending)}")

    async def _send_text(self, ws: WebSocket, text: str, session_id: str) -> None:
        try:
            requeue = False
            async with self._send_gate:
                # 에러A 수정: 게이트 획득 후 send 직전에 상태 재확인 (TOCTOU 해소).
                # 게이트 대기 중 detach/close 가 일어났을 수 있으므로 닫혔으면
                # §4-3 에 따라 버리지 않고 재적재.
                # 락 순서 불변식('_lock→_send_gate'만 허용) 때문에 _requeue(_lock)는
                # 반드시 게이트 블록 밖에서 호출한다 — 안에서 부르면 ABBA 데드락.
                if not self._is_connected(ws):
                    requeue = True
                else:
                    await ws.send_text(text)
            if requeue:
                await self._requeue(session_id, text)
                return
            print('hub: broadcast:', text)

        except Exception as e:
            print('hub: send failed:', e)
            # §4-3(원인 3): 실패 문장 재적재 (게이트는 이미 빠져나온 상태)
            await self._requeue(session_id, text)
            # 연결이 끊어진 경우에만 클라이언트 초기화
            if not self._is_connected(ws):
                await self._safe_close(ws)
                async with self._lock:
                    # REV-2: send 실패와 락 획득 사이에 새 클라가 attach 됐을 수 있으므로
                    # 현재 슬롯이 여전히 이 ws 일 때만 비운다(새 클라 오염 방지).
                    if self.client is ws:
                        self.client = None

    async def _safe_close(self, ws: WebSocket) -> None:
        # REV-4(에러A): close 도 send 와 같은 _send_gate 로 직렬화한다.
        # starlette 의 send_text/_send_ping/close 는 모두 같은 ASGI send 채널로 메시지를
        # 흘리므로, _send_text 가 send 를 await 하는 '도중' 게이트 밖에서 close 가 끼어들면
        # 'send after websocket.close'(에러A)가 난다. 게이트로 묶으면 close 는 진행 중인
        # send 가 끝날 때까지 대기하고, close 가 먼저면 후속 _send_text 가 게이트 안
        # 재확인(application_state)에서 return 한다.
        #
        # 락 순서 불변식: '_lock 이 _send_gate 를 감쌀 수는 있어도 그 반대는 금지'.
        # detach/attach 는 _lock 보유 중 _safe_close(→_send_gate)를 호출(_lock→_send_gate).
        # _safe_close 를 _send_gate 보유 중에 호출하는 경로는 없어야 한다(Semaphore 비재진입).
        try:
            async with self._send_gate:
                if ws.application_state == WebSocketState.CONNECTED:
                    await ws.close()
        except Exception:
            pass

    async def _flush_pending(self, ws: WebSocket) -> None:
        async with self._lock:
            if not self._pending:
                return
            # flush 는 attach 직후 그 세션의 소켓으로만 실행되므로 이 시점의
            # 슬롯 주인이 곧 이 flush 의 세션이다 (_send_text 재적재 판정용).
            session_id = self._session_id
            pending = list(self._pending)
            self._pending.clear()
        if session_id is None:
            return
        for i, text in enumerate(pending):
            if not self._is_connected(ws):
                async with self._lock:
                    for item in pending[i:]:
                        if len(self._pending) >= self._max_pending:
                            self._pending.popleft()
                        self._pending.append(item)
                print("hub: flush interrupted, re-queued", f"pending={len(self._pending)}")
                return
            await self._send_text(ws, text, session_id)
        print("hub: flushed pending", f"count={len(pending)}")

    def _mark_waiting_for_reconnect_locked(self) -> None:
        # 소켓만 비우고 _session_id·pending 은 보존한다 — 세션은 살아 있고
        # 연결만 죽은 상태이므로, 이후 번역은 pending 에 쌓였다가 재접속
        # attach 의 _flush_pending 으로 방류된다. (호출자가 _lock 보유 전제)
        self.client = None
        self._reconnect_waiting = True
        self._reconnect_waiting_since = time.time()
        self._first_pong_received = False
        self._last_pong_time = 0

    async def _mark_waiting_for_reconnect(self) -> None:
        async with self._lock:
            self._mark_waiting_for_reconnect_locked()

    async def handle_client_disconnect(self, session_id: str, ws: WebSocket) -> None:
        """/ws 엔드포인트가 WebSocketDisconnect 를 잡는 즉시 호출 (§4-3 원인 1).

        detach 와 달리 pending 을 비우지 않는다 — 끊김~재접속 사이의 번역을
        보존해 유실을 막기 위함. 주인 검사로 늦게 도착한 통지(이미 새 소켓으로
        교체된 뒤)는 무시한다.
        """
        async with self._lock:
            if session_id != self._session_id or self.client is not ws:
                print('hub: disconnect notice ignored (stale)', session_id)
                return
            self._mark_waiting_for_reconnect_locked()
            pending = len(self._pending)
        print('hub: client disconnected, waiting for reconnect',
              session_id, f'pending={pending}')

    async def _send_ping(self, ws: WebSocket) -> bool:
        """ping 전송 (클라이언트는 자동으로 pong 응답해야 함)"""
        try:
            # ping 메시지를 JSON으로 전송
            # 클라이언트는 이를 받으면 자동으로 {"type": "pong"}을 보내야 함
            async with self._send_gate:
                if not self._is_connected(ws):
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

                if ws is None or not self._is_connected(ws):
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
                    # 에러C 수정: 게이트 역전 제거.
                    # 첫 pong을 받은 적이 있을 때만 half-open 타임아웃을 체크하고,
                    # 그 외에는 매 주기 ping 을 '무조건' 보낸다. (이전엔 첫 pong 전까지
                    # ping 을 안 보내 서버·클라가 서로를 영원히 기다리는 데드락이 있었음.)
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
                    elif self._first_ping_sent_time > 0:
                        # REV-3: 첫 ping 을 보냈는데 첫 pong 이 한 번도 안 옴(초기 half-open).
                        # 에러C 수정으로 데드락은 풀렸지만, 처음부터 pong 을 못 보내는 클라가
                        # 붙으면 서버가 ping 만 무한 전송하게 된다. 첫 ping 후 60초 안에
                        # 첫 pong 이 없으면 끊고 재연결 대기로 넘긴다.
                        time_since_first_ping = time.time() - self._first_ping_sent_time
                        if time_since_first_ping > 60:
                            print(
                                f'keepalive: no first pong for {time_since_first_ping:.1f}s, closing and preparing for reconnect')
                            await self._safe_close(ws)
                            await self._mark_waiting_for_reconnect()
                            await asyncio.sleep(5)
                            continue

                    # ping 전송
                    # ping 직전에 현재 클라이언트인지와 상태를 다시 확인
                    async with self._lock:
                        current = self.client

                    if current is not ws:
                        print('keepalive: client replaced before ping, skipping this round')
                        continue

                    # 클라이언트는 이를 받으면 자동으로 {"type": "pong"}을 보내야 함
                    sent = await self._send_ping(ws)
                    if not sent:
                        raise ConnectionError('ping not sent; websocket closed')
                    # REV-3: 첫 ping 송신 시각 기록(초기 pong 타임아웃 기준점). 최초 1회만.
                    if self._first_ping_sent_time == 0:
                        self._first_ping_sent_time = time.time()
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
