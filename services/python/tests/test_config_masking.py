"""P2: 비밀값이 기동 로그(stdout)에 평문으로 남지 않아야 한다.

require_env는 mask 옵션을 이미 지원하지만, DeepL 키·DB 비밀번호·NATS URL의
자격증명이 mask 없이 호출되어 docker logs에 평문 잔존하던 문제의 회귀 테스트.
"""
import pytest

from src.config import get_deepl_config, get_nats_config, get_postgres_config


@pytest.fixture
def postgres_env(monkeypatch):
    monkeypatch.setenv('POSTGRES_HOST', 'db-host')
    monkeypatch.setenv('POSTGRES_PORT', '5432')
    monkeypatch.setenv('POSTGRES_USER', 'neemba')
    monkeypatch.setenv('POSTGRES_PASSWORD', 'pw-secret-9876')
    monkeypatch.setenv('POSTGRES_DATABASE', 'neemba_monitor')


@pytest.fixture
def nats_env(monkeypatch):
    monkeypatch.setenv('NATS_URL', 'nats://neemba:nats-pw-5555@nats:4222')
    monkeypatch.setenv('NATS_SUBJECT', 'transcript.session.*')
    monkeypatch.setenv('NATS_STREAM_NAME', 'transcripts')
    monkeypatch.setenv('NATS_CONSUMER_NAME', 'durable')


def test_deepl_설정을_읽으면_api_키가_로그에_평문으로_찍히지_않아야_한다(monkeypatch, capsys):
    monkeypatch.setenv('DEEPL_API_KEY', 'deepl-secret-key-1234')

    get_deepl_config()

    assert 'deepl-secret-key-1234' not in capsys.readouterr().out


def test_deepl_설정을_읽으면_마스킹되어도_값_자체는_반환해야_한다(monkeypatch):
    monkeypatch.setenv('DEEPL_API_KEY', 'deepl-secret-key-1234')

    config = get_deepl_config()

    assert config['deepl_api_key'] == 'deepl-secret-key-1234'


def test_postgres_설정을_읽으면_비밀번호가_로그에_평문으로_찍히지_않아야_한다(postgres_env, capsys):
    get_postgres_config()

    assert 'pw-secret-9876' not in capsys.readouterr().out


def test_postgres_설정을_읽으면_호스트는_로그에_보여야_한다(postgres_env, capsys):
    # 디버깅 가능성 유지: 비밀이 아닌 값까지 가려지면 안 된다.
    get_postgres_config()

    assert 'db-host' in capsys.readouterr().out


def test_nats_설정을_읽으면_url_안의_비밀번호가_로그에_찍히지_않아야_한다(nats_env, capsys):
    get_nats_config()

    assert 'nats-pw-5555' not in capsys.readouterr().out
