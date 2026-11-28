"""Listener event alerts to a Discord webhook (docs/prd/listener-event-alert.md).

The hub only calls the synchronous ``notify``, which puts the text on an
in-memory queue. A single background worker (``run``) drains the queue and
POSTs, so a slow or failing webhook can never stall attach/drop/detach.

This module must not import ``websocket.py`` — the hub imports us.
"""
import asyncio
import os
from typing import Literal, Protocol

import httpx

DropKind = Literal["blip", "app_closed", "app_closed_normal", "other"]

MESSAGE_PREFIX = "[neemba] "
CONTENT_LIMIT = 1900  # Discord caps content at 2000; keep headroom
QUEUE_MAX = 100
RETRY_AFTER_CAP_SECONDS = 5.0
POST_TIMEOUT_SECONDS = 5.0
USER_AGENT = "neemba-hub/1.0"  # default httpx UA gets a Cloudflare 403 (see monitor.py)


def classify_drop(close_code: int | None, detected_by: str) -> DropKind:
    """Classify one socket drop. First match wins; no input returns None."""
    if detected_by in ("keepalive_timeout", "send_failed"):
        return "blip"
    if detected_by == "client_disconnect":
        if close_code in (1006, 1001):
            return "blip"
        if close_code == 1005:
            return "app_closed"
        if close_code == 1000:
            return "app_closed_normal"
    return "other"


def short_id(value: str) -> str:
    return value[:8]


def format_duration(seconds: float) -> str:
    # Same notation as infra/monitor/monitor.py _fmt_duration (floor, minutes only when needed).
    minutes = int(seconds // 60)
    return f"{minutes}분 {int(seconds % 60)}초" if minutes else f"{int(seconds)}초"


def format_attach(*, session_id: str, client_id: str, listeners: int) -> str:
    return (f"🟢 청취자 접속 — 세션 {short_id(session_id)} · "
            f"기기 {short_id(client_id)} · 현재 {listeners}명")


def format_reconnect(*, session_id: str, client_id: str, dropped_client_id: str,
                     duration_s: float, lost_count: int, listeners: int) -> str:
    return (f"🟢 복귀 — 세션 {short_id(session_id)} · 기기 {short_id(client_id)} "
            f"(끊긴 소켓 {short_id(dropped_client_id)}) · "
            f"끊긴 시간 {format_duration(duration_s)} · 유실 {lost_count}건 · "
            f"현재 {listeners}명")


def format_drop(kind: DropKind, *, session_id: str, client_id: str,
                close_code: int | None, detected_by: str, listeners: int) -> str:
    # Deliberately no IP / User-Agent / close_reason: reason is client-controlled
    # text and the PRD forbids personal data in the channel.
    sid, cid = short_id(session_id), short_id(client_id)
    code = "없음" if close_code is None else str(close_code)
    if kind == "blip":
        if detected_by == "keepalive_timeout":
            cause = "keepalive 무응답"
        elif detected_by == "send_failed":
            cause = "전송 실패"
        else:
            cause = f"code={code}"
        return f"🔴 순단 — 세션 {sid} · 기기 {cid} · {cause} · 남은 {listeners}명"
    if kind == "app_closed":
        return (f"🟠 앱이 닫음 (정지·화면 닫기·앱 재연결 구분 불가) — 세션 {sid} · "
                f"기기 {cid} · code={code} · 남은 {listeners}명")
    if kind == "app_closed_normal":
        return (f"🟠 앱이 닫음 (code=1000) — 세션 {sid} · 기기 {cid} · "
                f"남은 {listeners}명")
    return (f"⚪ 기타 끊김 — 세션 {sid} · 기기 {cid} · "
            f"code={code}/{detected_by} · 남은 {listeners}명")


def format_session_end(*, session_id: str, listeners: int) -> str:
    return f"🔚 세션 종료 — 세션 {short_id(session_id)} · 청취자 {listeners}명 연결 종료"


class ListenerAlertSink(Protocol):
    def notify(self, text: str) -> None: ...  # sync, non-blocking, never raises


def webhook_url_from_env() -> str | None:
    url = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    # The URL embeds the secret token — log only whether it is set.
    if url:
        print("listener-alert: webhook configured")
        return url
    print("listener-alert: log-only")
    return None


def _split_chunks(lines: list[str]) -> list[str]:
    """Join lines under MESSAGE_PREFIX into contents of <= CONTENT_LIMIT chars.

    Splits on line boundaries so no alert is cut in half; a single line that
    is itself too long is truncated rather than dropped.
    """
    room = CONTENT_LIMIT - len(MESSAGE_PREFIX)
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line = line[:room]
        extra = len(line) + (1 if current else 0)
        if current and size + extra > room:
            chunks.append(MESSAGE_PREFIX + "\n".join(current))
            current, size, extra = [], 0, len(line)
        current.append(line)
        size += extra
    if current:
        chunks.append(MESSAGE_PREFIX + "\n".join(current))
    return chunks


class DiscordListenerAlerter:
    def __init__(self, webhook_url: str | None, *,
                 client: httpx.AsyncClient | None = None,
                 queue_max: int = QUEUE_MAX) -> None:
        self._webhook_url = webhook_url
        self._client = client
        self._owns_client = False
        # asyncio.Queue binds no loop at construction (3.10+), so notify works
        # from sync code and before the worker starts.
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_max)
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def notify(self, text: str) -> None:
        try:
            print("listener-alert:", text)
            if self._webhook_url is None:
                return
            try:
                self._queue.put_nowait(text)
            except asyncio.QueueFull:
                self._dropped += 1
                print("listener-alert: queue full, dropped", self._dropped)
        except Exception as e:
            print("listener-alert: notify failed (ignored):", type(e).__name__)

    def _make_client(self, transport: httpx.AsyncBaseTransport | None = None
                     ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=POST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    async def run(self) -> None:
        if self._webhook_url is None:
            return
        if self._client is None:
            self._client = self._make_client()
            self._owns_client = True
        while True:
            lines = [await self._queue.get()]
            # No time window (D4): whatever piled up while the previous POST
            # was in flight goes out together.
            while True:
                try:
                    lines.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for content in _split_chunks(lines):
                await self._post(content)

    async def _post(self, content: str) -> None:
        """POST one chunk; retry once on 429. Never raises (except cancellation)."""
        assert self._client is not None and self._webhook_url is not None
        for attempt in range(2):
            try:
                resp = await self._client.post(
                    self._webhook_url, json={"content": content})
            except httpx.HTTPError as e:
                # Log the type only — messages can echo the URL (secret token).
                print("listener-alert: post failed:", type(e).__name__)
                return
            except Exception as e:
                print("listener-alert: post error:", type(e).__name__)
                return
            if resp.status_code == 429 and attempt == 0:
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                await asyncio.sleep(min(max(wait, 0.0), RETRY_AFTER_CAP_SECONDS))
                continue
            if not resp.is_success:
                print("listener-alert: post status", resp.status_code)
            return

    async def aclose(self) -> None:
        # Only close a client we built; an injected one belongs to the caller.
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False
