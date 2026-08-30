"""알림 사이드카 — 앱 /metrics 폴링 → 규칙 판정 → 디스코드.

docker.sock 없이(저장소 보안 결정) compose 네트워크에서 python/node 의
/metrics 만 읽는다. 규칙·dedup 은 순수 함수 evaluate() 에 격리
(services/python/tests/test_monitor_rules.py 가 CI 에서 회귀 방지).

상태 전이 모델:
- condition 규칙: 발생 1회 + 복구 1회(지속시간) + REMINDER_SECONDS 리마인더
- event(카운터) 규칙: 세션 활성 중 증가 시 알림, COOLDOWN 동안은 침묵하며
  누적, 쿨다운 경과 후 누적 delta 로 1회 보고
- daily 규칙: 24h 마다 1회 판정 (auth 등 비긴급 위생)

표준 라이브러리만 사용 (사이드카 이미지 = python:3.12-alpine + ro 마운트).
"""
import json
import os
import time
import urllib.request

INTERVAL_SECONDS = int(os.environ.get('MONITOR_INTERVAL', '60'))
GAP_SECONDS = int(os.environ.get('MONITOR_GAP_THRESHOLD', '180'))
REMINDER_SECONDS = 1800
COUNTER_COOLDOWN_SECONDS = 600
DAILY_SECONDS = 86400

ACTIVE = 'neemba_hub_active_session'
LAST_BROADCAST = 'neemba_hub_last_broadcast_timestamp_seconds'
STT_PAUSED = 'neemba_stt_paused'


def _is_active(samples: dict) -> bool:
    return samples.get(ACTIVE, 0.0) == 1.0


# stt_paused 는 세션별 sessionId 라벨 시리즈다 (마이크 다중 세션 이후).
# fetch_metrics 가 exposition 라인을 통째로 키로 쓰므로 samples 키는
# 'neemba_stt_paused{sessionId="..."}' 꼴 — 라벨 유무 모두 매칭한다
# (라벨 없는 구버전 node 를 읽는 전환 창 호환).

def _stt_paused_values(samples: dict) -> list:
    return [v for k, v in samples.items()
            if k == STT_PAUSED or k.startswith(STT_PAUSED + '{')]


def _any_stt_paused(samples: dict, now: float = 0.0) -> bool:
    """어느 한 세션이라도 '4연속 무오디오'로 STT 로테이션이 멈춘 상태.

    정보성 보고(송출 중단 알림)용 — 한 세션의 중단도 알릴 가치가 있다.
    """
    return any(v == 1.0 for v in _stt_paused_values(samples))


def _all_stt_paused(samples: dict, now: float = 0.0) -> bool:
    """살아 있는 모든 세션이 오디오를 잃은 상태 = 방송 종료 수순.

    '번역이 없는 게 당연한가'와 'ffmpeg 무진행이 방송 종료 탓인가'의 판정은
    이쪽이다 — any-of 를 쓰면 마이크 한 세션의 pause 가 RTMP 세션의 진짜
    장애 경보(심장박동·ffmpeg_stale)까지 삼킨다 (계획 §4 함정). 시리즈가
    0개면(세션 없음/정리 직후) 공허참으로 삼키지 않고 False — 세션이
    활성인데 첫 번역이 영영 안 오는 경우를 놓치지 않기 위해서다.
    """
    values = _stt_paused_values(samples)
    return bool(values) and all(v == 1.0 for v in values)


def _heartbeat_stale(samples: dict, now: float) -> bool:
    if not _is_active(samples):
        return False
    # 전 세션이 오디오를 잃어 STT 가 멈춘 상태면 번역이 없는 게 당연하다 —
    # 장애가 아니라 송출 종료다. 여기서 걸러야 예배 종료 3분 뒤 심장박동
    # 오탐이 안 뜬다. (일부 세션만 pause 면 나머지가 번역을 내야 정상이므로
    # 억제하지 않는다.)
    if _all_stt_paused(samples):
        return False
    # last_broadcast==0(부팅 후 broadcast 없음)이면 gap 무한대로 간주 —
    # 세션이 열렸는데 첫 번역이 영영 안 오는 경우를 놓치지 않는다.
    return now - samples.get(LAST_BROADCAST, 0.0) > GAP_SECONDS


# condition 규칙: (이름, 판정, 게이트, 발생 문구)
CONDITION_RULES = [
    ('heartbeat',
     _heartbeat_stale,
     'always',
     '🚨 번역 심장박동 끊김 — 세션 활성 중 {gap:.0f}초째 번역 없음'),
    # 장애가 아니라 상태 전환 보고다 — 방송 종료면 정상, 방송 중이면 송출
    # 사고다. 어느 쪽인지는 사람이 시간대를 보고 판단하는 게 맞아 정보성으로
    # 낮췄다. 오디오가 돌아오면 아래 공통 경로가 ✅ 복구를 보낸다.
    ('stt_paused',
     lambda s, now: _is_active(s) and _any_stt_paused(s),
     'always',
     'ℹ️ 송출 중단 — 오디오 유입이 끊겨 STT 일시정지 (방송 종료면 정상, '
     '방송 중이면 OBS·RTMP 확인). 오디오 복귀 시 자동 재개'),
    ('nats_down',
     lambda s, now: s.get('neemba_nats_connected', 1.0) == 0.0,
     'active',
     '🚨 NATS 연결 끊김 — consumer 가 브로커에 붙지 못함'),
    ('scrape_failed',
     # 일시 타임아웃 1회로도 울리는 과민 방지: 2연속 실패부터 발화
     # (streak 은 evaluate 가 state 에 유지)
     lambda s, now: s.get('_scrape_fail_streak', 0.0) >= 2,
     'last_known_active',
     '🚨 /metrics 응답 없음 — 앱 컨테이너 상태 확인 필요'),
    ('rtmp_auth_disabled',
     lambda s, now: s.get('neemba_rtmp_auth_enabled', 1.0) == 0.0,
     'daily',
     '⚠️ RTMP 인증 꺼짐 (RTMP_PUBLISH_KEY 미설정) — 아무나 publish 가능'),
]

# event(카운터) 규칙: (이름, 메트릭, 문구)
COUNTER_RULES = [
    ('send_failed', 'neemba_hub_send_failed_total',
     '🚨 WS send 실패 {delta:.0f}건 발생 (누적 {total:.0f})'),
    ('buffer_dropped', 'neemba_publish_buffer_dropped_total',
     '🚨 재시도 버퍼가 자막 {delta:.0f}건을 버림 — 60초 초과 NATS 순단 의심'),
    ('unparseable', 'neemba_consumer_unparseable_total',
     '🚨 파싱 불가 메시지 {delta:.0f}건 term — 발행 쪽 포맷 확인'),
    ('ffmpeg_stale', 'neemba_ffmpeg_stale_total',
     '🚨 ffmpeg 10초 무진행 {delta:.0f}회 — RTMP 수신 정체 의심'),
]

# 카운터 규칙별 예외 옵션. 기본(미등재)은 '억제 없음 + 즉시 보고'라 규칙
# 목록 자체는 단순하게 유지된다.
#   suppress: 참이면 쌓인 delta 를 폐기하고 보고하지 않는다
#   grace   : 첫 증가분을 이 초만큼 붙들어 둔다 — 억제 신호가 늦게 도착하는
#             경합을 흡수하기 위한 유예
#
# ffmpeg_stale 이 유일한 대상인 이유: 송출이 끊기면 ffmpeg 무진행이 먼저
# 관측되고 node 의 stt_paused 판정(4연속 무오디오)은 그보다 몇 초 늦게 선다.
# 유예 없이 억제만 걸면 '방송 종료'인데도 첫 1회는 그대로 발화한다(2026-07-29
# 실측: stale 11:21:39 → paused 11:21:45 → 알림 11:21:46).
COUNTER_STALE_GRACE_SECONDS = 90

# suppress 가 _all_stt_paused 인 이유: ffmpeg 무진행은 RTMP 세션의 신호인데
# 메트릭만으로는 어느 세션이 RTMP 인지 알 수 없다. any-of 면 마이크 세션의
# pause 가 RTMP 의 진짜 정체 경보를 삼키므로, '전 세션 무오디오 = 방송 종료
# 수순'일 때만 억제한다 (보수적 축소 — 계획 §4). 방송 종료 시 마이크 세션이
# 유예(90s)보다 늦게 닫히면 stale 1회가 발화할 수 있다: 오탐이지만 침묵보다
# 낫다고 판단.
COUNTER_OPTIONS = {
    'ffmpeg_stale': {'suppress': _all_stt_paused,
                     'grace': COUNTER_STALE_GRACE_SECONDS},
}

# 규칙 이름이 바뀌면 옵션이 조용히 무력화되는 걸 막는다.
assert set(COUNTER_OPTIONS) <= {name for name, _, _ in COUNTER_RULES}
# info(카운터) 규칙: 장애가 아니라 운영 사실. COUNTER_RULES 와 달리 세션 활성
# 게이트도 쿨다운도 없다 — 자동 종료는 세션이 닫힌 *뒤* 관측되므로 활성 게이트를
# 통과할 수 없고, 방송당 많아야 1회라 쿨다운이 침묵시킬 이유가 없다.
# 메트릭 키에 라벨이 붙어 있는 건 fetch_metrics 가 exposition 라인을 통째로
# 키로 쓰기 때문이다 (node metrics.ts 가 이 문자열을 고정한다).
#
# 문구가 "운영자 stop 미호출" 이었던 건 수동 stop 이 주 경로이던 시절의 서술이다.
# P1 D4(release #82) 로 POST /api/sessions/stop 이 세션을 닫지 않게 되면서
# on_publish_done + 유예가 **유일한** 종료 경로가 됐다. 이제 이 알림은 이상
# 신호가 아니라 정상 종료 그 자체이고, 운영자가 봐야 할 것은 발화가 아니라
# **부재**다 — 예배가 끝났는데 이게 안 오면 세션이 안 닫힌 것이고, 회수 수단은
# POST /internal/sessions/stop 직접 호출뿐이다.
INFO_COUNTER_RULES = [
    ('session_auto_stopped',
     'neemba_session_stopped_total{reason="publisher_done"}',
     'ℹ️ 방송 종료 — 세션 자동 종료 {delta:.0f}회 (정상 경로. '
     '예배 후 이 알림이 없으면 세션이 안 닫힌 것)'),
    # 2026-08-24 인시던트. 위 규칙과 라벨만 다른 같은 메트릭인데 문구를 나눈
    # 이유는, 이쪽은 방송이 아예 없던 세션이라 '방송 종료' 로 보고하면 위
    # 규칙의 운영 휴리스틱(예배 후 알림 부재 = 세션 미종료)이 무너지기
    # 때문이다. 이 알림이 없으면 운영자가 보는 유일한 신호는 stt_paused
    # 시리즈 소멸로 인한 '✅ 복구' 뿐이고, 그건 정반대로 읽힌다.
    ('session_stopped_no_publisher',
     'neemba_session_stopped_total{reason="no_publisher"}',
     'ℹ️ 방송 없음 — 발행자가 끝내 붙지 않아 세션을 정리했습니다 {delta:.0f}회 '
     '([시작]만 눌리고 OBS 가 안 켜진 경우. 예배 중이면 발행 경로를 확인할 것)'),
]


def _fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f'{minutes}분 {int(seconds % 60)}초' if minutes else f'{int(seconds)}초'


def evaluate(state: dict, samples: dict, now: float) -> tuple[dict, list[str]]:
    """순수 함수: (규칙 상태, 관측값, 현재시각) → (새 상태, 알림 문자열들)."""
    state = json.loads(json.dumps(state))  # caller 의 dict 를 오염시키지 않음
    alerts: list[str] = []

    scrape_ok = samples.get('_scrape_ok', 1.0) == 1.0
    if scrape_ok:
        state['_last_known_active'] = _is_active(samples)
        state['_scrape_fail_streak'] = 0
    else:
        state['_scrape_fail_streak'] = state.get('_scrape_fail_streak', 0) + 1
    samples = dict(samples)
    samples['_scrape_fail_streak'] = state['_scrape_fail_streak']

    # scrape 실패 중엔 일일 틱을 소모하지 않는다 — 부팅 경합으로 첫 틱이
    # 실패하면 daily 규칙(auth 등)이 24h 밀리는 버그 방지.
    daily_due = scrape_ok and now - state.get('_last_daily', 0.0) >= DAILY_SECONDS
    if daily_due:
        state['_last_daily'] = now

    for name, predicate, gate, message in CONDITION_RULES:
        rule = state.setdefault(name, {'active': False, 'since': 0.0,
                                       'last_alert': 0.0})
        if gate == 'daily' and not daily_due:
            continue
        if gate == 'active' and not (_is_active(samples) or daily_due):
            continue
        if gate == 'last_known_active' and not state.get('_last_known_active'):
            continue
        # scrape 실패 시 앱 메트릭 기반 규칙은 판정 불가 → scrape_failed 만 판정
        if not scrape_ok and name != 'scrape_failed':
            continue

        firing = predicate(samples, now)
        gap = now - samples.get(LAST_BROADCAST, 0.0)

        if firing and not rule['active']:
            rule.update(active=True, since=now, last_alert=now)
            alerts.append(message.format(gap=gap))
        elif firing and rule['active'] and gate != 'daily':
            if now - rule['last_alert'] >= REMINDER_SECONDS:
                rule['last_alert'] = now
                alerts.append(
                    f'⏰ {name} 장애 지속 중 ({_fmt_duration(now - rule["since"])})')
        elif firing and rule['active'] and gate == 'daily':
            rule['last_alert'] = now
            alerts.append(message.format(gap=gap))
        elif not firing and rule['active']:
            rule['active'] = False
            alerts.append(
                f'✅ {name} 복구 (지속 {_fmt_duration(now - rule["since"])})')

    for name, metric, message in COUNTER_RULES:
        rule = state.setdefault(name, {'last_value': None, 'last_alert': 0.0,
                                       'unreported': 0.0})
        # 디스크에 영속된 옛 상태에는 pending_since 가 없다 (state.json).
        rule.setdefault('pending_since', 0.0)
        if not scrape_ok:
            continue
        opts = COUNTER_OPTIONS.get(name, {})
        suppress = opts.get('suppress')
        value = samples.get(metric, 0.0)
        prev = rule['last_value']
        rule['last_value'] = value
        # 정상 종료로 판명 — 유예 중 쌓인 delta 는 보고하지 않고 버린다.
        # last_value 갱신 뒤에 둬야 재개 시점에 옛 값과의 차이로 오탐하지 않는다.
        if suppress and suppress(samples, now):
            rule['unreported'] = 0.0
            rule['pending_since'] = 0.0
            continue
        if prev is None:
            continue  # 첫 관측은 기준점만 잡는다 (재시작 후 오탐 방지)
        delta = value - prev
        # 이원화: 카운터류는 세션 활성 중에만 의미
        if delta > 0 and _is_active(samples):
            rule['unreported'] += delta
            if not rule['pending_since']:
                rule['pending_since'] = now
        if rule['unreported'] <= 0:
            continue
        # 유예 중엔 증가가 멈춰도 계속 붙들고 있는다 — 다음 틱에 억제 신호가
        # 서면 위에서 폐기되고, 안 서면 진짜 정체로 보고된다.
        if now - rule['pending_since'] < opts.get('grace', 0):
            continue
        if now - rule['last_alert'] >= COUNTER_COOLDOWN_SECONDS:
            alerts.append(message.format(delta=rule['unreported'], total=value))
            rule['last_alert'] = now
            rule['unreported'] = 0.0
            rule['pending_since'] = 0.0

    for name, metric, message in INFO_COUNTER_RULES:
        rule = state.setdefault(name, {'last_value': None})
        if not scrape_ok:
            continue
        value = samples.get(metric, 0.0)
        prev = rule['last_value']
        rule['last_value'] = value
        # 첫 관측은 기준점만 잡고, 감소는 node 재시작에 의한 리셋이므로 무시.
        if prev is None or value <= prev:
            continue
        alerts.append(message.format(delta=value - prev, total=value))

    return state, alerts


# ---------- IO (evaluate 밖: 테스트 대상 아님, dev 라이브 검증으로 확인) ----------

def fetch_metrics(urls: list[str]) -> dict:
    merged: dict = {}
    ok = True
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=5) as res:
                for line in res.read().decode().splitlines():
                    if line.startswith('#') or ' ' not in line:
                        continue
                    key, _, val = line.partition(' ')
                    try:
                        merged[key] = float(val)
                    except ValueError:
                        continue
        except Exception as exc:
            print(f'monitor: scrape failed {url}: {exc!r}', flush=True)
            ok = False
    merged['_scrape_ok'] = 1.0 if ok else 0.0
    return merged


def send_discord(webhook: str | None, text: str) -> None:
    if not webhook:
        print(f'monitor: (no webhook) {text}', flush=True)
        return
    body = json.dumps({'content': f'[neemba] {text}'}).encode()
    req = urllib.request.Request(
        webhook, data=body, headers={
            'Content-Type': 'application/json',
            # Cloudflare 가 기본 'Python-urllib/x' UA 의 POST 를 403 으로
            # 차단한다 (dev 실측). GET 은 통과해서 URL 검증만으론 안 잡힘.
            'User-Agent': 'neemba-monitor/1.0',
        })
    try:
        urllib.request.urlopen(req, timeout=10).read()
        print(f'monitor: alerted: {text}', flush=True)
    except Exception as exc:
        print(f'monitor: discord send failed: {exc!r}', flush=True)


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main() -> None:
    urls = [
        os.environ.get('PYTHON_METRICS_URL', 'http://python:8000/metrics'),
        os.environ.get('NODE_METRICS_URL', 'http://node:3000/metrics'),
    ]
    webhook = os.environ.get('DISCORD_WEBHOOK_URL')
    state_file = os.environ.get('STATE_FILE', '/var/lib/monitor/state.json')
    oneshot = os.environ.get('MONITOR_ONESHOT') == '1'
    if not webhook:
        print('monitor: DISCORD_WEBHOOK_URL not set — log-only mode', flush=True)

    state = load_state(state_file)
    while True:
        samples = fetch_metrics(urls)
        state, alerts = evaluate(state, samples, time.time())
        for alert in alerts:
            send_discord(webhook, alert)
        save_state(state_file, state)
        if oneshot:
            return
        time.sleep(INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
