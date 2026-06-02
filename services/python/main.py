import asyncio
import contextlib
import json
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, Field

from src.compose import build
from src.config import get_nats_config, get_deepl_config, get_ws_url
from src.database.pool import Db
from src.deepL.deepL import DeeplTranslationService
from src.pushClient.pusher import Pusher
from src.repository.implementation.translation_repository import (
    ensure_session,
    end_session,
)
from src.separator.kss_separator import SentenceSeparator
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

        deepl_api = app.state.deepl_config['deepl_api_key']

        hub = WebSocketHub()
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

        for task in (getattr(app.state, "consumer_task", None), getattr(app.state, "separator_task", None)):
            if task and not task.done():
                task.cancel()

        for task in (getattr(app.state, "consumer_task", None), getattr(app.state, "separator_task", None)):
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


app = FastAPI(title='neemba-python', lifespan=lifespan)

request_count = Counter('neemba_requests_total', 'Total number of requests')


def get_db_pool(request: Request):
    """DI helper: access the asyncpg pool from routes.

    Usage: ``pool = Depends(get_db_pool)`` or ``request.app.state.db_pool``.
    Actual queries are wired in Phase 4.
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


@app.post('/internal/sessions/start', response_model=StartResponse)
async def start_session(req: StartRequest, request: Request):
    base_ws_url = request.app.state.get_ws_config['ws_url']
    print('base url', base_ws_url)
    webSocket_url = f"{base_ws_url}?sessionId={req.session_id}"
    print('ws url', webSocket_url)

    # Create the session row up front so ended_at always has a target on stop.
    # Isolated: a DB hiccup must not fail the session start signal.
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            await ensure_session(pool, req.session_id, req.source_lang, req.target_lang)
        except Exception as e:
            print("start_session: ensure_session failed (ignored):", repr(e))

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

    hub: WebSocketHub = request.app.state.hub
    await hub.detach()

    # Only the first (transitioning) stop emits the monitor close event.
    monitor_hub: MonitorHub = getattr(request.app.state, "monitor_hub", None)
    if ended and monitor_hub is not None:
        await monitor_hub.close_session(req.session_id, {
            "type": "session_closed",
            "sessionId": req.session_id,
            "translationCount": translation_count,
        })

    return {"ok": True, "ended": ended, "translationCount": translation_count}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    print('websocket')

    hub: WebSocketHub = ws.app.state.hub

    print(">>> hub at endpoint:", id(hub), "ws:", id(ws))

    await hub.attach(ws)

    try:
        await hub.broadcast_to_session({
            "sentence": "Connect!",
            "isFinal": True
        })

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
                await hub.on_pong()
                continue
            # ping 타입: 클라이언트가 보내면 pong으로 응답
            if message_type == "ping":
                await ws.send_json({"type": "pong"})
                await hub.on_pong()
                continue
            # 다른 메시지도 활동으로 간주해 keepalive 갱신
            await hub.on_pong()

            # 다른 메시지 타입은 여기서 처리 (현재는 없음)
            # 실제 데이터 메시지는 여기서 처리됨

    except WebSocketDisconnect:
        print('main : websocket disconnected')
        pass
    except Exception as e:
        print(f'main : websocket error: {e}')
        await hub.detach()


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
