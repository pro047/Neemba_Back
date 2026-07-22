"""모니터링 사이드카가 폴링할 도메인 메트릭 (감시 계획 §1).

/metrics 는 기본 레지스트리 generate_latest 를 쓰므로(main.py) 이 모듈의
메트릭은 등록만으로 노출된다. hub/consumer 배선의 실동작은 dev 라이브
검증에서 확인하고, 여기서는 메트릭 갱신 함수와 consumer 통합 지점을 본다.
"""
import json

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
