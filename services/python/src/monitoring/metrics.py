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
_ensure_session_failed = Counter(
    'neemba_ensure_session_failed_total',
    'Failed ensure_session attempts (session row upsert), counted per attempt',
)
_status_db_failed = Counter(
    'neemba_monitor_status_db_failed_total',
    'DB query failures on GET /api/monitor/status (degraded to activeSessions:null)',
)

# WU5(GET /api/monitor/status): prometheus_client 레지스트리는 앱 코드용 읽기
# API 가 없어서, 게이지 setter 가 모듈 수준 스냅샷에도 병기한다. setter 시그니처
# 불변(hub·consumer 공유 모듈) — 추가 비용은 dict 대입뿐.
_snapshot: dict = {
    'active_session': False,
    'nats_connected': False,
    'last_broadcast_ts': None,  # time.time() epoch seconds, None = 브로드캐스트 이력 없음
}


def get_snapshot() -> dict:
    """Point-in-time copy of the gauge values for the status endpoint."""
    return dict(_snapshot)


def set_active_session(active: bool) -> None:
    _active_session.set(1 if active else 0)
    _snapshot['active_session'] = active


def record_broadcast(timestamp: float) -> None:
    _last_broadcast.set(timestamp)
    _snapshot['last_broadcast_ts'] = timestamp


def record_send_failed() -> None:
    _send_failed.inc()


def set_nats_connected(connected: bool) -> None:
    _nats_connected.set(1 if connected else 0)
    _snapshot['nats_connected'] = connected


def record_unparseable() -> None:
    _unparseable.inc()


def record_ensure_session_failed() -> None:
    _ensure_session_failed.inc()


def record_status_db_failed() -> None:
    # 상태 개요가 DB 없이 열화 응답한 횟수. 화면은 칩 하나로 조용히 넘어가므로
    # 이 카운터가 없으면 사람이 브라우저를 열기 전까지 열화를 아무도 모른다.
    _status_db_failed.inc()
