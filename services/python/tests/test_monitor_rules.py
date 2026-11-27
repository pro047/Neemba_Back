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


# AC-12: 아래부터 test_auto_stop_ignores_counter_reset_from_node_restart 까지
# 기존 23건은 heartbeat-alert 변경 후에도 본문·기대값 수정 없이 통과해야 한다
# (회귀 기준 — 구현 전에도 통과한다).
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


NO_PUBLISHER_STOP = 'neemba_session_stopped_total{reason="no_publisher"}'


def test_no_publisher_stop_is_reported_separately():
    # 2026-08-24 인시던트: 방송 없이 열린 세션이 스스로 닫힌다. 이걸 알리지
    # 않으면 운영자가 받는 유일한 신호가 stt_paused 시리즈 소멸로 인한
    # '✅ 복구' 뿐인데, 그건 "오디오가 돌아왔다"로 읽힌다.
    state, alerts = evaluate({}, samples(**{NO_PUBLISHER_STOP: 0.0}), now=T0)
    assert alerts == []

    stopped = samples(neemba_hub_active_session=0.0, **{NO_PUBLISHER_STOP: 1.0})
    state, alerts = evaluate(state, stopped, now=T0 + 60)

    alert = next((a for a in alerts if '방송 없음' in a), None)
    assert alert is not None
    # 정상 종료 문구와 섞이면 안 된다 — '방송 종료'는 실제 방송이 끝났다는 뜻이다.
    assert '방송 종료' not in alert


def test_no_publisher_stop_does_not_fire_the_normal_stop_rule():
    # 두 규칙이 같은 메트릭 이름을 라벨만 달리 쓴다. 라벨을 흘리면 방송 없이
    # 닫힌 세션이 '방송 종료' 로 보고돼 §운영 휴리스틱이 무너진다.
    state, alerts = evaluate(
        {}, samples(**{AUTO_STOP: 0.0, NO_PUBLISHER_STOP: 0.0}), now=T0)
    assert alerts == []

    stopped = samples(neemba_hub_active_session=0.0,
                      **{AUTO_STOP: 0.0, NO_PUBLISHER_STOP: 1.0})
    state, alerts = evaluate(state, stopped, now=T0 + 60)
    assert not any('방송 종료' in a for a in alerts)


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


# --- heartbeat-alert: 세션 시작 직후 오탐 제거 ---------------------------------
# 2026-11-22: 세션 시작 2초 뒤 tick 에서 "44977초째 번역 없음" 이 울렸다.
# 판정이 세션 시작 시각을 안 보고 now - last_broadcast 만 봤기 때문이다.
# monitor 가 관측한 비활성→활성 전이 시각(state['_active_since'])으로 gap 을 자른다.

OLD_BROADCAST = T0 - 44977  # 전날 예배의 마지막 번역


def _hb(alerts):
    return [a for a in alerts if '심장박동' in a]


def _idle(**over):
    return samples(neemba_hub_active_session=0.0, **over)


def _live_old(**over):
    return samples(neemba_hub_last_broadcast_timestamp_seconds=OLD_BROADCAST,
                   **over)


# AC-1
def test_first_active_tick_after_idle_tick_does_not_fire_heartbeat():
    # Arrange
    state, _ = evaluate({}, _idle(
        neemba_hub_last_broadcast_timestamp_seconds=OLD_BROADCAST), now=T0)

    # Act
    state, alerts = evaluate(state, _live_old(), now=T0 + 60)

    # Assert
    assert _hb(alerts) == []
    assert state['_active_since'] == T0 + 60


# AC-2
def test_heartbeat_gap_is_measured_from_observed_session_start():
    # Arrange
    state, _ = evaluate({}, _idle(
        neemba_hub_last_broadcast_timestamp_seconds=OLD_BROADCAST), now=T0)
    state, _ = evaluate(state, _live_old(), now=T0 + 60)
    state, alerts_180 = evaluate(state, _live_old(), now=T0 + 60 + 180)

    # Act
    state, alerts = evaluate(state, _live_old(), now=T0 + 60 + 240)

    # Assert
    assert _hb(alerts_180) == []  # 180 > 180 은 거짓
    hb = _hb(alerts)
    assert len(hb) == 1
    assert '240초째' in hb[0]
    assert '44977' not in hb[0]


# AC-3
def test_unobserved_transition_keeps_last_broadcast_judgement():
    # Arrange — 빈 state = 활성 중 첫 부팅: 전이를 못 봤다 (확정 결정 2)
    stale = samples(neemba_hub_last_broadcast_timestamp_seconds=T0 - 300)

    # Act
    state, alerts = evaluate({}, stale, now=T0)

    # Assert
    assert len(_hb(alerts)) == 1
    assert '300초째' in _hb(alerts)[0]
    assert state['_active_since'] == 0.0


# AC-4
def test_idle_tick_resets_active_since_and_next_session_restarts_it():
    # Arrange
    state, _ = evaluate({}, _idle(), now=T0)
    state, _ = evaluate(state, _live_old(), now=T0 + 60)
    assert state['_active_since'] == T0 + 60

    # Act
    state, _ = evaluate(state, _idle(
        neemba_hub_last_broadcast_timestamp_seconds=OLD_BROADCAST), now=T0 + 600)
    idle_since = state['_active_since']
    state, alerts = evaluate(state, _live_old(), now=T0 + 660)

    # Assert
    assert idle_since == 0.0
    assert state['_active_since'] == T0 + 660
    assert _hb(alerts) == []


# AC-5
def test_scrape_failure_tick_keeps_active_since():
    # Arrange
    state, _ = evaluate({}, _idle(), now=T0)
    state, _ = evaluate(state, _live_old(), now=T0 + 60)

    # Act
    state, _ = evaluate(state, {'_scrape_ok': 0.0}, now=T0 + 120)
    since_after_failure = state['_active_since']
    state, alerts = evaluate(state, _live_old(), now=T0 + 180)

    # Assert
    assert since_after_failure == T0 + 60
    assert state['_active_since'] == T0 + 60
    assert _hb(alerts) == []


# --- heartbeat-alert: 세션별 청취자 0명 알림 -----------------------------------
# python 허브가 세션별 시리즈 neemba_hub_session_listeners{sessionId} 와
# neemba_hub_undelivered_total{sessionId} 를 낸다. fetch_metrics 는 exposition
# 라인을 통째로 키로 쓰므로 samples 키는 아래 L()/U() 꼴이다.

S1 = 'alpha-session-1'   # 앞 8자 'alpha-se'
S2 = 'bravo-session-2'   # 앞 8자 'bravo-se'


def L(sid):
    return f'neemba_hub_session_listeners{{sessionId="{sid}"}}'


def U(sid):
    return f'neemba_hub_undelivered_total{{sessionId="{sid}"}}'


def sess(now, listeners=None, undelivered=None, **over):
    """세션별 시리즈가 붙은 samples. broadcast 는 신선하게(심장박동 비개입)."""
    s = fresh(now, **over)
    for sid, v in (listeners or {}).items():
        s[L(sid)] = float(v)
    for sid, v in (undelivered or {}).items():
        s[U(sid)] = float(v)
    return s


def _nl(alerts):
    return [a for a in alerts if '청취자 0명' in a]


def _recovered(alerts):
    return [a for a in alerts if 'no_listeners 복구' in a]


def _run(state, ticks):
    """ticks = [(now, samples)] 를 차례로 evaluate. (state, [alerts...])."""
    out = []
    for now, s in ticks:
        state, alerts = evaluate(state, s, now=now)
        out.append(alerts)
    return state, out


def _zero_ticks(start, steps, sid=S1, u0=0, inc=1, **over):
    """sid 가 0명인 채 매 60초 U 가 inc 씩 느는 tick 들 (start 포함 steps 개)."""
    return [(start + 60 * i,
             sess(start + 60 * i, {sid: 0}, {sid: u0 + inc * i}, **over))
            for i in range(steps)]


# AC-6
def test_zero_listeners_under_threshold_does_not_alert():
    # Arrange — T0 ~ T0+240 (0명 구간 240초)
    ticks = _zero_ticks(T0, 5)

    # Act
    _, outs = _run({}, ticks)

    # Assert
    assert all(_nl(a) == [] for a in outs)


# AC-7
def test_zero_listeners_over_threshold_with_drops_alerts_once():
    # Arrange — T0 ~ T0+300, U 0→5
    ticks = _zero_ticks(T0, 6)

    # Act
    state, outs = _run({}, ticks)
    state, next_alerts = evaluate(
        state, sess(T0 + 360, {S1: 0}, {S1: 6}), now=T0 + 360)

    # Assert
    assert all(_nl(a) == [] for a in outs[:-1])
    fired = _nl(outs[-1])
    assert len(fired) == 1
    assert S1[:8] in fired[0]
    assert '5건' in fired[0]
    assert '300초째' in fired[0]
    assert _hb(outs[-1]) == []
    assert _nl(next_alerts) == []


# AC-8
def test_listener_return_recovers_and_next_zero_interval_restarts():
    # Arrange — 발화까지
    state, _ = _run({}, _zero_ticks(T0, 6))

    # Act
    state, back = evaluate(
        state, sess(T0 + 360, {S1: 1}, {S1: 5}), now=T0 + 360)
    state, outs_again = _run(state, _zero_ticks(T0 + 420, 5, u0=6))  # ~T0+660
    state, at_300 = evaluate(
        state, sess(T0 + 720, {S1: 0}, {S1: 11}), now=T0 + 720)

    # Assert
    assert len(_recovered(back)) == 1
    assert _nl(back) == []
    assert all(_nl(a) == [] for a in outs_again)  # 재시작 후 240초
    fired = _nl(at_300)
    assert len(fired) == 1 and '5건' in fired[0]


# AC-9
def test_global_listener_gauge_alone_never_fires_no_listeners():
    # Arrange — 구버전 python: 세션별 시리즈 없음
    ticks = [(T0 + 60 * i,
              fresh(T0 + 60 * i, neemba_hub_listeners=0.0,
                    neemba_hub_undelivered_total=float(i)))
             for i in range(8)]

    # Act
    _, outs = _run({}, ticks)

    # Assert
    assert all(_nl(a) == [] for a in outs)


# AC-10
def test_heartbeat_suppressed_while_only_session_has_no_listeners():
    # Arrange
    s = sess(T0, {S1: 0}, {S1: 0})
    # sess() 는 broadcast 를 신선하게 두므로 묵힌다
    s['neemba_hub_last_broadcast_timestamp_seconds'] = T0 - 300

    # Act
    _, alerts = evaluate({}, s, now=T0)

    # Assert
    assert _hb(alerts) == []


# AC-11
def test_zero_listeners_while_inactive_does_not_alert():
    # Arrange
    ticks = _zero_ticks(T0, 8, neemba_hub_active_session=0.0)

    # Act
    _, outs = _run({}, ticks)

    # Assert
    assert all(_nl(a) == [] for a in outs)


# AC-13
def test_zero_listeners_without_dropped_translation_does_not_alert():
    # Arrange — U 가 구간 내내 3 그대로
    ticks = _zero_ticks(T0, 8, u0=3, inc=0)

    # Act
    _, outs = _run({}, ticks)

    # Assert
    assert all(_nl(a) == [] for a in outs)


# AC-13
def test_zero_listeners_without_undelivered_series_does_not_alert():
    # Arrange — U 키 자체가 없음
    ticks = [(T0 + 60 * i, sess(T0 + 60 * i, {S1: 0})) for i in range(8)]

    # Act
    _, outs = _run({}, ticks)

    # Assert
    assert all(_nl(a) == [] for a in outs)


# AC-14
def test_undelivered_counter_reset_rebases_without_alert():
    # Arrange — 50 에서 python 재시작으로 2 로 리셋, 이후 2→5
    values = [50, 50, 2, 3, 4, 5]
    ticks = [(T0 + 60 * i, sess(T0 + 60 * i, {S1: 0}, {S1: v}))
             for i, v in enumerate(values)]

    # Act
    _, outs = _run({}, ticks)

    # Assert
    assert all(_nl(a) == [] for a in outs[:-1])
    fired = _nl(outs[-1])
    assert len(fired) == 1
    assert '3건' in fired[0]


# AC-15
def test_evaluate_does_not_inject_keys_into_caller_samples():
    # Arrange
    state, _ = _run({}, _zero_ticks(T0, 5))
    s = sess(T0 + 300, {S1: 0}, {S1: 5})
    keys_before = set(s)

    # Act
    evaluate(state, s, now=T0 + 300)

    # Assert
    assert set(s) == keys_before
    assert '_active_since' not in s
    assert '_no_listener_offenders' not in s


# AC-16
def test_one_unattended_session_alerts_even_when_another_has_listeners():
    # Arrange — 전역 합계라면 2 라서 못 잡는 경우
    ticks = [(T0 + 60 * i,
              sess(T0 + 60 * i, {S1: 2, S2: 0}, {S1: 0, S2: i}))
             for i in range(6)]

    # Act
    _, outs = _run({}, ticks)

    # Assert
    fired = _nl(outs[-1])
    assert len(fired) == 1
    assert S2[:8] in fired[0]
    assert S1[:8] not in fired[0]
    assert '5건' in fired[0]


# AC-17
def test_heartbeat_not_suppressed_when_some_session_has_listeners():
    # Arrange
    s = sess(T0, {S1: 2, S2: 0}, {S1: 0, S2: 0})
    s['neemba_hub_last_broadcast_timestamp_seconds'] = T0 - 300

    # Act
    _, alerts = evaluate({}, s, now=T0)

    # Assert
    assert len(_hb(alerts)) == 1


# AC-18
def test_undelivered_of_other_session_does_not_count():
    # Arrange — s1: 0명 360초지만 U 불변. s2: T0+120 부터 0명(240초), U 증가
    ticks = []
    for i in range(7):
        now = T0 + 60 * i
        listeners, undelivered = {S1: 0}, {S1: 7}
        if i >= 2:
            listeners[S2] = 0
            undelivered[S2] = (i - 2) * 3
        ticks.append((now, sess(now, listeners, undelivered)))

    # Act
    _, outs = _run({}, ticks)

    # Assert
    assert all(_nl(a) == [] for a in outs)


# AC-19
def test_session_series_disappearing_recovers_and_clears_tracking():
    # Arrange — s1 발화까지
    state, outs = _run({}, _zero_ticks(T0, 6))
    assert len(_nl(outs[-1])) == 1

    # Act — 세션 해제로 s1 시리즈가 /metrics 에서 사라짐
    state, alerts = evaluate(state, fresh(T0 + 360), now=T0 + 360)

    # Assert
    assert len(_recovered(alerts)) == 1
    assert S1 not in state.get('_listeners_zero', {})


# AC-20
def test_sessions_crossing_together_are_reported_in_one_alert():
    # Arrange — s1 +5, s2 +10 (같은 tick 에 함께 300초)
    ticks = [(T0 + 60 * i,
              sess(T0 + 60 * i, {S1: 0, S2: 0}, {S1: i, S2: 2 * i}))
             for i in range(6)]

    # Act
    _, outs = _run({}, ticks)

    # Assert
    fired = _nl(outs[-1])
    assert len(fired) == 1
    assert S1[:8] in fired[0]
    assert S2[:8] in fired[0]
    assert '15건' in fired[0]


# --- 설계 경계 (검증 기준 밖) --------------------------------------------------

def test_zero_interval_base_filled_when_undelivered_appears_later():
    # Arrange — 0명 첫 tick 에 U 키가 없으면 base=None, 다음 tick 의 값으로 채움
    ticks = [(T0, sess(T0, {S1: 0}))]
    ticks += [(T0 + 60 * i, sess(T0 + 60 * i, {S1: 0}, {S1: i - 1}))
              for i in range(1, 6)]  # U: 0,1,2,3,4

    # Act
    _, outs = _run({}, ticks)

    # Assert
    fired = _nl(outs[-1])
    assert len(fired) == 1
    assert '4건' in fired[0]


def test_scrape_failure_inside_zero_interval_keeps_interval_start():
    # Arrange
    ticks = _zero_ticks(T0, 4)  # ~T0+180
    ticks.append((T0 + 240, {'_scrape_ok': 0.0}))
    ticks.append((T0 + 300, sess(T0 + 300, {S1: 0}, {S1: 5})))

    # Act
    state, outs = _run({}, ticks)

    # Assert
    fired = _nl(outs[-1])
    assert len(fired) == 1
    assert '300초째' in fired[0] and '5건' in fired[0]


def test_going_inactive_clears_zero_interval_tracking():
    # Arrange
    state, _ = _run({}, _zero_ticks(T0, 3))

    # Act
    state, _ = evaluate(state, _idle(), now=T0 + 180)

    # Assert
    assert state['_listeners_zero'] == {}


def test_empty_session_series_does_not_suppress_heartbeat():
    # Arrange — 세션별 시리즈 0개: all-of 공허참으로 억제하면 안 된다
    s = samples(neemba_hub_last_broadcast_timestamp_seconds=T0 - 300,
                neemba_hub_listeners=0.0)

    # Act
    _, alerts = evaluate({}, s, now=T0)

    # Assert
    assert len(_hb(alerts)) == 1


def test_session_series_parser_ignores_unlabelled_and_created_keys():
    # Arrange
    from monitor import UNDELIVERED, _session_series
    s = {
        U(S1): 4.0,
        'neemba_hub_undelivered_total': 99.0,
        f'neemba_hub_undelivered_created{{sessionId="{S1}"}}': 1.8e9,
        f'neemba_hub_undelivered_total_created{{sessionId="{S2}"}}': 1.8e9,
        L(S2): 0.0,
    }

    # Act
    got = _session_series(s, UNDELIVERED)

    # Assert
    assert got == {S1: 4.0}


def test_created_series_does_not_inflate_dropped_count():
    # Arrange — Counter 의 _created 시리즈(타임스탬프 값)가 섞여 들어와도
    ticks = []
    for now, s in _zero_ticks(T0, 6):
        s[f'neemba_hub_undelivered_created{{sessionId="{S1}"}}'] = T0 - 5
        ticks.append((now, s))

    # Act
    _, outs = _run({}, ticks)

    # Assert
    fired = _nl(outs[-1])
    assert len(fired) == 1 and '5건' in fired[0]
