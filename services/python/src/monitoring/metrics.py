"""모니터링 사이드카가 폴링하는 도메인 메트릭.

별도 모듈인 이유: websocket.py(hub)와 consumer.py 양쪽이 쓰는데, 서로를
import 하면 순환이 생긴다. 기본 레지스트리에 등록하므로 main.py 의
/metrics(generate_latest)에 자동 노출된다 — nginx 미노출(컨테이너 내부 전용),
사이드카가 compose 네트워크에서 python:8000/metrics 로 읽는다.
"""
from prometheus_client import Counter, Gauge

_active_session = Gauge(
    'neemba_hub_active_session',
    '1 while a translation session occupies the hub slot',
)
_last_broadcast = Gauge(
    'neemba_hub_last_broadcast_timestamp_seconds',
    'Wall-clock time of the last translation delivered to the client',
)
_send_failed = Counter(
    'neemba_hub_send_failed_total',
    'WebSocket send failures (old error-A signature)',
)
_nats_connected = Gauge(
    'neemba_nats_connected',
    '1 while the transcript consumer holds a NATS connection',
)
_unparseable = Counter(
    'neemba_consumer_unparseable_total',
    'NATS messages dropped as unparseable (term-ed)',
)


def set_active_session(active: bool) -> None:
    _active_session.set(1 if active else 0)


def record_broadcast(timestamp: float) -> None:
    _last_broadcast.set(timestamp)


def record_send_failed() -> None:
    _send_failed.inc()


def set_nats_connected(connected: bool) -> None:
    _nats_connected.set(1 if connected else 0)


def record_unparseable() -> None:
    _unparseable.inc()
