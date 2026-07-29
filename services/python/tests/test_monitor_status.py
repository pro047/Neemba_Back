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

import pytest

from src.consumer import consumer as consumer_module
from src.monitor import node_metrics
from src.monitoring import metrics
from src.repository.implementation import monitor_query_repository as mq
from src.repository.implementation.translation_repository import (
    end_session,
    ensure_session,
)

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
