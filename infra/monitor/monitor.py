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


def _stt_paused(samples: dict, now: float = 0.0) -> bool:
    """node 가 '4연속 무오디오'를 판정해 STT 로테이션을 멈춘 상태.

    송출이 끊겼다는 확정 신호라 '방송 종료(정상)'와 '수신 장애'를 가르는
    유일한 관측값이다. 세션은 stop 호출 전까지 active 로 남으므로, 이 신호
    없이는 예배가 끝난 것과 RTMP 가 막힌 것을 구분할 수 없다.
    """
    return samples.get(STT_PAUSED, 0.0) == 1.0


def _heartbeat_stale(samples: dict, now: float) -> bool:
    if not _is_active(samples):
        return False
    # 오디오가 끊겨 STT 가 멈춘 상태면 번역이 없는 게 당연하다 — 장애가 아니라
    # 송출 종료다. 여기서 걸러야 예배 종료 3분 뒤 심장박동 오탐이 안 뜬다.
    if _stt_paused(samples):
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
     lambda s, now: _is_active(s) and _stt_paused(s),
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

COUNTER_OPTIONS = {
    'ffmpeg_stale': {'suppress': _stt_paused,
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
