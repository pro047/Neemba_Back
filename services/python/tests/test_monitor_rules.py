"""알림 사이드카 규칙 엔진(infra/monitor/monitor.py evaluate) 테스트.

사이드카는 앱 코드가 아니라 infra 스크립트지만, 규칙·dedup 로직은
순수 함수라 여기(CI python-test)에 편입해 회귀를 막는다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3] / 'infra' / 'monitor'))

from monitor import evaluate  # noqa: E402

T0 = 1_800_000_000.0


def samples(**over):
    base = {
        'neemba_hub_active_session': 1.0,
        'neemba_hub_last_broadcast_timestamp_seconds': T0 - 10,
        'neemba_hub_send_failed_total': 0.0,
        'neemba_nats_connected': 1.0,
        'neemba_consumer_unparseable_total': 0.0,
        'neemba_stt_paused': 0.0,
        'neemba_ffmpeg_stale_total': 0.0,
        'neemba_publish_buffer_dropped_total': 0.0,
        'neemba_rtmp_auth_enabled': 1.0,
        '_scrape_ok': 1.0,
    }
    base.update(over)
    return base


def test_heartbeat_fires_only_while_session_active():
    stale = samples(neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)

    state, alerts = evaluate({}, stale, now=T0)
    assert any('심장박동' in a for a in alerts)

    idle = samples(neemba_hub_active_session=0.0,
                   neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)
    state, alerts = evaluate({}, idle, now=T0)
    assert alerts == []


def test_condition_alert_dedups_then_recovers_with_duration():
    stale = samples(neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)

    state, alerts = evaluate({}, stale, now=T0)
    assert len(alerts) == 1

    # 같은 장애 지속 → 침묵
    state, alerts = evaluate(state, stale, now=T0 + 60)
    assert alerts == []

    # 복구 → 지속시간 포함 1회
    state, alerts = evaluate(state, samples(), now=T0 + 120)
    assert len(alerts) == 1
    assert '복구' in alerts[0]


def test_condition_reminder_after_30min():
    stale = samples(neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)
    state, _ = evaluate({}, stale, now=T0)

    stale2 = samples(neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)
    state, alerts = evaluate(state, stale2, now=T0 + 1801)
    assert len(alerts) == 1
    assert '지속' in alerts[0]


def fresh(now, **over):
    # 심장박동 규칙이 끼어들지 않도록 broadcast 를 now 기준으로 신선하게 유지
    return samples(neemba_hub_last_broadcast_timestamp_seconds=now - 10, **over)


def test_counter_increase_fires_once_then_cooldown_accumulates():
    state, alerts = evaluate({}, fresh(T0), now=T0)
    assert alerts == []

    state, alerts = evaluate(
        state, fresh(T0 + 60, neemba_hub_send_failed_total=2.0), now=T0 + 60)
    assert any('send' in a for a in alerts)

    # 쿨다운 내 추가 증가 → 침묵
    state, alerts = evaluate(
        state, fresh(T0 + 120, neemba_hub_send_failed_total=5.0), now=T0 + 120)
    assert alerts == []

    # 쿨다운 경과 후 → 누적 delta 보고
    state, alerts = evaluate(
        state, fresh(T0 + 800, neemba_hub_send_failed_total=6.0), now=T0 + 800)
    assert len(alerts) == 1 and '4' in alerts[0]


def test_counter_ignored_while_session_inactive():
    state, _ = evaluate({}, samples(), now=T0)
    idle_bump = samples(neemba_hub_active_session=0.0,
                        neemba_hub_send_failed_total=3.0)
    state, alerts = evaluate(state, idle_bump, now=T0 + 60)
    assert alerts == []


def test_auth_disabled_alerts_on_daily_tick_only():
    state, alerts = evaluate(
        {}, fresh(T0, neemba_rtmp_auth_enabled=0.0), now=T0)
    assert any('인증' in a for a in alerts)  # 첫 실행 = 일일 틱

    state, alerts = evaluate(
        state, fresh(T0 + 3600, neemba_rtmp_auth_enabled=0.0), now=T0 + 3600)
    assert alerts == []  # 하루 안 지남 → 침묵

    state, alerts = evaluate(
        state, fresh(T0 + 86401, neemba_rtmp_auth_enabled=0.0), now=T0 + 86401)
    assert len(alerts) == 1  # 다음 일일 틱


def test_scrape_failure_fires_only_if_last_known_active():
    state, _ = evaluate({}, samples(), now=T0)  # active 기억됨

    down = {'_scrape_ok': 0.0}
    state, alerts = evaluate(state, down, now=T0 + 60)
    assert any('metrics' in a for a in alerts)

    # 비활성 상태에서 스크레이프 실패 → GHA health-watch 몫, 침묵
    state2, _ = evaluate({}, samples(neemba_hub_active_session=0.0), now=T0)
    state2, alerts = evaluate(state2, down, now=T0 + 60)
    assert alerts == []
