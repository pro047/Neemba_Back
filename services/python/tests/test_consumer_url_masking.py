"""NATS URL credential masking for logs.

Regression for the reconnect log leaking the plaintext password:
`NATS reconnected to nats://user:<password>@nats:4222` (found during the
2026-07-12 develop runtime verification, manual failure-survival scenario).
"""
from src.consumer.consumer import TranscriptConsumer, strip_credentials


class _StubSeparator:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def offer(self, event) -> None: ...


def _make_consumer(url: str) -> TranscriptConsumer:
    return TranscriptConsumer(
        nats_url=url,
        nats_subject='transcript.session.*',
        stream_name='TEST_STREAM',
        consumer_name='test-worker',
        separator=_StubSeparator(),
    )


def test_자격증명이_포함된_url이면_비밀번호를_제거해야_한다():
    # Arrange
    url = 'nats://neemba:supersecret@nats:4222'

    # Act
    masked = strip_credentials(url)

    # Assert
    assert masked == 'nats://nats:4222'


def test_자격증명이_없는_url이면_그대로_반환해야_한다():
    # Arrange
    url = 'nats://nats:4222'

    # Act
    masked = strip_credentials(url)

    # Assert
    assert masked == 'nats://nats:4222'


def test_비밀번호에_골뱅이가_있어도_전부_제거해야_한다():
    # Arrange — '@' inside the password must not survive the split
    url = 'nats://neemba:p@ssw@rd@nats:4222'

    # Act
    masked = strip_credentials(url)

    # Assert
    assert masked == 'nats://nats:4222'


def test_컨슈머를_생성하면_safe_url에_자격증명이_없어야_한다():
    # Arrange & Act
    consumer = _make_consumer('nats://neemba:supersecret@nats:4222')

    # Assert
    assert 'supersecret' not in consumer.safe_url
