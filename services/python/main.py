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
from src.deepL.deepL import DeeplTranslationService
from src.pushClient.pusher import Pusher
from src.separator.kss_separator import SentenceSeparator
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

    try:
        app.state.nats_config = get_nats_config()
        app.state.deepl_config = get_deepl_config()
        app.state.get_ws_config = get_ws_url()

        deepl_api = app.state.deepl_config['deepl_api_key']

        hub = WebSocketHub()
        translator = DeeplTranslationService(deepl_api)
        pusher = Pusher(hub)

        separator = SentenceSeparator(
            translator=translator,
            pusher=pusher
        )

        app.state.hub = hub
        app.state.translator = translator
        app.state.pusher = pusher

        app.state.separator = separator

        def _log_task_result(task: asyncio.Task[None]) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception('background task crasehd', exc_info=True)

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
    return StartResponse(**{"sessionId": req.session_id, "webSocketUrl": webSocket_url})


@app.post('/internal/sessions/stop')
async def stop_session(req: StopRequest):
    print(f'sessionId: {req.session_id} stopped')

    hub: WebSocketHub = app.state.hub

    await hub.detach()
    return {"ok": True}


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

            # 다른 메시지 타입은 여기서 처리 (현재는 없음)
            # 실제 데이터 메시지는 여기서 처리됨

    except WebSocketDisconnect:
        print('main : websocket disconnected')
        pass
    except Exception as e:
        print(f'main : websocket error: {e}')
        await hub.detach()
