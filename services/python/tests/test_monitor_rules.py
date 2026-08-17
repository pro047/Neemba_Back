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


def test_daily_tick_not_burned_by_scrape_failure():
    # 부팅 직후 앱 재시작 등으로 첫 틱이 scrape 실패면, 일일 틱을 소모하지
    # 않고 다음 정상 틱에서 daily 규칙(auth)이 발화해야 한다.
    state, alerts = evaluate({}, {'_scrape_ok': 0.0}, now=T0)
    assert alerts == []

    state, alerts = evaluate(
        state, fresh(T0 + 60, neemba_rtmp_auth_enabled=0.0), now=T0 + 60)
    assert any('인증' in a for a in alerts)


def test_scrape_failure_fires_after_two_consecutive_misses():
    state, _ = evaluate({}, samples(), now=T0)  # active 기억됨

    down = {'_scrape_ok': 0.0}
    # 1회 실패는 일시 타임아웃일 수 있음 → 침묵
    state, alerts = evaluate(state, down, now=T0 + 60)
    assert alerts == []
    # 2연속 실패 → 발화
    state, alerts = evaluate(state, down, now=T0 + 120)
    assert any('metrics' in a for a in alerts)

    # 비활성 상태에서 스크레이프 실패 → GHA health-watch 몫, 침묵
    state2, _ = evaluate({}, samples(neemba_hub_active_session=0.0), now=T0)
    state2, alerts = evaluate(state2, down, now=T0 + 60)
    state2, alerts = evaluate(state2, down, now=T0 + 120)
    assert alerts == []


def test_STT가_일시정지_상태면_심장박동_경보가_뜨지_않아야_한다():
    # 세션은 stop 호출 전까지 active 로 남는다 — 방송이 끝나 오디오가 끊긴
    # 상태를 번역 끊김으로 오인하면 안 된다.
    paused = samples(neemba_hub_last_broadcast_timestamp_seconds=T0 - 300,
                     neemba_stt_paused=1.0)

    _, alerts = evaluate({}, paused, now=T0)

    assert not any('심장박동' in a for a in alerts)
    assert any('송출 중단' in a for a in alerts)


# --- sessionId 라벨드 stt_paused (게이지 라벨화 계획) --------------------------
# node 가 세션별 시리즈를 노출하면 fetch_metrics 는 exposition 라인 통째로
# 키를 잡으므로 samples 의 키가 'neemba_stt_paused{sessionId="..."}' 가 된다.

def _stt_series(session_id):
    return f'neemba_stt_paused{{sessionId="{session_id}"}}'


def labelled(paused_by_session, **over):
    s = samples(**over)
    del s['neemba_stt_paused']
    for session_id, value in paused_by_session.items():
        s[_stt_series(session_id)] = value
    return s


def test_일부_세션만_일시정지면_심장박동_경보를_억제하지_않아야_한다():
    # 마이크 세션 하나가 오디오를 잃어도 RTMP 세션이 살아 있으면 번역은
    # 나와야 정상이다 — 전 세션이 멈춘 게 아니면 침묵은 장애다.
    partial = labelled(
        {'rtmp': 0.0, 'mic': 1.0},
        neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)

    _, alerts = evaluate({}, partial, now=T0)

    assert any('심장박동' in a for a in alerts)


def test_모든_세션이_일시정지면_심장박동_경보를_억제해야_한다():
    # 전 세션 무오디오 = 방송 종료 수순. 번역이 없는 게 당연하다.
    all_paused = labelled(
        {'rtmp': 1.0, 'mic': 1.0},
        neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)

    _, alerts = evaluate({}, all_paused, now=T0)

    assert not any('심장박동' in a for a in alerts)


def test_stt_시리즈가_하나도_없으면_심장박동_억제가_없어야_한다():
    # 라벨드 게이지는 세션이 없으면 시리즈가 0줄이다. 세션이 활성인데 첫
    # 번역이 영영 안 오는 경우를 빈 시리즈의 공허참(all)으로 삼키면 안 된다.
    no_series = labelled(
        {}, neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)

    _, alerts = evaluate({}, no_series, now=T0)

    assert any('심장박동' in a for a in alerts)


def test_어느_세션이든_일시정지면_송출_중단_정보_알림이_떠야_한다():
    partial = labelled(
        {'rtmp': 0.0, 'mic': 1.0},
        neemba_hub_last_broadcast_timestamp_seconds=T0 - 10)

    _, alerts = evaluate({}, partial, now=T0)

    assert any('송출 중단' in a for a in alerts)


def test_ffmpeg_정체는_유예_안에서는_보고하지_않아야_한다():
    state, _ = evaluate({}, fresh(T0), now=T0)

    state, alerts = evaluate(
        state, fresh(T0 + 60, neemba_ffmpeg_stale_total=4.0), now=T0 + 60)

    assert not any('ffmpeg' in a for a in alerts)


def test_유예_중_STT가_멈추면_ffmpeg_정체를_폐기해야_한다():
    state, _ = evaluate({}, fresh(T0), now=T0)
    state, _ = evaluate(
        state, fresh(T0 + 60, neemba_ffmpeg_stale_total=4.0), now=T0 + 60)

    state, alerts = evaluate(
        state,
        fresh(T0 + 120, neemba_ffmpeg_stale_total=4.0, neemba_stt_paused=1.0),
        now=T0 + 120)

    assert not any('ffmpeg' in a for a in alerts)

    # 오디오가 돌아와도 폐기된 delta 가 되살아나면 안 된다
    state, alerts = evaluate(
        state, fresh(T0 + 300, neemba_ffmpeg_stale_total=4.0), now=T0 + 300)
    assert not any('ffmpeg' in a for a in alerts)


def test_유예_중_다른_세션만_일시정지면_ffmpeg_정체를_폐기하지_않아야_한다():
    # 계획 §4 함정: any-of 억제였다면 마이크 세션의 pause 가 RTMP 세션의
    # 진짜 수신 정체 경보까지 삼킨다. 전 세션 pause 일 때만 방송 종료로 본다.
    state, _ = evaluate({}, fresh(T0), now=T0)
    state, _ = evaluate(
        state, fresh(T0 + 60, neemba_ffmpeg_stale_total=4.0), now=T0 + 60)

    partial = labelled(
        {'rtmp': 0.0, 'mic': 1.0},
        neemba_hub_last_broadcast_timestamp_seconds=T0 + 180 - 10,
        neemba_ffmpeg_stale_total=4.0)
    state, alerts = evaluate(state, partial, now=T0 + 180)

    assert any('ffmpeg' in a for a in alerts)


def test_유예_중_모든_세션이_일시정지면_ffmpeg_정체를_폐기해야_한다():
    state, _ = evaluate({}, fresh(T0), now=T0)
    state, _ = evaluate(
        state, fresh(T0 + 60, neemba_ffmpeg_stale_total=4.0), now=T0 + 60)

    all_paused = labelled(
        {'rtmp': 1.0, 'mic': 1.0},
        neemba_hub_last_broadcast_timestamp_seconds=T0 + 120 - 10,
        neemba_ffmpeg_stale_total=4.0)
    state, alerts = evaluate(state, all_paused, now=T0 + 120)

    assert not any('ffmpeg' in a for a in alerts)


def test_STT가_정상인데_ffmpeg_정체가_유예를_넘기면_보고해야_한다():
    state, _ = evaluate({}, fresh(T0), now=T0)
    state, _ = evaluate(
        state, fresh(T0 + 60, neemba_ffmpeg_stale_total=4.0), now=T0 + 60)

    # 증가가 멈춰도 유예가 지나면 붙들고 있던 delta 를 보고한다
    state, alerts = evaluate(
        state, fresh(T0 + 180, neemba_ffmpeg_stale_total=4.0), now=T0 + 180)

    stale_alert = next((a for a in alerts if 'ffmpeg' in a), None)
    assert stale_alert is not None
    assert '4' in stale_alert
AUTO_STOP = 'neemba_session_stopped_total{reason="publisher_done"}'


def test_auto_stop_alerts_even_after_session_went_inactive():
    # 자동 종료는 세션이 닫힌 뒤에 관측된다 — COUNTER_RULES 의 활성 게이트를
    # 통과할 수 없으므로 info 규칙이 따로 있어야 한다.
    state, alerts = evaluate({}, samples(**{AUTO_STOP: 0.0}), now=T0)
    assert alerts == []

    stopped = samples(neemba_hub_active_session=0.0, **{AUTO_STOP: 1.0})
    state, alerts = evaluate(state, stopped, now=T0 + 60)
    assert any('자동 종료' in a for a in alerts)


def test_auto_stop_does_not_repeat_without_a_new_increase():
    state, _ = evaluate({}, samples(**{AUTO_STOP: 0.0}), now=T0)
    state, alerts = evaluate(state, samples(**{AUTO_STOP: 1.0}), now=T0 + 60)
    assert len(alerts) == 1

    state, alerts = evaluate(state, samples(**{AUTO_STOP: 1.0}), now=T0 + 120)
    assert alerts == []


def test_auto_stop_ignores_counter_reset_from_node_restart():
    state, _ = evaluate({}, samples(**{AUTO_STOP: 3.0}), now=T0)
    state, alerts = evaluate(state, samples(**{AUTO_STOP: 0.0}), now=T0 + 60)
    assert alerts == []
