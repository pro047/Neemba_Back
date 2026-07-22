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


def _is_active(samples: dict) -> bool:
    return samples.get(ACTIVE, 0.0) == 1.0


def _heartbeat_stale(samples: dict, now: float) -> bool:
    if not _is_active(samples):
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
    ('stt_paused',
     lambda s, now: _is_active(s) and s.get('neemba_stt_paused', 0.0) == 1.0,
     'always',
     '🚨 STT 일시정지 상태 — 오디오 유입 없음 (Stt paused)'),
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
        if not scrape_ok:
            continue
        value = samples.get(metric, 0.0)
        prev = rule['last_value']
        rule['last_value'] = value
        if prev is None:
            continue  # 첫 관측은 기준점만 잡는다 (재시작 후 오탐 방지)
        delta = value - prev
        if delta <= 0:
            continue
        if not _is_active(samples):
            continue  # 이원화: 카운터류는 세션 활성 중에만 의미
        rule['unreported'] += delta
        if now - rule['last_alert'] >= COUNTER_COOLDOWN_SECONDS:
            alerts.append(message.format(delta=rule['unreported'], total=value))
            rule['last_alert'] = now
            rule['unreported'] = 0.0

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
        webhook, data=body, headers={'Content-Type': 'application/json'})
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
