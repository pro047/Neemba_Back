"""모니터링 사이드카가 폴링할 도메인 메트릭 (감시 계획 §1).

/metrics 는 기본 레지스트리 generate_latest 를 쓰므로(main.py) 이 모듈의
메트릭은 등록만으로 노출된다. hub/consumer 배선의 실동작은 dev 라이브
검증에서 확인하고, 여기서는 메트릭 갱신 함수와 consumer 통합 지점을 본다.
"""
import json
import uuid

from prometheus_client import REGISTRY

from src.monitoring import metrics
from src.consumer.consumer import TranscriptConsumer


def sample(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


def test_active_session_gauge_follows_attach_detach_state():
    metrics.set_active_session(True)
    assert sample('neemba_hub_active_session') == 1.0
    metrics.set_active_session(False)
    assert sample('neemba_hub_active_session') == 0.0


def test_broadcast_records_wall_clock_timestamp():
    metrics.record_broadcast(1_700_000_000.0)
    assert sample('neemba_hub_last_broadcast_timestamp_seconds') == 1_700_000_000.0


def test_send_failed_counter_increments():
    before = sample('neemba_hub_send_failed_total') or 0.0
    metrics.record_send_failed()
    metrics.record_send_failed()
    assert sample('neemba_hub_send_failed_total') == before + 2.0


def test_nats_connected_gauge_toggles():
    metrics.set_nats_connected(False)
    assert sample('neemba_nats_connected') == 0.0
    metrics.set_nats_connected(True)
    assert sample('neemba_nats_connected') == 1.0


SESSION_LISTENERS = 'neemba_hub_session_listeners'
UNDELIVERED = 'neemba_hub_undelivered_total'


def labelled(name: str, session_id: str) -> float | None:
    return REGISTRY.get_sample_value(name, {'sessionId': session_id})


def _sid(tag: str) -> str:
    # 전역 레지스트리라 테스트마다 고유 sessionId 를 써야 값이 안 섞인다
    return f'{tag}-{uuid.uuid4().hex}'


# AC-21
def test_session_series_open_set_remove_lifecycle():
    # Arrange
    sid = _sid('m-life')

    # Act / Assert — 열기
    metrics.open_session_series(sid, 0)
    assert labelled(SESSION_LISTENERS, sid) == 0.0
    assert labelled(UNDELIVERED, sid) == 0.0

    # Act / Assert — 갱신
    metrics.set_session_listeners(sid, 3)
    assert labelled(SESSION_LISTENERS, sid) == 3.0

    # Act / Assert — 삭제, 두 번째 삭제도 예외 없음
    metrics.remove_session_series(sid)
    assert labelled(SESSION_LISTENERS, sid) is None
    assert labelled(UNDELIVERED, sid) is None
    metrics.remove_session_series(sid)


def test_remove_session_series_for_never_opened_session_is_silent():
    # Arrange
    sid = _sid('m-never')

    # Act
    metrics.remove_session_series(sid)

    # Assert
    assert labelled(SESSION_LISTENERS, sid) is None


def test_open_session_series_keeps_existing_undelivered_count():
    # Arrange
    sid = _sid('m-reopen')
    metrics.open_session_series(sid, 0)
    metrics.record_undelivered(sid)

    # Act
    metrics.open_session_series(sid, 2)

    # Assert
    assert labelled(UNDELIVERED, sid) == 1.0
    assert labelled(SESSION_LISTENERS, sid) == 2.0
    metrics.remove_session_series(sid)


# AC-22
def test_undelivered_counter_is_independent_per_session():
    # Arrange
    a, b = _sid('m-a'), _sid('m-b')
    before_a = labelled(UNDELIVERED, a) or 0.0
    before_b = labelled(UNDELIVERED, b) or 0.0

    # Act
    metrics.record_undelivered(a)
    metrics.record_undelivered(a)
    metrics.record_undelivered(b)

    # Assert
    assert labelled(UNDELIVERED, a) == before_a + 2.0
    assert labelled(UNDELIVERED, b) == before_b + 1.0
    metrics.remove_session_series(a)
    metrics.remove_session_series(b)


def test_session_series_does_not_touch_global_listener_gauge():
    # Arrange — 전역 neemba_hub_listeners 는 Grafana perf.json 이 쓴다
    sid = _sid('m-global')
    metrics.set_listeners(7)

    # Act
    metrics.open_session_series(sid, 0)
    metrics.set_session_listeners(sid, 2)
    metrics.remove_session_series(sid)

    # Assert
    assert sample('neemba_hub_listeners') == 7.0


class _NullSeparator:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def offer(self, event) -> None: ...


class _FakeMsg:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.termed = False

    async def ack(self) -> None: ...
    async def nak(self) -> None: ...

    async def term(self) -> None:
        self.termed = True


async def test_unparseable_message_increments_counter():
    consumer = TranscriptConsumer(
        nats_url='nats://x', nats_subject='s', stream_name='st',
        consumer_name='c', separator=_NullSeparator(),
    )
    before = sample('neemba_consumer_unparseable_total') or 0.0

    await consumer._handle_message(_FakeMsg(b'not-json'))

    assert sample('neemba_consumer_unparseable_total') == before + 1.0


async def test_parseable_message_does_not_touch_unparseable_counter():
    consumer = TranscriptConsumer(
        nats_url='nats://x', nats_subject='s', stream_name='st',
        consumer_name='c', separator=_NullSeparator(),
    )
    before = sample('neemba_consumer_unparseable_total') or 0.0
    ok = json.dumps({
        'sessionId': 's1', 'segmentId': 1, 'sequence': 1,
        'transcriptText': 't', 'targetLanguage': 'en-US',
        'sourceLanguage': 'ko-KR',
    }).encode('utf-8')

    await consumer._handle_message(_FakeMsg(ok))

    assert sample('neemba_consumer_unparseable_total') == before
