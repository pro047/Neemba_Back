import asyncio
import contextlib
import json
import logging
import time
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from src.compose import build
from src.config import get_nats_config, get_deepl_config, get_ws_url
from src.database.pool import Db
from src.deepL.deepL import DeeplTranslationService
from src.monitor.node_metrics import fetch_node_gauges, gauge_bool, gauge_int
from src.monitoring import metrics
from src.pushClient.pusher import Pusher
from src.repository.implementation import monitor_query_repository as mq
from src.repository.implementation.translation_repository import (
    close_stale_sessions,
    end_session,
    ensure_session_with_retry,
)
from src.repository.implementation import ws_blip_repository as wb
from src.separator.kss_separator import SentenceSeparator
from src.ws.blip_recorder import WsBlipRecorder
from src.ws.monitor import MonitorHub
from src.ws.websocket import WebSocketHub

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(">>> lifespan : entered")

    hub = None
    translator = None
    pusher = None
    separator = None
    consumer_task = None
    separator_task = None
    db = None

    try:
        app.state.nats_config = get_nats_config()
        app.state.deepl_config = get_deepl_config()
        app.state.get_ws_config = get_ws_url()

        # DB pool (asyncpg). get_postgres_config() uses require_env, so
        # missing POSTGRES_* env vars fail fast here — same policy as
        # the NATS/DeepL configs above. Actual queries land in Phase 4.
        db = Db()
        app.state.db = db
        app.state.db_pool = await db.create_pool()
        print(">>> lifespan : db pool created")

        # D4-a (monitor-page-v2): sessions left ended_at IS NULL are ghosts of
        # the previous process (lost stop / crash) — stamp them ended so the
        # monitor list shows no phantom LIVE badge. Isolated: a DB hiccup here
        # must not block startup.
        try:
            stale = await close_stale_sessions(app.state.db_pool)
            if stale:
                print(f">>> lifespan : closed {len(stale)} stale session(s): {stale}")
        except Exception as e:
            print("lifespan: close_stale_sessions failed (ignored):", repr(e))

        deepl_api = app.state.deepl_config['deepl_api_key']

        # §4-7 순단 계측: /ws 끊김·재접속을 ws_blips 에 기록 (DB 실패는 recorder 가 격리)
        hub = WebSocketHub(blip_recorder=WsBlipRecorder(app.state.db_pool))
        monitor_hub = MonitorHub()
        translator = DeeplTranslationService(deepl_api)
        pusher = Pusher(hub, monitor_hub=monitor_hub, db_pool=app.state.db_pool)

        separator = SentenceSeparator(
            translator=translator,
            pusher=pusher
        )

        app.state.hub = hub
        app.state.monitor_hub = monitor_hub
        app.state.translator = translator
        app.state.pusher = pusher

        app.state.separator = separator

        def _log_task_result(task: asyncio.Task[None]) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                print("background task crashed:", repr(task.exception()))
                traceback.print_exc()

        app.state.consumer_task = asyncio.create_task(
            build(
                app.state.hub,
                app.state.separator,
                app.state.nats_config)
        )
        app.state.consumer_task.add_done_callback(_log_task_result)

        app.state.separator_task = asyncio.create_task(
            app.state.separator.start()
        )
        app.state.separator_task.add_done_callback(_log_task_result)

        # perf-test-plan.md §5: python 에는 node 의 nodejs_eventloop_lag_* 에
        # 해당하는 지표가 없다. 이 태스크가 유일한 관측 수단이므로 consumer·
        # separator 와 같은 수명(lifespan)에 묶고 아래 finally 에서 함께 취소한다.
        app.state.loop_lag_task = asyncio.create_task(
            metrics.sample_event_loop_lag()
        )
        app.state.loop_lag_task.add_done_callback(_log_task_result)

        logger.info(">>> lifespan : init done")
        print(f"--------ws : {app.state.get_ws_config} -------")
        print(">>> hub at lifespan:", id(app.state.hub))
        yield

    except Exception:
        logger.exception("lifespan init failed:\n%s", traceback.format_exc())
        raise

    finally:
        if getattr(app.state, "separator", None):
            with contextlib.suppress(Exception):
                await app.state.separator.stop()

        background_tasks = tuple(
            getattr(app.state, name, None)
            for name in ("consumer_task", "separator_task", "loop_lag_task")
        )

        for task in background_tasks:
            if task and not task.done():
                task.cancel()

        for task in background_tasks:
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if getattr(app.state, "db", None):
            with contextlib.suppress(Exception):
                await app.state.db.close()

        logger.info(">>> lifespan : cleanup done")


class StartRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    source_lang: str = Field(alias="sourceLang")
    target_lang: str = Field(alias="targetLang")


class StartResponse(BaseModel):
    session_id: str = Field(alias="sessionId")
    webSocket_url: str = Field(alias="webSocketUrl")


class StopRequest(BaseModel):
    session_id: str = Field(alias="sessionId")


# --- monitor history API (Phase 5) response models -------------------------
# Field names are snake_case but serialized as camelCase (alias) to match the
# existing API style (StartResponse etc.). datetime fields serialize to ISO8601
# automatically. populate_by_name lets us build straight from asyncpg records
# (snake_case keys).


class MonitorSession(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    started_at: datetime = Field(alias="startedAt")
    ended_at: datetime | None = Field(default=None, alias="endedAt")
    source_lang: str | None = Field(default=None, alias="sourceLang")
    target_lang: str | None = Field(default=None, alias="targetLang")
    translation_count: int = Field(alias="translationCount")
    # MAX(translations.created_at) — WU2: STALE 배지·미니 통계용. 번역 0건이면 NULL.
    last_translation_at: datetime | None = Field(
        default=None, alias="lastTranslationAt"
    )
    # ended_at IS NULL ⇒ still running (docs §7 Phase 5: 라이브/종료 구분).
    live: bool


class SessionListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: list[MonitorSession]
    limit: int
    offset: int
    next_offset: int | None = Field(default=None, alias="nextOffset")


class TranslationPair(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    segment_id: int | None = Field(default=None, alias="segmentId")
    sequence: int | None = None
    source_text: str = Field(alias="sourceText")
    translated_text: str = Field(alias="translatedText")
    source_lang: str | None = Field(default=None, alias="sourceLang")
    target_lang: str | None = Field(default=None, alias="targetLang")
    confidence: float | None = None
    created_at: datetime = Field(alias="createdAt")


class SessionTranslationsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: list[TranslationPair]
    limit: int
    next_cursor: int | None = Field(default=None, alias="nextCursor")


class TranslationSearchItem(TranslationPair):
    # Search spans sessions, so each row carries its session id.
    session_id: str = Field(alias="sessionId")


class TranslationSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: list[TranslationSearchItem]
    limit: int
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class WsBlip(BaseModel):
    # §4-7 순단 계측. reconnected_at NULL = 미복귀. close_code/close_reason 은
    # 클라 close frame 값(서버발 감지면 NULL), detected_by 로 감지 주체 구분.
    model_config = ConfigDict(populate_by_name=True)

    id: int
    session_id: str = Field(alias="sessionId")
    # P1 D5: 세션당 소켓이 N개라 session_id 만으로는 행의 주인을 못 가린다.
    # 이 컬럼 이전에 기록된 행은 NULL.
    client_id: str | None = Field(default=None, alias="clientId")
    disconnected_at: datetime = Field(alias="disconnectedAt")
    reconnected_at: datetime | None = Field(default=None, alias="reconnectedAt")
    duration_ms: int | None = Field(default=None, alias="durationMs")
    flushed_count: int | None = Field(default=None, alias="flushedCount")
    lost_count: int | None = Field(default=None, alias="lostCount")
    close_code: int | None = Field(default=None, alias="closeCode")
    close_reason: str | None = Field(default=None, alias="closeReason")
    detected_by: str = Field(alias="detectedBy")


class WsBlipListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: list[WsBlip]
    limit: int
    # (disconnected_at, id) 키셋 토큰. offset 이었을 때는 보는 사이에 새 순단이
    # 앞에 끼면 페이지 경계가 밀려 행이 중복·누락됐다.
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class NodeStatus(BaseModel):
    # WU5: node /metrics 게이지 스냅샷. 게이지가 응답에 없으면 None
    # (node 는 떠 있으나 해당 게이지 미노출).
    model_config = ConfigDict(populate_by_name=True)

    stt_paused: bool | None = Field(default=None, alias="sttPaused")
    rtmp_auth_enabled: bool | None = Field(default=None, alias="rtmpAuthEnabled")
    publish_buffer_size: int | None = Field(default=None, alias="publishBufferSize")


class MonitorStatusResponse(BaseModel):
    # WU5 시스템 상태 개요. 판정(경보) 없이 현재값 표시만 — 판정은 사이드카 책임.
    model_config = ConfigDict(populate_by_name=True)

    # None = DB 조회 실패로 이 필드만 열화 (나머지 칩은 그대로 응답).
    # nullable 이지만 default 는 두지 않는다 — 라우트가 이 필드를 빠뜨리면
    # 조용히 null 이 나가는 대신 ValidationError 로 즉시 드러나야 한다.
    active_sessions: int | None = Field(alias="activeSessions")
    ws_client_connected: bool = Field(alias="wsClientConnected")
    # P1: 지금 붙어 있는 청취자 소켓 수. wsClientConnected 는 1명이든 5명이든
    # true 라 '2대 중 1대가 빠졌다' 를 못 본다. default 0 = 허브 미기동.
    listeners: int = 0
    nats_connected: bool = Field(alias="natsConnected")
    # None = 이 프로세스에서 브로드캐스트 이력 없음 (기동 직후 등).
    last_broadcast_ago_sec: float | None = Field(
        default=None, alias="lastBroadcastAgoSec"
    )
    node_up: bool = Field(alias="nodeUp")
    node: NodeStatus | None = None


app = FastAPI(title='neemba-python', lifespan=lifespan)

request_count = Counter('neemba_requests_total', 'Total number of requests')

# 상태 개요의 DB 조회 데드라인. 프런트 폴링 주기(10s)보다 넉넉히 짧아야
# 요청이 겹쳐 쌓이지 않는다. node 수집 상한(3s)과 같은 자릿수로 맞췄다.
_STATUS_DB_TIMEOUT_SECONDS = 3.0


def get_db_pool(request: Request):
    """DI helper: access the asyncpg pool from routes.

    Usage: ``pool = Depends(get_db_pool)`` or ``request.app.state.db_pool``.
    Used by the Phase 4 capture path and the Phase 5 history API.
    """
    return request.app.state.db_pool


@app.get("/ping")
def pong():
    return {'message': 'pong'}


@app.get("/health")
def healthz():
    return {'status': 'ok'}


@app.get('/metrics')
def get_metrics():
    request_count.inc()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _parse_dt(value: str | None, field: str) -> datetime | None:
    """Parse an ISO8601 query param into a datetime, or 422 on bad input."""
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail=f"invalid {field}: expected ISO8601 datetime"
        ) from e


@app.get('/api/monitor/sessions', response_model=SessionListResponse)
async def monitor_sessions(
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
    pool=Depends(get_db_pool),
):
    """Session list, newest-first (started_at DESC), OFFSET-paginated."""
    limit = mq.clamp_limit(
        limit, default=mq.SESSIONS_LIMIT_DEFAULT, maximum=mq.SESSIONS_LIMIT_MAX
    )
    offset = mq.clamp_offset(offset)
    rows, next_offset = await mq.list_sessions(pool, limit=limit, offset=offset)
    items = [
        MonitorSession(**dict(r), live=r["ended_at"] is None) for r in rows
    ]
    return SessionListResponse(
        items=items, limit=limit, offset=offset, next_offset=next_offset
    )


@app.get(
    '/api/monitor/sessions/{session_id}/translations',
    response_model=SessionTranslationsResponse,
)
async def monitor_session_translations(
    session_id: str,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    pool=Depends(get_db_pool),
):
    """One session's source↔translation pairs, keyset-paginated on PK id."""
    limit = mq.clamp_limit(
        limit,
        default=mq.SESSION_TRANSLATIONS_LIMIT_DEFAULT,
        maximum=mq.SESSION_TRANSLATIONS_LIMIT_MAX,
    )
    try:
        cur = mq.parse_int_cursor(cursor)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="invalid cursor") from e

    rows, next_cursor = await mq.list_session_translations(
        pool, session_id, limit=limit, cursor=cur
    )
    items = [TranslationPair(**dict(r)) for r in rows]
    return SessionTranslationsResponse(
        items=items, limit=limit, next_cursor=next_cursor
    )


@app.get('/api/monitor/translations', response_model=TranslationSearchResponse)
async def monitor_translations_search(
    lang: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    pool=Depends(get_db_pool),
):
    """Search pairs by language / created_at range / masked keyword.

    ``lang`` matches **either** source_lang or target_lang. ``from``/``to`` are
    the inclusive created_at range. ``q`` is an ILIKE substring match over the
    already-masked text. Keyset-paginated on (created_at DESC, id DESC).
    """
    limit = mq.clamp_limit(
        limit, default=mq.SEARCH_LIMIT_DEFAULT, maximum=mq.SEARCH_LIMIT_MAX
    )
    dt_from = _parse_dt(from_, "from")
    dt_to = _parse_dt(to, "to")
    try:
        cur = mq.decode_search_cursor(cursor)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="invalid cursor") from e

    rows, next_cursor = await mq.search_translations(
        pool,
        lang=lang,
        dt_from=dt_from,
        dt_to=dt_to,
        q=q,
        cursor=cur,
        limit=limit,
    )
    items = [TranslationSearchItem(**dict(r)) for r in rows]
    return TranslationSearchResponse(
        items=items, limit=limit, next_cursor=next_cursor
    )


@app.get('/api/monitor/ws-blips', response_model=WsBlipListResponse)
async def monitor_ws_blips(
    session_id: str | None = Query(default=None, alias="sessionId"),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    pool=Depends(get_db_pool),
):
    """/ws 순단(blip) 이력, disconnected_at 최신순, (disconnected_at, id) 키셋 (§4-7).

    커서는 translations 검색과 같은 opaque 토큰 규약이다 — 클라이언트는 받은
    문자열을 그대로 되돌려주기만 하고, 깨진 토큰은 422 다(조용히 1페이지로
    되감으면 사용자는 목록이 왜 처음으로 돌아갔는지 알 길이 없다).
    """
    limit = mq.clamp_limit(
        limit, default=wb.BLIPS_LIMIT_DEFAULT, maximum=wb.BLIPS_LIMIT_MAX
    )
    try:
        cur = mq.decode_search_cursor(cursor)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="invalid cursor") from e

    rows, next_cursor = await wb.list_blips(
        pool, session_id=session_id, limit=limit, cursor=cur
    )
    items = [WsBlip(**dict(r)) for r in rows]
    return WsBlipListResponse(items=items, limit=limit, nextCursor=next_cursor)


@app.get('/api/monitor/status', response_model=MonitorStatusResponse)
async def monitor_status(request: Request, pool=Depends(get_db_pool)):
    """시스템 상태 개요 (WU5): python 게이지 + DB 집계 + node /metrics 통합.

    node 수집 실패는 ``nodeUp:false, node:null`` 로 표시하고 나머지 필드는
    정상 응답한다 — node 장애가 상태 개요 전체를 막지 않는다 (D 결정).
    DB 조회 실패도 같은 정책: ``activeSessions:null`` 로만 열화시킨다.
    """
    # DB 가 죽은 순간이야말로 NATS·자막기기·node 칩을 봐야 하는 순간이다.
    # 여기서 예외를 올려보내면 라우트가 500 이 되고 프런트는 상태 바 전체를
    # "상태 조회 실패" 칩 하나로 덮어버린다 — 위 docstring 의 열화 원칙과 모순.
    #
    # 데드라인이 필요한 이유: pool 에 command_timeout 이 없어(database/pool.py)
    # DB 가 accept 만 하고 응답을 멈추면 예외가 아니라 '매달림'이 된다. 그러면
    # try/except 는 발동하지 않고 nginx proxy_read_timeout(30s)이 504 를 내
    # 결국 상태 바 전멸 — 예외 경로만 막아서는 이 결함이 안 닫힌다.
    # node 수집(node_metrics.fetch_node_gauges)과 같은 방식이다.
    try:
        async with asyncio.timeout(_STATUS_DB_TIMEOUT_SECONDS):
            active: int | None = await mq.count_active_sessions(pool)
    except Exception:
        metrics.record_status_db_failed()
        logger.exception("monitor_status: count_active_sessions failed (degraded)")
        active = None

    hub: WebSocketHub | None = getattr(request.app.state, "hub", None)
    ws_connected = hub.is_client_connected() if hub is not None else False
    # P1: wsClientConnected 는 '한 명이라도 붙어 있나' 라서 2명이 1명이 된 것을
    # 못 잡는다. 예배 중 기기 이탈은 이 숫자로만 보인다.
    listeners = hub.listener_count() if hub is not None else 0

    snap = metrics.get_snapshot()
    last_ts = snap["last_broadcast_ts"]
    ago = max(0.0, time.time() - last_ts) if last_ts else None

    # gauge_bool/gauge_int 를 쓰는 이유: Prometheus 텍스트 형식은 NaN/+Inf 를
    # 허용하고 prom-client 도 그대로 내보낸다. 맨 int() 는 거기서 예외를 던져
    # node 게이지 하나 때문에 라우트 전체가 500 이 된다 — nodeUp:false 로
    # 열화시키려는 이 라우트의 설계와 정반대다.
    gauges = await fetch_node_gauges()
    node = None
    if gauges is not None:
        node = NodeStatus(
            sttPaused=gauge_bool(gauges.get('neemba_stt_paused')),
            rtmpAuthEnabled=gauge_bool(gauges.get('neemba_rtmp_auth_enabled')),
            publishBufferSize=gauge_int(gauges.get('neemba_publish_buffer_size')),
        )

    return MonitorStatusResponse(
        activeSessions=active,
        wsClientConnected=ws_connected,
        listeners=listeners,
        natsConnected=bool(snap["nats_connected"]),
        lastBroadcastAgoSec=ago,
        nodeUp=gauges is not None,
        node=node,
    )


@app.post('/internal/sessions/start', response_model=StartResponse)
async def start_session(req: StartRequest, request: Request):
    base_ws_url = request.app.state.get_ws_config['ws_url']
    print('base url', base_ws_url)
    webSocket_url = f"{base_ws_url}?sessionId={req.session_id}"
    print('ws url', webSocket_url)

    # P1: 세션의 언어 채널을 청취자보다 먼저 등록한다. 첫 소켓이 붙는 시점에
    # 채널이 정해져 있어야 브로드캐스트가 갈 곳을 안다 (D2).
    hub: WebSocketHub = request.app.state.hub
    await hub.register_session(req.session_id, req.target_lang)

    # Create the session row up front so ended_at always has a target on stop.
    # Retried (WU2) because a lost row here means the session may never appear
    # in the monitor list; still isolated — a DB outage must not fail the
    # session start signal (failures land on the ensure_session_failed metric).
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            await ensure_session_with_retry(
                pool, req.session_id, req.source_lang, req.target_lang
            )
        except Exception as e:
            print("start_session: ensure_session failed (ignored):", repr(e))

    # Global monitor event (WU1). Emitted regardless of the DB outcome above —
    # live visibility must not be hostage to a DB hiccup (D3 principle).
    # startedAt is server time by decision Q2-a, not the DB-recorded value.
    monitor_hub: MonitorHub | None = getattr(request.app.state, "monitor_hub", None)
    if monitor_hub is not None:
        try:
            await monitor_hub.broadcast_global({
                "type": "session_started",
                "sessionId": req.session_id,
                "sourceLang": req.source_lang,
                "targetLang": req.target_lang,
                "startedAt": datetime.now(UTC).isoformat(),
            })
        except Exception as e:
            print("start_session: event broadcast failed (ignored):", repr(e))

    return StartResponse(**{"sessionId": req.session_id, "webSocketUrl": webSocket_url})


@app.post('/internal/sessions/stop')
async def stop_session(req: StopRequest, request: Request):
    print(f'sessionId: {req.session_id} stopped')

    # Idempotent end: ended_at is stamped once, the monitor close event is
    # emitted once. A duplicate stop (docs §3 weakness 1) is a no-op. The DB
    # call is isolated so a failure never blocks client teardown below.
    ended = False
    translation_count = 0
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            ended, translation_count = await end_session(pool, req.session_id)
        except Exception as e:
            print("stop_session: end_session failed (ignored):", repr(e))

    # Flush the session's buffered tail before tearing anything down so the
    # capture path (DB + monitor) still records it. Isolated like the rest of
    # the stop path: a separator failure must not block the stop.
    separator = getattr(request.app.state, "separator", None)
    if separator is not None:
        try:
            await separator.close_session(req.session_id)
        except Exception as e:
            print("stop_session: separator flush failed (ignored):", repr(e))

    hub: WebSocketHub = request.app.state.hub
    # Session-aware detach: a stop for a stale session cannot close the
    # socket owned by the currently live session.
    await hub.detach(req.session_id)

    # Only the first (transitioning) stop emits the monitor close event and
    # the global session_ended event (WU1) — a duplicate stop emits nothing.
    monitor_hub: MonitorHub | None = getattr(request.app.state, "monitor_hub", None)
    if ended and monitor_hub is not None:
        await monitor_hub.close_session(req.session_id, {
            "type": "session_closed",
            "sessionId": req.session_id,
            "translationCount": translation_count,
        })
        try:
            await monitor_hub.broadcast_global({
                "type": "session_ended",
                "sessionId": req.session_id,
                "translationCount": translation_count,
                "endedAt": datetime.now(UTC).isoformat(),
            })
        except Exception as e:
            print("stop_session: event broadcast failed (ignored):", repr(e))

    return {"ok": True, "ended": ended, "translationCount": translation_count}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    print('websocket')

    hub: WebSocketHub = ws.app.state.hub

    # wss://.../ws?sessionId={id} 의 쿼리로 붙을 세션을 고른다.
    # sessionId 없는 접속은 라우팅 대상이 없으므로 거절(handshake close).
    session_id = ws.query_params.get("sessionId")
    if not session_id:
        print('main : websocket rejected, missing sessionId')
        await ws.close(code=1008)
        return

    # P1: 청취자 여러 명이 같은 sessionId 로 붙는다. 소켓을 구분할 값이 필요한데
    # (D5 순단 행의 주인) 앱은 아직 아무것도 안 보내므로 서버가 발급한다.
    # clientId/lang 쿼리는 앱이 보내기 시작하면(P2) 코드 변경 없이 쓰이는 훅이다.
    client_id = await hub.attach(
        ws,
        session_id,
        target_lang=ws.query_params.get("lang"),
        client_id=ws.query_params.get("clientId"),
    )
    if client_id is None:
        # 라이브가 아닌 세션 — 허브가 이미 4404 로 닫았다. 여기서 등재를
        # 허용하면 종료된 세션이 재접속만으로 되살아난다.
        return

    print(">>> hub at endpoint:", id(hub), "ws:", id(ws),
          "session:", session_id, "client:", client_id)

    try:
        # 접속 인사는 이 소켓에만. 브로드캐스트로 보내면 이미 듣고 있던
        # 청취자 전원의 화면에 남의 접속 인사가 자막으로 뜬다.
        await hub.send_to_socket(ws, "Connect!")

        while True:
            raw_text = await ws.receive_text()

            message_type = None
            if raw_text:
                try:
                    decoded = json.loads(raw_text)
                except json.JSONDecodeError:
                    if raw_text.strip().lower() == "pong":
                        message_type = "pong"
                else:
                    if isinstance(decoded, dict):
                        message_type = decoded.get("type")

            # pong 타입: 클라이언트가 주기적으로 보내는 pong (keepalive)
            if message_type == "pong":
                await hub.on_pong(ws)
                continue
            # ping 타입: 클라이언트가 보내면 pong으로 응답
            if message_type == "ping":
                await ws.send_json({"type": "pong"})
                await hub.on_pong(ws)
                continue
            # 다른 메시지도 활동으로 간주해 keepalive 갱신
            await hub.on_pong(ws)

            # 다른 메시지 타입은 여기서 처리 (현재는 없음)
            # 실제 데이터 메시지는 여기서 처리됨

    except WebSocketDisconnect as e:
        # §4-3(원인 1): hub 에 즉시 통지해 이 소켓만 명부에서 내린다. 통지가
        # 늦으면 다음 keepalive 틱(최대 30초)까지 죽은 소켓에 send 를 시도한다.
        # detach 를 쓰면 안 된다 — 그건 세션의 모든 소켓을 닫고 세션을 지운다.
        # §4-7: close code 가 원인 판별의 핵심 — 1001(클라 정상 종료: 절전/
        # 백그라운드) vs 1006(비정상 단절: 네트워크). ws_blips 로도 기록된다.
        print(f'main : websocket disconnected code={e.code} reason={e.reason!r}')
        await hub.handle_client_disconnect(
            session_id, ws, close_code=e.code, close_reason=e.reason or None
        )
    except Exception as e:
        # 여기도 '소켓 1개가 죽었다' 이지 '방송이 끝났다' 가 아니다. detach 를
        # 부르면 예외 하나에 그 세션의 청취자 전원이 끊기고 세션이 지워져,
        # 이후 번역이 전부 stale drop 된다 — P1 이 없애려던 증상 그대로다.
        # close frame 이 없으므로 code/reason 없이 열고 detected_by 로 구분한다.
        print(f'main : websocket error: {e}')
        await hub.handle_client_disconnect(
            session_id, ws, detected_by="ws_error"
        )


@app.websocket("/ws/monitor")
async def monitor_endpoint(ws: WebSocket):
    """Monitor dashboard stream: live masked source↔translation payloads.

    Subscribe with ``/ws/monitor?sessionId=<id>``. Receives every payload the
    capture path produces for that session plus a final ``session_closed``
    event when the session is stopped. Read-only: inbound frames are ignored.
    """
    session_id = ws.query_params.get("sessionId")
    monitor_hub: MonitorHub = ws.app.state.monitor_hub

    if not session_id:
        await ws.close(code=4000)
        return

    await monitor_hub.attach(session_id, ws)
    try:
        while True:
            # Monitors are read-only; just drain inbound frames to detect close.
            await ws.receive_text()
    except WebSocketDisconnect:
        print(f'monitor : websocket disconnected session={session_id}')
    except Exception as e:
        print(f'monitor : websocket error: {e}')
    finally:
        await monitor_hub.detach(session_id, ws)


@app.websocket("/ws/monitor/events")
async def monitor_events_endpoint(ws: WebSocket):
    """Global monitor event stream (WU1): session lifecycle fan-out.

    Session-agnostic: every subscriber receives ``session_started`` /
    ``session_ended`` events for all sessions. No backlog — events emitted
    while disconnected are lost (history comes from ``/api/monitor/sessions``).
    Read-only: inbound frames are ignored.
    """
    monitor_hub: MonitorHub = ws.app.state.monitor_hub
    await monitor_hub.attach_global(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        print('monitor-events : websocket disconnected')
    except Exception as e:
        print(f'monitor-events : websocket error: {e}')
    finally:
        await monitor_hub.detach_global(ws)
