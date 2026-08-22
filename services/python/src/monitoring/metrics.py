"""모니터링 사이드카가 폴링하는 도메인 메트릭.

별도 모듈인 이유: websocket.py(hub)와 consumer.py 양쪽이 쓰는데, 서로를
import 하면 순환이 생긴다. 기본 레지스트리에 등록하므로 main.py 의
/metrics(generate_latest)에 자동 노출된다 — nginx 미노출(컨테이너 내부 전용),
사이드카가 compose 네트워크에서 python:8000/metrics 로 읽는다.
"""
import asyncio

from prometheus_client import Counter, Gauge, Histogram

_active_session = Gauge(
    'neemba_hub_active_session',
    '1 while a translation session is live in the hub',
)
# P1: 세션당 소켓이 N개가 됐다. active_session 이 1 이어도 듣는 사람이 0명일
# 수 있고 그 둘은 다른 장애다 — 게이지를 나눠야 예배 중에 구분이 된다.
_listeners = Gauge(
    'neemba_hub_listeners',
    'Listener websockets currently attached to the hub',
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

# 성능 계기 3종 (perf-test-plan.md §5). node 는 collectDefaultMetrics 로
# nodejs_eventloop_lag_* 를 공짜로 내지만 python 에는 없고, 블로킹이 있는 곳은
# 정확히 python 이다 — DeepL 이 동기 호출인데 _push_loop 이 단일 태스크라,
# 번역 한 건이 도는 동안 허브 브로드캐스트·keepalive·NATS 소비가 함께 멈춘다.
#
# 버킷은 좁게 잡는다(시리즈 = 버킷 + inf + sum + count). lag 의 관심 영역은
# "정상 10ms 미만 / 블로킹 100ms 이상" 이고, 번역 왕복은 네트워크라 50ms 미만이
# 나올 일이 없다. 기본 버킷(14개)은 이 둘 다에 과하다.
_event_loop_lag = Histogram(
    'neemba_event_loop_lag_seconds',
    'Scheduling delay of the asyncio loop: actual sleep duration minus requested',
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
_translate_duration = Histogram(
    'neemba_translate_duration_seconds',
    'Wall-clock time of one blocking DeepL translate call',
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)
_sentence_queue_depth = Gauge(
    'neemba_sentence_queue_depth',
    'Split sentences waiting for translation in the separator push queue',
)

# 샘플러 주기. lag 은 "이 주기 동안 루프가 얼마나 밀렸나" 라서 주기보다 짧은
# 블로킹은 통계적으로만 잡힌다. 1초면 15초 스크레이프 간격당 15표본이다.
_LAG_SAMPLE_INTERVAL_SECONDS = 1.0

# WU5(GET /api/monitor/status): prometheus_client 레지스트리는 앱 코드용 읽기
# API 가 없어서, 게이지 setter 가 모듈 수준 스냅샷에도 병기한다. setter 시그니처
# 불변(hub·consumer 공유 모듈) — 추가 비용은 dict 대입뿐.
_snapshot: dict = {
    'active_session': False,
    'listeners': 0,
    'nats_connected': False,
    'last_broadcast_ts': None,  # time.time() epoch seconds, None = 브로드캐스트 이력 없음
}


def get_snapshot() -> dict:
    """Point-in-time copy of the gauge values for the status endpoint."""
    return dict(_snapshot)


def set_active_session(active: bool) -> None:
    _active_session.set(1 if active else 0)
    _snapshot['active_session'] = active


def set_listeners(count: int) -> None:
    _listeners.set(count)
    _snapshot['listeners'] = count


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


def observe_event_loop_lag(seconds: float) -> None:
    _event_loop_lag.observe(seconds)


def observe_translate_duration(seconds: float) -> None:
    _translate_duration.observe(seconds)


def set_sentence_queue_depth(depth: int) -> None:
    _sentence_queue_depth.set(depth)


async def sample_event_loop_lag(
    interval: float = _LAG_SAMPLE_INTERVAL_SECONDS,
) -> None:
    """Observe how late the loop wakes this task up, forever.

    A sleeping task can only be resumed once the loop regains control, so the
    overshoot past ``interval`` is exactly how long something else held the
    thread. Runs until cancelled (lifespan shutdown).
    """
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(interval)
        # Clamp: a loop that wakes early (clock granularity) is not negative lag.
        observe_event_loop_lag(max(0.0, loop.time() - started - interval))
