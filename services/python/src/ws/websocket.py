"""청취자 소켓 팬아웃 허브 (멀티 청취자 P1 — `docs/multi-listener-p1-plan.md`).

P1 이전에는 허브가 소켓을 1개만 들었다. 두 번째 청취자가 붙으면 첫 소켓을
닫았고(`attach` 의 무조건 `_safe_close`), send gate·keepalive·pong·blip 이
전부 '그 하나의 소켓' 전제로 허브 필드에 있었다.

지금 구조:

    세션 (session_id)                     ← 라이브 여부 = _sessions 등재 여부
      └─ 언어 채널 (session_id, lang)     ← _channels. P1 은 세션당 1개 (D2)
           ├─ 소켓 → _Conn               ← 소켓별 자원의 단일 소유자
           └─ 소켓 → _Conn

D2 로 채널 키를 처음부터 ``(session_id, target_lang)`` 으로 둔다. P1 에서는
언어가 1개일 뿐이고, P2(청취자별 언어)가 자료구조를 갈아엎지 않게 하는 자리다.

D3 으로 백로그(``_pending``)를 들어냈다. 커서 없이 멀티 소켓에 백로그를
태우면 붙어 있던 청취자에게 중복 전송이 되고, 커서를 넣으면 전송 포맷이
JSON 으로 바뀌어 앱 배포가 필요해진다. 순단 구간 자막은 P3 까지 유실을
감수하며, 유실량은 blip 의 ``lost_count`` 로 집계한다.

지켜야 할 불변식 (계획 §7):
- 락 순서는 ``_lock → conn.send_gate`` 만. 역방향은 ABBA 데드락이다
- send gate 는 **소켓별**. 전역이면 느린 청취자 1명이 전원의 자막을 막는다
- 연결 검사는 ``client_state`` 와 ``application_state`` 를 함께 본다
- ``_safe_close`` 는 send 와 같은 게이트로 직렬화한다 (에러A)
- 다른 세션의 번역은 큐잉 없이 drop 한다 (에러B 교차 전송 방지)
"""
import asyncio
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from src.monitoring import metrics
from src.ws.blip_recorder import WsBlipRecorder

# node 의 rtmp 라우터 zod 기본값과 같은 값 (`router/rtmp.ts:42`).
DEFAULT_TARGET_LANG = "en-US"

# 라이브가 아닌 세션으로 붙은 소켓의 close code. 1008(파라미터 오류)과 갈라두면
# 서버 로그에서 '세션 없음' 과 'sessionId 누락' 이 구분된다. 앱은 아직 close
# code 로 분기하지 않지만(P2 배포 때 재시도 중단에 쓴다) 코드 자체는 지금부터
# 정확한 값을 실어 보낸다.
CLOSE_SESSION_NOT_FOUND = 4404

# 세션이 끝나서 서버가 먼저 닫는 소켓의 close code. 4404 와 갈라두는 이유는 두
# 가지다. 로그에서 '처음부터 없던 세션' 과 '방금 끝난 세션' 이 구분되고, 무엇보다
# **클라이언트가 재접속 한 번 없이 이 프레임만으로 만료를 안다** — 기본값 1000 은
# 정상 종료와 네트워크 순단이 같은 값이라, 붙어보고 4404 를 받기 전에는 재시도를
# 멈춰야 할지 판단할 수 없었다. 클라이언트 동작은 4404 와 같다(재시도 중단).
CLOSE_SESSION_ENDED = 4410

_PING_INTERVAL_SECONDS = 30
_PONG_TIMEOUT_SECONDS = 60


def _norm_lang(lang: str | None) -> str:
    """언어 채널 키 정규화. 'en-US' 와 'en-us' 가 다른 채널이 되면 안 된다."""
    return (lang or DEFAULT_TARGET_LANG).strip().lower()


class _Conn:
    """소켓 1개분의 상태 — send gate·keepalive·pong·blip 식별자가 모두 여기 붙는다.

    P1 이전에는 이 값들이 허브 필드였다. 소켓이 N개가 된 지금도 허브에 두면
    소켓 간 간섭이 그대로 버그가 된다: 세마포어 1개는 head-of-line blocking,
    pong 타임스탬프 1개는 '남의 pong 으로 살아있는 판정'이다.
    """

    __slots__ = (
        "ws", "session_id", "target_lang", "client_id", "send_gate",
        "keepalive_task", "last_pong_time", "first_pong_received",
        "first_ping_sent_time",
    )

    def __init__(
        self,
        ws: WebSocket,
        session_id: str,
        target_lang: str,
        client_id: str,
    ) -> None:
        self.ws = ws
        self.session_id = session_id
        self.target_lang = target_lang
        self.client_id = client_id
        self.send_gate = asyncio.Semaphore(1)
        self.keepalive_task: asyncio.Task | None = None
        self.last_pong_time = 0.0
        self.first_pong_received = False
        self.first_ping_sent_time = 0.0


class _OpenBlip:
    """기록 중인 순단 1건. ``task`` 는 insert 를 돌리는 fire-and-forget 태스크."""

    __slots__ = ("task", "client_id", "lost")

    def __init__(self, task: "asyncio.Task[int | None]", client_id: str) -> None:
        self.task = task
        self.client_id = client_id
        self.lost = 0


class WebSocketHub:
    def __init__(self, blip_recorder: WsBlipRecorder | None = None) -> None:
        self._lock = asyncio.Lock()
        # (session_id, lang) -> 그 언어 채널에 붙은 소켓 집합 (D2).
        self._channels: Dict[tuple[str, str], set[WebSocket]] = {}
        # ws -> _Conn. 소켓별 자원의 단일 소유자이자 '붙어 있는 소켓' 명부.
        self._conns: Dict[WebSocket, _Conn] = {}
        # 라이브 세션 -> 등록된 언어. broadcast stale drop 의 판정 기준(에러B)
        # 이자 /ws 가 소켓을 어느 채널에 넣을지 정하는 근거다.
        self._sessions: Dict[str, str] = {}
        # §4-7 순단 계측: 끊김 시 ws_blips insert(fire-and-forget task 가 id 로
        # resolve) → 재접속 시 종결. 미주입(None)이면 전부 no-op.
        self._blip_recorder = blip_recorder
        # session_id -> 열린 blip 들(오래된 것부터). D5 로 소켓별 1행이다.
        self._open_blips: Dict[str, Deque[_OpenBlip]] = {}
        # fire-and-forget 태스크의 강한 참조 (`pushClient/pusher.py:49` 와 같은
        # 관용구). 이벤트 루프는 실행 중인 태스크를 약한 참조로만 들고 있어,
        # create_task 의 반환값을 아무도 안 붙잡으면 GC 가 그 태스크를 거둬갈 수
        # 있다. 여기서는 자막 1건이 조용히 사라지는 것이고, D3 으로 재적재가
        # 없어져 복구 경로가 없다.
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- 태스크 수명 -------------------------------------------------------

    def _spawn(self, coro: "Any") -> "asyncio.Task[Any]":
        """fire-and-forget 코루틴을 태스크로 띄우고 참조를 붙잡는다.

        ``add_done_callback(discard)`` 로 끝난 태스크는 스스로 빠지므로 set 이
        무한히 자라지 않는다. keepalive 루프는 여기 담지 않는다 — 그건
        ``conn.keepalive_task`` 가 소켓 수명 동안 이미 붙잡고 있다.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- 연결 상태 ---------------------------------------------------------

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

    def is_client_connected(self) -> bool:
        # WU5 상태 개요(GET /api/monitor/status): 자막 기기가 하나라도 붙어
        # 있는지. 읽기 전용 — 락 없이 스냅샷만 본다 (표시용, 정합성 요구 없음).
        return any(self._is_connected(ws) for ws in list(self._conns))

    def listener_count(self) -> int:
        """지금 붙어 있는 청취자 소켓 수 (P1: 상태 개요·게이지용)."""
        return sum(1 for ws in list(self._conns) if self._is_connected(ws))

    # --- 세션 등록 ---------------------------------------------------------

    async def register_session(
        self, session_id: str, target_lang: str | None = None
    ) -> None:
        """세션을 라이브로 등록한다 (`/internal/sessions/start`).

        청취자보다 먼저 불린다 — 첫 소켓이 붙기 전에 언어 채널이 정해져야
        브로드캐스트가 갈 곳을 안다. 재호출은 언어만 갱신하는 멱등 연산이다.
        """
        lang = _norm_lang(target_lang)
        async with self._lock:
            previous = self._sessions.get(session_id)
            self._sessions[session_id] = lang
            if previous is not None and previous != lang:
                # 세션의 언어가 바뀌면 옛 채널의 소켓들을 새 채널로 옮긴다.
                # 안 옮기면 그 소켓들은 등재된 채로 영원히 자막을 못 받는다.
                self._move_channel_locked(session_id, previous, lang)
            metrics.set_active_session(True)
        print('hub: session registered', session_id, lang)

    def _move_channel_locked(
        self, session_id: str, old_lang: str, new_lang: str
    ) -> None:
        peers = self._channels.pop((session_id, old_lang), None)
        if not peers:
            return
        target = self._channels.setdefault((session_id, new_lang), set())
        for ws in peers:
            target.add(ws)
            conn = self._conns.get(ws)
            if conn is not None:
                conn.target_lang = new_lang

    # --- attach / detach ---------------------------------------------------

    async def attach(
        self,
        ws: WebSocket,
        session_id: str,
        *,
        target_lang: str | None = None,
        client_id: str | None = None,
    ) -> str | None:
        """청취자 소켓을 세션의 언어 채널에 등재한다. 발급된 client_id 를 반환.

        **기존 소켓을 닫지 않는다** — 여기가 P1 의 본질이다. 같은 세션에 N명이
        동시에 붙을 수 있고, 각자 자기 ``_Conn`` 을 갖는다.

        **라이브 세션에만 붙일 수 있다.** ``_sessions`` 에 없는 sessionId 면
        등재하지 않고 ``4404`` 로 닫은 뒤 ``None`` 을 돌려준다. 세션을 라이브로
        만드는 곳은 ``register_session`` 하나뿐이다 — 소켓이 자기를 등재할 수
        있으면 종료된 세션이 재접속만으로 되살아나고, 그러면 stale drop 규약
        (§7 에러B)과 ``neemba_hub_active_session`` 낙하가 함께 무너진다.

        ``client_id`` 는 호출자가 줄 수 있고(앱이 보내기 시작하면 P2 에서 정확한
        순단 매칭이 된다) 없으면 서버가 발급한다.
        """
        cid = client_id or uuid.uuid4().hex
        # accept 를 등재보다 먼저: 등재된 소켓에 accept 전 send 가 나가면
        # application_state 가 CONNECTING 이라 그 자막만 조용히 버려진다.
        # 거절할 때도 accept 는 필요하다 — accept 전 close 는 핸드셰이크 실패
        # (HTTP 403)라 close code 가 클라에 도달하지 않는다.
        await ws.accept()

        conn: _Conn | None = None
        reopened: _OpenBlip | None = None

        async with self._lock:
            existing = self._conns.get(ws)
            if existing is not None:
                # 같은 소켓의 중복 attach 는 멱등 — 채널을 두 번 등재하지 않는다.
                print('hub: attach ignored, already attached', existing.client_id)
                return existing.client_id

            # 화이트리스트: 세션에 등록된 언어가 곧 이 소켓이 들어갈 채널이다.
            # target_lang 은 P2 에서 '같은 세션의 다른 언어 채널' 을 고를 때
            # 쓸 훅이고, 세션을 만드는 근거가 되지는 않는다.
            lang = self._sessions.get(session_id)
            if lang is not None:
                if target_lang and _norm_lang(target_lang) != lang:
                    # P2 전까지 세션의 언어는 하나뿐이라 요청 언어를 들어줄 수
                    # 없다. 조용히 무시하면 '내 언어로 듣는 중' 오해가 남는다.
                    print('hub: requested lang ignored',
                          _norm_lang(target_lang), '->', lang)
                conn = _Conn(ws, session_id, lang, cid)
                self._conns[ws] = conn
                self._channels.setdefault((session_id, lang), set()).add(ws)
                metrics.set_listeners(len(self._conns))

                # §4-7: 이 재접속이 세션의 열린 blip 을 닫는다. 같은 락 안에서
                # 판정해야 attach 경쟁과 직렬화된다.
                reopened = self._match_open_blip_locked(session_id, cid)

        if conn is None:
            print('hub: attach rejected, not a live session', session_id,
                  'live=', sorted(self._sessions))
            try:
                await ws.close(code=CLOSE_SESSION_NOT_FOUND)
            except Exception as e:
                print('hub: reject close failed (ignored):', repr(e))
            return None

        if reopened is not None:
            self._spawn(self._finish_blip(reopened))

        conn.keepalive_task = asyncio.create_task(self._keepalive_loop(conn))
        print('curr ws :', ws, 'session:', session_id,
              'lang:', conn.target_lang, 'client:', cid)
        return cid

    async def detach(self, session_id: str) -> None:
        """세션 종료: 그 세션의 모든 소켓을 닫고 라이브 등록을 해제한다.

        다른 세션의 stop 은 현재 소켓들을 끊지 못한다 (mic/rtmp 교차 종료 방지).
        """
        async with self._lock:
            if session_id not in self._sessions:
                print('hub: detach ignored, not a live session', session_id,
                      'live=', sorted(self._sessions))
                return
            conns = [c for c in self._conns.values() if c.session_id == session_id]
            for conn in conns:
                self._forget_conn_locked(conn)
            self._sessions.pop(session_id, None)
            # §11 F-1: 세션이 끝나면 열린 blip 도 미복귀로 마감하고 슬롯을 반납한다.
            abandoned = self._release_blips_locked(session_id)
            metrics.set_active_session(bool(self._sessions))
            metrics.set_listeners(len(self._conns))

        # close 는 락 밖에서. 안에서 하면 게이트를 쥔 느린 소켓 하나가
        # 허브 전체(_lock)를 얼린다 — 소켓이 N개가 된 지금은 그게 전면 정지다.
        if conns:
            await asyncio.gather(
                *(
                    self._safe_close(c, close_code=CLOSE_SESSION_ENDED)
                    for c in conns
                ),
                return_exceptions=True,
            )
        for conn in conns:
            self._cancel_keepalive(conn)
        for blip in abandoned:
            self._spawn(self._abandon_blip(blip))
        print('hub: detached', session_id, f'sockets={len(conns)}')

    def _forget_conn_locked(self, conn: _Conn) -> None:
        """명부에서만 지운다 — close 와 blip 은 호출자 책임. (_lock 보유 전제)"""
        self._conns.pop(conn.ws, None)
        key = (conn.session_id, conn.target_lang)
        peers = self._channels.get(key)
        if peers is not None:
            peers.discard(conn.ws)
            if not peers:
                del self._channels[key]

    def _cancel_keepalive(self, conn: _Conn) -> None:
        task = conn.keepalive_task
        # 자기 자신을 취소하면 뒤따르는 close 가 중간에 잘린다 — keepalive 가
        # 스스로 소켓을 버리는 경로(_drop_conn)가 이 함수를 지난다.
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _drop_conn(
        self,
        conn: _Conn,
        *,
        close_code: int | None = None,
        close_reason: str | None = None,
        detected_by: str,
    ) -> None:
        """소켓 1개를 명부에서 내리고 순단을 연다. 세션과 다른 소켓은 그대로."""
        async with self._lock:
            if self._conns.get(conn.ws) is not conn:
                # 이미 정리됨 — 중복 통지(끊김 통지 + keepalive)는 여기서 멎는다.
                return
            self._forget_conn_locked(conn)
            self._open_blip_locked(conn, close_code, close_reason, detected_by)
            metrics.set_listeners(len(self._conns))
        self._cancel_keepalive(conn)
        # 기본 1000 그대로 — 여기서 CLOSE_SESSION_ENDED 를 쓰면 안 된다. 소켓
        # 하나가 keepalive 로 떨어졌을 뿐 세션은 살아 있고, 그 청취자는 재접속을
        # **해야 하는** 쪽이다. 만료 코드를 주면 정확히 반대로 재시도를 멈춘다.
        await self._safe_close(conn)
        print('hub: listener dropped', conn.client_id,
              'session:', conn.session_id, 'by:', detected_by)

    async def handle_client_disconnect(
        self,
        session_id: str,
        ws: WebSocket,
        close_code: int | None = None,
        close_reason: str | None = None,
        detected_by: str = "client_disconnect",
    ) -> None:
        """/ws 엔드포인트가 소켓 하나를 잃었을 때 호출 (§4-3 원인 1).

        detach 와 달리 **세션을 끝내지 않는다** — 나머지 청취자는 계속 듣는다.
        주인 검사로 늦게 도착한 통지(이미 정리된 소켓)는 무시한다.
        close_code/reason 은 클라 close frame 값(1001=정상 종료, 1006=비정상
        단절)으로 ws_blips 에 기록된다 (§4-7).

        ``detected_by`` 는 ws_blips 행에 그대로 실린다. /ws 의 일반 예외 경로도
        이 함수를 쓰므로(정상 close frame 이 아니다) 그쪽은 다른 값을 넘긴다.
        """
        conn = self._conns.get(ws)
        if conn is None or conn.session_id != session_id:
            print('hub: disconnect notice ignored (stale)', session_id)
            return
        await self._drop_conn(
            conn,
            close_code=close_code,
            close_reason=close_reason,
            detected_by=detected_by,
        )

    # --- 브로드캐스트 ------------------------------------------------------

    async def broadcast_to_session(
        self,
        session_id: str,
        payload: Dict[str, Any],
        *,
        target_lang: str | None = None,
    ) -> None:
        raw_text = payload.get('sentence')
        if raw_text is None:
            print('hub: skip send, sentence is None')
            return

        text = str(raw_text)

        async with self._lock:
            # 라이브가 아닌 세션의 번역은 stale → drop (에러B). D3 으로 백로그가
            # 없어졌으므로 큐잉 없이 버린다. 이 규약을 완화하면 다른 세션의
            # 번역이 새 청취자에게 새는 교차 전송이 된다.
            if session_id not in self._sessions:
                print('hub: drop stale broadcast', session_id,
                      'live=', sorted(self._sessions))
                return

            lang = self._channel_lang_locked(session_id, target_lang)
            targets = [
                self._conns[ws]
                for ws in self._channels.get((session_id, lang), ())
                if ws in self._conns and self._is_connected(ws)
            ]
            if not targets:
                # 아무도 못 받은 문장은 이 세션의 열린 순단들이 놓친 문장이다.
                self._count_lost_locked(session_id)

        if not targets:
            print('hub: no connected listener', session_id, lang)
            return

        # 소켓마다 별개 태스크 → 별개 게이트. 느린 청취자가 다른 청취자의
        # 자막을 막지 못한다 (§7 head-of-line blocking 방지). 참조는 허브가
        # 든다 — 여기서 GC 된 태스크는 그 청취자의 자막 1건 유실이다.
        for conn in targets:
            self._spawn(self._send_text(conn, text))

    def _channel_lang_locked(
        self, session_id: str, target_lang: str | None
    ) -> str:
        """이 브로드캐스트가 실릴 언어 채널을 고른다. (_lock 보유 전제)

        P1 은 세션당 채널이 1개다. upstream 이 넘긴 ``target_lang`` 이 그 채널
        이름과 어긋나면(설정 표기 차이 등) 자막이 통째로 사라지므로, 채널이
        하나뿐일 때는 그 하나로 보낸다. 채널이 2개 이상인 P2 부터는 이 폴백이
        스스로 꺼지고 정확 일치만 남는다.
        """
        lang = _norm_lang(target_lang)
        if (session_id, lang) in self._channels:
            return lang
        channels = [l for (s, l) in self._channels if s == session_id]
        if len(channels) == 1:
            if lang != channels[0]:
                print('hub: lang fallback', lang, '->', channels[0])
            return channels[0]
        return lang

    async def send_to_socket(self, ws: WebSocket, text: str) -> None:
        """소켓 하나에만 보낸다 (/ws 접속 인사).

        브로드캐스트로 보내면 이미 듣고 있던 청취자 전원의 화면에 남의 접속
        인사가 자막으로 뜬다 — 소켓이 1개일 때는 없던 문제다.
        """
        conn = self._conns.get(ws)
        if conn is None:
            return
        await self._send_text(conn, text)

    async def _send_text(self, conn: _Conn, text: str) -> None:
        try:
            async with conn.send_gate:
                # 게이트 대기 중 close 가 일어났을 수 있으므로 send 직전 재확인
                # (에러A TOCTOU). D3 으로 재적재 경로가 없어져 여기서 버린다.
                if not self._is_connected(conn.ws):
                    print('hub: drop send, socket closed', conn.client_id)
                    return
                await conn.ws.send_text(text)
            metrics.record_broadcast(time.time())
            print('hub: broadcast:', text)

        except Exception as e:
            metrics.record_send_failed()
            print('hub: send failed:', conn.client_id, e)
            # 연결이 끊어진 경우에만 명부에서 내린다 — 일시적 실패로 살아 있는
            # 청취자를 쫓아내면 안 된다.
            if not self._is_connected(conn.ws):
                await self._drop_conn(conn, detected_by="send_failed")

    async def _safe_close(self, conn: _Conn, *, close_code: int = 1000) -> None:
        # REV-4(에러A): close 도 send 와 같은 게이트로 직렬화한다. starlette 의
        # send_text/send_json/close 는 모두 같은 ASGI send 채널을 쓰므로,
        # _send_text 가 send 를 await 하는 '도중' 게이트 밖에서 close 가 끼어들면
        # 'send after websocket.close'(에러A)가 난다.
        #
        # 락 순서 불변식: '_lock 이 send_gate 를 감쌀 수는 있어도 그 반대는 금지'.
        # 게이트가 소켓별이 된 지금도 규칙은 같다 (Semaphore 비재진입).
        try:
            async with conn.send_gate:
                if conn.ws.application_state == WebSocketState.CONNECTED:
                    await conn.ws.close(code=close_code)
        except Exception:
            pass

    # --- 순단(blip) 계측 ---------------------------------------------------

    def _open_blip_locked(
        self,
        conn: _Conn,
        close_code: int | None,
        close_reason: str | None,
        detected_by: str,
    ) -> None:
        # fire-and-forget insert — DB 가 죽어도 /ws 경로를 막지 않는다
        # (recorder 가 예외를 삼킴). D5: 소켓별 1행이라 세션당 N행이 될 수 있고,
        # client_id 가 그 행들을 구분한다. (_lock 보유 전제)
        if self._blip_recorder is None:
            return
        task = asyncio.create_task(
            self._blip_recorder.record_disconnect(
                conn.session_id,
                client_id=conn.client_id,
                close_code=close_code,
                close_reason=close_reason,
                detected_by=detected_by,
            )
        )
        self._open_blips.setdefault(conn.session_id, deque()).append(
            _OpenBlip(task, conn.client_id)
        )

    def _match_open_blip_locked(
        self, session_id: str, client_id: str
    ) -> _OpenBlip | None:
        """재접속한 소켓이 닫을 순단을 고른다. (_lock 보유 전제)

        client_id 가 일치하면 그것 — 앱이 ``/ws?clientId=`` 를 보내기 시작하면
        (P2) 코드 변경 없이 정확 매칭이 된다.

        P1 에서는 앱이 안 보내 서버가 접속마다 새 id 를 발급하므로 정확 매칭이
        성립하지 않는다. 그래서 같은 세션의 **가장 오래된** 열린 순단을 복귀로
        본다 (사용자 결정 2026-08-08). 청취자가 여럿이면 A 의 순단이 B 의
        접속으로 닫히는 오귀속이 가능하다 — 정확도보다 '순단 이력이 전부
        미복귀로 남지 않는 것'을 택한 결과다.
        """
        queue = self._open_blips.get(session_id)
        if not queue:
            return None
        matched: _OpenBlip | None = None
        for i, blip in enumerate(queue):
            if blip.client_id == client_id:
                del queue[i]
                matched = blip
                break
        if matched is None:
            matched = queue.popleft()
        if not queue:
            del self._open_blips[session_id]
        return matched

    def _release_blips_locked(self, session_id: str) -> list[_OpenBlip]:
        """세션 종료로 열린 순단들을 미복귀 확정하고 슬롯을 반납한다.

        ``reconnected_at`` 은 NULL 로 남긴다 — 잔존 NULL = 미복귀가 §4-7 결정 2
        이고 모니터도 그 규약으로 '미복귀' 뱃지를 그린다. (_lock 보유 전제)
        """
        queue = self._open_blips.pop(session_id, None)
        return list(queue) if queue else []

    def _count_lost_locked(self, session_id: str) -> None:
        """이 세션의 열린 순단들에 '놓친 문장 1건' 을 적는다. (_lock 보유 전제)

        D3 으로 백로그가 사라져 순단 중 자막은 되돌려지지 않는다. 유실을 세지
        않으면 순단 행이 '얼마나 손해였나' 를 못 말한다.
        """
        for blip in self._open_blips.get(session_id, ()):
            blip.lost += 1

    async def _abandon_blip(self, blip: _OpenBlip) -> None:
        # insert 완료를 기다린다 — 세션 종료가 insert 보다 먼저 올 수 있다.
        try:
            blip_id = await blip.task
            if blip_id is not None and self._blip_recorder is not None:
                await self._blip_recorder.record_abandon(
                    blip_id, lost_count=blip.lost
                )
        except Exception as e:
            print('hub: blip abandon failed (ignored):', repr(e))

    async def _finish_blip(self, blip: _OpenBlip) -> None:
        # 빠른 재접속이 insert 완료보다 먼저 올 수 있으므로 start 태스크의
        # id 를 await 로 기다렸다가 종결한다 (레이스를 대기로 직렬화).
        # flushed_count 는 D3 이후 항상 0 이다 — 방류할 백로그가 없다.
        try:
            blip_id = await blip.task
            if blip_id is not None and self._blip_recorder is not None:
                await self._blip_recorder.record_reconnect(
                    blip_id, flushed_count=0, lost_count=blip.lost
                )
        except Exception as e:
            print('hub: blip close failed (ignored):', repr(e))

    # --- keepalive ---------------------------------------------------------

    async def _send_ping(self, conn: _Conn) -> bool:
        """ping 전송 (클라이언트는 자동으로 pong 응답해야 함)."""
        try:
            async with conn.send_gate:
                if not self._is_connected(conn.ws):
                    return False
                await conn.ws.send_json({"type": "ping"})
            return True
        except Exception as e:
            msg = str(e)
            # 연결이 이미 종료된 뒤 ping 을 보내려 하면 starlette 가 RuntimeError.
            if isinstance(e, RuntimeError) and (
                "websocket.send" in msg or "close" in msg.lower()
            ):
                print('ping skipped: websocket already closed')
                return False
            print(f'ping send error: {e}')
            return False

    async def _keepalive_loop(self, conn: _Conn) -> None:
        """소켓 1개를 살피는 루프. 소켓마다 하나씩 돈다.

        P1 이전의 '재연결 300초 대기' 는 사라졌다 — 백로그(D3)가 없어져
        붙잡고 있을 상태가 없고, 재접속은 그냥 새 소켓으로 등재된다.
        """
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_SECONDS)

                if not self._is_connected(conn.ws):
                    await self._drop_conn(conn, detected_by="keepalive_timeout")
                    return

                now = time.time()
                if conn.first_pong_received and conn.last_pong_time > 0:
                    # 60초 이상 pong 이 없으면 half-open 으로 보고 버린다.
                    idle = now - conn.last_pong_time
                    if idle > _PONG_TIMEOUT_SECONDS:
                        print(f'keepalive: no pong for {idle:.1f}s, dropping',
                              conn.client_id)
                        await self._drop_conn(
                            conn, detected_by="keepalive_timeout"
                        )
                        return
                elif conn.first_ping_sent_time > 0:
                    # REV-3: 첫 ping 을 보냈는데 첫 pong 이 한 번도 안 옴.
                    # 처음부터 pong 을 못 보내는 클라가 붙으면 서버가 ping 만
                    # 무한 전송하게 되므로 같은 기준으로 끊는다.
                    idle = now - conn.first_ping_sent_time
                    if idle > _PONG_TIMEOUT_SECONDS:
                        print(f'keepalive: no first pong for {idle:.1f}s, dropping',
                              conn.client_id)
                        await self._drop_conn(
                            conn, detected_by="keepalive_timeout"
                        )
                        return

                if not await self._send_ping(conn):
                    await self._drop_conn(conn, detected_by="keepalive_timeout")
                    return
                if conn.first_ping_sent_time == 0:
                    conn.first_ping_sent_time = time.time()
                print('keepalive: sent ping', conn.client_id)
        except asyncio.CancelledError:
            print('keepalive: cancelled', conn.client_id)
        except Exception as e:
            print(f'keepalive: unexpected error {e}', conn.client_id)

    async def on_pong(self, ws: Optional[WebSocket] = None) -> None:
        """클라이언트로부터 pong(또는 임의의 프레임)을 받으면 호출.

        ``ws`` 는 어느 청취자가 살아있음을 알린 것인지 가른다 — 소켓이 N개인
        지금 이게 없으면 한 명의 pong 이 전원을 살아있는 것으로 만든다.
        """
        if ws is None:
            return
        conn = self._conns.get(ws)
        if conn is None:
            return
        conn.last_pong_time = time.time()
        if not conn.first_pong_received:
            conn.first_pong_received = True
            print('keepalive: first pong received!', conn.client_id)
