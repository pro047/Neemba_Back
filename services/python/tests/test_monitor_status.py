"""WU5 (monitor-page-v2): ``GET /api/monitor/status`` 를 받치는 단위들.

라우트 통합 동작(칩 표시·nodeUp 전환)은 dev compose 라이브 검증으로 확인하고,
여기서는 세 단위를 본다:

- metrics 스냅샷: setter 가 Prometheus 게이지와 함께 모듈 스냅샷에도 기록
  (레지스트리는 앱 코드용 읽기 API 가 없어 스냅샷이 status 의 데이터 소스).
- node /metrics 파서: 고정 Prometheus 텍스트에서 대상 게이지 3개만 추출.
- ``count_active_sessions``: 실 DB(conftest ``pg_pool``)에서 ended_at IS NULL
  집계가 세션 목록의 live 정의와 일치.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
from src.consumer import consumer as consumer_module
from src.monitor import node_metrics
from src.monitoring import metrics
from src.repository.implementation import monitor_query_repository as mq
from src.repository.implementation.translation_repository import (
    end_session,
    ensure_session,
)


async def _attach(hub, ws, session_id: str, **kwargs):
    """라이브 세션에 청취자를 붙인다 — 프로덕션의 start → /ws 순서 그대로.

    ``attach`` 는 화이트리스트라 ``register_session`` 되지 않은 sessionId 를
    거절한다. 청취자 수를 세려면 붙을 세션이 먼저 라이브여야 한다.
    """
    await hub.register_session(session_id, kwargs.get("target_lang"))
    return await hub.attach(ws, session_id, **kwargs)


@pytest.fixture(autouse=True)
def _restore_metrics_snapshot():
    # 이 파일의 테스트들은 모듈 전역 게이지 스냅샷을 건드린다. 되돌리지 않으면
    # 뒤에 오는 테스트가 기본값을 전제할 때 파일 전체 실행에서만 깨진다.
    # last_broadcast_ts 는 None 으로 되돌릴 setter 가 없어 값이 있을 때만 복원한다.
    before = metrics.get_snapshot()
    yield
    metrics.set_active_session(bool(before["active_session"]))
    metrics.set_nats_connected(bool(before["nats_connected"]))
    if before["last_broadcast_ts"] is not None:
        metrics.record_broadcast(before["last_broadcast_ts"])


# --- metrics snapshot ---------------------------------------------------------


def test_snapshot_reflects_setters_when_called():
    metrics.set_active_session(True)
    metrics.set_nats_connected(True)
    metrics.record_broadcast(1_700_000_000.0)

    snap = metrics.get_snapshot()

    assert snap["active_session"] is True
    assert snap["nats_connected"] is True
    assert snap["last_broadcast_ts"] == 1_700_000_000.0

    metrics.set_active_session(False)
    metrics.set_nats_connected(False)

    snap = metrics.get_snapshot()
    assert snap["active_session"] is False
    assert snap["nats_connected"] is False


def test_snapshot_returns_copy_when_mutated():
    # 반환 dict 를 호출자가 오염시켜도 내부 상태가 바뀌면 안 된다.
    snap = metrics.get_snapshot()
    snap["nats_connected"] = "tampered"
    assert metrics.get_snapshot()["nats_connected"] != "tampered"


# --- node /metrics parser -------------------------------------------------------

_FIXED_METRICS_TEXT = """\
# HELP neemba_stt_paused 1 while STT is paused
# TYPE neemba_stt_paused gauge
neemba_stt_paused 1
neemba_rtmp_auth_enabled 0
neemba_publish_buffer_size 42
neemba_other_counter_total 7
malformed_line_without_value
neemba_stt_paused_not_target 1
"""


def test_parse_gauges_extracts_only_targets_when_given_prometheus_text():
    values = node_metrics.parse_gauges(_FIXED_METRICS_TEXT)

    assert values == {
        "neemba_stt_paused": 1.0,
        "neemba_rtmp_auth_enabled": 0.0,
        "neemba_publish_buffer_size": 42.0,
    }


def test_parse_gauges_skips_unparseable_value_when_present():
    values = node_metrics.parse_gauges("neemba_stt_paused not-a-number\n")
    assert values == {}


def test_parse_gauges_returns_empty_when_targets_absent():
    # node 는 응답하지만 대상 게이지가 없으면 빈 dict (라우트가 null 필드로 표시).
    values = node_metrics.parse_gauges("neemba_other 1\n")
    assert values == {}


# --- sessionId 라벨드 시리즈 집계 (게이지 라벨화 계획 §2) ---------------------

_LABELLED_METRICS_TEXT = """\
# TYPE neemba_stt_paused gauge
neemba_stt_paused{sessionId="aaa"} 0
neemba_stt_paused{sessionId="bbb"} 1
# TYPE neemba_publish_buffer_size gauge
neemba_publish_buffer_size{sessionId="aaa"} 3
neemba_publish_buffer_size{sessionId="bbb"} 7
neemba_rtmp_auth_enabled 1
"""


def test_라벨드_시리즈는_max로_집계해야_한다():
    # stt_paused 는 any-of(하나라도 1이면 1), buffer_size 는 세션 중 최댓값 —
    # 둘 다 max 로 환원된다. 세션 B 의 0 이 A 의 1 을 덮으면 안 된다.
    values = node_metrics.parse_gauges(_LABELLED_METRICS_TEXT)

    assert values == {
        'neemba_stt_paused': 1.0,
        'neemba_publish_buffer_size': 7.0,
        'neemba_rtmp_auth_enabled': 1.0,
    }


def test_게이지_패밀리는_노출됐는데_시리즈가_없으면_0이어야_한다():
    # 라벨드 게이지는 세션이 없으면 시리즈가 0줄이다. # TYPE 라인이 있으면
    # 게이지 자체는 존재하는 것이므로 '값 없음(null)'이 아니라 0 — 세션이
    # 없다는 뜻이지 게이지가 없는 게 아니다.
    text = ("# TYPE neemba_stt_paused gauge\n"
            "# TYPE neemba_publish_buffer_size gauge\n")

    values = node_metrics.parse_gauges(text)

    assert values == {
        'neemba_stt_paused': 0.0,
        'neemba_publish_buffer_size': 0.0,
    }


def test_비유한_샘플이_섞이면_집계값도_비유한값이어야_한다():
    # max 는 NaN 을 삼킨다 — max(0.0, nan) == 0.0 이라 '알 수 없음'이
    # 자신 있는 '정상 0' 으로 둔갑한다(TYPE 시딩과 겹치면 항상 발생).
    # 하나라도 비유한값이면 이름째 nan 으로 고정해 라우트가 null 을 내게 한다.
    text = ('# TYPE neemba_stt_paused gauge\n'
            'neemba_stt_paused{sessionId="aaa"} NaN\n')

    values = node_metrics.parse_gauges(text)

    assert node_metrics.gauge_bool(values['neemba_stt_paused']) is None


def test_비유한_샘플_뒤에_정상_샘플이_와도_집계값은_비유한값이어야_한다():
    # 라인 순서에 따라 결과가 달라지면 안 된다: max(nan, 1.0) 은 구현에 따라
    # 1.0 을 돌려줘 NaN 이 조용히 사라진다.
    text = ('# TYPE neemba_stt_paused gauge\n'
            'neemba_stt_paused{sessionId="aaa"} NaN\n'
            'neemba_stt_paused{sessionId="bbb"} 1\n')

    values = node_metrics.parse_gauges(text)

    assert node_metrics.gauge_bool(values['neemba_stt_paused']) is None


def test_라벨_없는_게이지는_TYPE만_있을_때_시딩하지_않아야_한다():
    # 라벨 없는 게이지는 TYPE 다음에 샘플이 반드시 온다. 잘린 응답에서까지
    # 0.0 을 깔면 '미상' 이어야 할 rtmp 인증 상태를 '꺼짐' 으로 단정한다.
    text = '# TYPE neemba_rtmp_auth_enabled gauge\n'

    values = node_metrics.parse_gauges(text)

    assert values == {}


def test_비유한_게이지값이면_None을_반환해야_한다():
    # Prometheus 텍스트 형식은 NaN/+Inf 를 허용하고 prom-client 도 그대로
    # 내보낸다. 라우트가 맨 int() 를 쓰면 여기서 500 이 나므로, 변환은 반드시
    # 이 헬퍼를 거쳐 '알 수 없음'(None) 으로 열화돼야 한다.
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert node_metrics.gauge_int(bad) is None
        assert node_metrics.gauge_bool(bad) is None

    assert node_metrics.gauge_int(42.0) == 42
    assert node_metrics.gauge_bool(1.0) is True
    assert node_metrics.gauge_bool(0.0) is False
    assert node_metrics.gauge_int(None) is None
    assert node_metrics.gauge_bool(None) is None


async def test_fetch_node_gauges_returns_none_when_node_unreachable():
    # 닫힌 포트로 즉시 connection refused — 모든 실패는 None (nodeUp:false).
    result = await node_metrics.fetch_node_gauges("http://127.0.0.1:59999/metrics")
    assert result is None


async def test_DB_조회가_실패해도_나머지_상태_필드는_응답해야_한다(pg_pool, monkeypatch):
    # DB 가 죽은 순간이야말로 NATS·자막기기·node 칩을 봐야 하는 순간이다.
    # 라우트가 500 을 내면 프런트는 상태 바 전체를 "상태 조회 실패" 칩 하나로
    # 덮어버린다 — node 실패를 nodeUp:false 로만 열화시키는 정책과 같아야 한다.
    #
    # 저장소 함수를 모킹하지 않고 실 pool 을 닫아서 장애를 만든다 — 그래야
    # asyncpg 가 실제로 던지는 예외 타입으로 검증되고, 나중에 except 절을
    # 좁히는 변경이 있으면 이 테스트가 잡는다.
    async def _node_unreachable(*_args, **_kwargs):
        return None

    class _Request:
        app = SimpleNamespace(state=SimpleNamespace(hub=None))

    monkeypatch.setattr(main, "fetch_node_gauges", _node_unreachable)
    metrics.set_nats_connected(True)
    await pg_pool.close()

    res = await main.monitor_status(_Request(), pool=pg_pool)

    assert res.active_sessions is None       # 이 필드만 열화
    assert res.nats_connected is True        # 나머지는 그대로 응답
    assert res.ws_client_connected is False
    assert res.node_up is False


async def test_붙어있는_청취자_수가_상태_개요에_나와야_한다(monkeypatch):
    # P1: 세션당 소켓이 N개가 됐다. wsClientConnected 는 1명이든 5명이든 true 라
    # '기기 2대 중 1대가 빠졌다' 를 못 본다 — 8/9 검증 1번(기기 2대 동시 수신)의
    # 판정 근거가 이 숫자다. pool=None 으로 DB 경로는 열화시켜 격리한다.
    from starlette.websockets import WebSocketState

    from src.ws.websocket import WebSocketHub

    class _FakeWS:
        def __init__(self) -> None:
            self.client_state = WebSocketState.CONNECTED
            self.application_state = WebSocketState.CONNECTING

        async def accept(self) -> None:
            self.application_state = WebSocketState.CONNECTED

        async def send_json(self, _data) -> None: ...
        async def close(self, code: int = 1000) -> None:
            self.application_state = WebSocketState.DISCONNECTED

    async def _node_unreachable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "fetch_node_gauges", _node_unreachable)

    hub = WebSocketHub()
    ws1, ws2 = _FakeWS(), _FakeWS()
    await _attach(hub, ws1, "s1")
    await _attach(hub, ws2, "s1")

    class _Request:
        app = SimpleNamespace(state=SimpleNamespace(hub=hub))

    try:
        res = await main.monitor_status(_Request(), pool=None)

        assert res.listeners == 2
        assert res.ws_client_connected is True

        # 한 명이 빠지면 wsClientConnected 는 그대로 true 이고 숫자만 준다 —
        # 이 구분이 없으면 이탈이 보이지 않는다.
        await hub.handle_client_disconnect("s1", ws1)
        res = await main.monitor_status(_Request(), pool=None)
        assert res.listeners == 1
        assert res.ws_client_connected is True
    finally:
        await hub.detach("s1")


async def test_JetStream_준비가_실패하면_NATS_연결_플래그가_False여야_한다(monkeypatch):
    # TCP 연결은 성공한 뒤 스트림/구독 준비에서 터지는 경우 — 연결 자체는
    # 멀쩡하니 nats-py 의 disconnected/closed 콜백이 영영 안 뜬다. 플래그를
    # 내려주지 않으면 소비가 0건인 채로 상태 개요가 초록으로 거짓말한다.
    class _JetStreamDown(RuntimeError):
        """JetStream 준비 단계에서 터지는 상황을 흉내내는 테스트용 예외."""

    class _JetStream:
        async def stream_info(self, _name):
            raise _JetStreamDown

    class _Client:
        def jetstream(self):
            return _JetStream()

    async def _fake_connect(*_args, **_kwargs):
        return _Client()

    class _NoopSeparator:
        async def start(self) -> None: ...
        async def stop(self) -> None: ...
        async def offer(self, event) -> None: ...

    monkeypatch.setattr(consumer_module.nats, "connect", _fake_connect)
    metrics.set_nats_connected(True)  # 직전 성공 상태를 흉내

    consumer = consumer_module.TranscriptConsumer(
        nats_url="nats://stub:4222",
        nats_subject="stub.subject",
        stream_name="STUB_STREAM",
        consumer_name="stub-worker",
        separator=_NoopSeparator(),
    )

    with pytest.raises(_JetStreamDown):
        await consumer.connect()

    assert metrics.get_snapshot()["nats_connected"] is False


def test_node_metrics_url_prefers_env_when_set(monkeypatch):
    monkeypatch.setenv("NODE_METRICS_URL", "http://example.internal:9/metrics")
    assert node_metrics.node_metrics_url() == "http://example.internal:9/metrics"
    monkeypatch.delenv("NODE_METRICS_URL")
    assert node_metrics.node_metrics_url() == node_metrics.DEFAULT_NODE_METRICS_URL


# --- count_active_sessions (real DB) -----------------------------------------


async def test_count_active_sessions_counts_only_null_ended_at(pg_pool):
    assert await mq.count_active_sessions(pg_pool) == 0

    await ensure_session(pg_pool, "wu5-live-1", "ko-KR", "en-US")
    await ensure_session(pg_pool, "wu5-live-2", "ko-KR", "en-US")
    await ensure_session(pg_pool, "wu5-ended-1", "ko-KR", "en-US")
    await end_session(pg_pool, "wu5-ended-1")

    assert await mq.count_active_sessions(pg_pool) == 2

    await end_session(pg_pool, "wu5-live-2")
    assert await mq.count_active_sessions(pg_pool) == 1
