"""node /metrics 수집기 — ``GET /api/monitor/status`` (WU5) 전용.

사이드카(``infra/monitor/monitor.py`` ``fetch_metrics``)와 같은 Prometheus
텍스트 라인 파싱이되, 비동기(httpx) + 상태 개요가 쓰는 게이지 3개만
추출한다. ``stt_paused``·``publish_buffer_size`` 는 세션별
``sessionId`` 라벨 시리즈로 오므로 이름별 **max 로 집계**해 기존 단일값
계약을 유지한다 (stt_paused 는 any-of, buffer 는 세션 중 최댓값). 경보
**판정**은 계속 사이드카 책임 — 여기는 현재값 표시용 수집만 한다
(monitor-page-v2 불변 제약).

모든 실패(연결 불가·타임아웃·비 2xx)는 ``None`` 반환 — 라우트가
``nodeUp:false`` 로 표시한다. 예외를 밖으로 내보내지 않는다.
"""
from __future__ import annotations

import asyncio
import math
import os

import httpx

DEFAULT_NODE_METRICS_URL = 'http://node:3000/metrics'

# httpx 의 timeout 은 단계별(connect·read·write·pool) 상한이라 조금씩
# 흘려보내는 응답이면 합산 지연이 커질 수 있다 — 전체 데드라인은
# asyncio.timeout 으로 따로 건다. status 응답 지연 상한 = _TOTAL 쪽.
_PHASE_TIMEOUT_SECONDS = 2.0
_TOTAL_TIMEOUT_SECONDS = 3.0

# 대상 게이지 3개 (계획 §3 WU5). stt_paused·publish_buffer_size 는 라벨드
# 시리즈, rtmp_auth_enabled 는 라벨 없음 — 파서는 둘 다 같은 규칙으로 다룬다.
_LABELLED_GAUGES = frozenset((
    'neemba_stt_paused',
    'neemba_publish_buffer_size',
))
_TARGET_GAUGES = _LABELLED_GAUGES | frozenset(('neemba_rtmp_auth_enabled',))


def node_metrics_url() -> str:
    return os.environ.get('NODE_METRICS_URL') or DEFAULT_NODE_METRICS_URL


def parse_gauges(text: str) -> dict[str, float]:
    """Prometheus 텍스트에서 대상 게이지를 추출 (사이드카 파서와 같은 규칙).

    라벨드 시리즈(``name{sessionId="..."} v``)는 라벨을 벗기고 이름별
    **max** 로 집계한다 — 0/1 게이지에는 any-of, 수치 게이지에는 세션 중
    최댓값. 라벨드 게이지는 세션이 없으면 시리즈가 0줄이므로, ``# TYPE``
    라인이 있으면(게이지 자체는 노출됨) 기본값 0.0 을 깐다. 대상 게이지가
    패밀리째 없으면 그 키는 빠진다 — 라우트는 빠진 키를 ``null`` 필드로
    내려보낸다 (node 는 떠 있지만 해당 게이지 미노출).

    비유한 샘플(NaN/Inf)은 max 로 집계할 수 없다 — 하나라도 섞이면 그 이름은
    ``nan`` 으로 고정해 ``gauge_bool``/``gauge_int`` 가 ``None``(알 수 없음)
    을 내도록 한다. max 에 맡기면 ``max(0.0, nan) == 0.0`` 이라 '알 수 없음'
    이 '정상 0' 으로 둔갑하고, 결과가 라인 순서에도 의존한다.

    한계: 라벨 값 안의 공백은 지원하지 않는다(uuid 라벨 전제) — 사이드카
    파서와 같은 제약.
    """
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith('# TYPE '):
            parts = line.split()
            # 시딩은 라벨드 게이지 한정: 라벨 없는 게이지는 TYPE 다음 줄에
            # 샘플이 반드시 오므로, 시딩하면 잘린 응답에서 '미상(null)' 이어야
            # 할 값을 단정적 0.0 (예: rtmp 인증 꺼짐) 으로 보고하게 된다.
            if len(parts) >= 3 and parts[2] in _LABELLED_GAUGES:
                values.setdefault(parts[2], 0.0)
            continue
        if line.startswith('#') or ' ' not in line:
            continue
        key, _, rest = line.partition(' ')
        name = key.partition('{')[0]
        if name not in _TARGET_GAUGES:
            continue
        # 값 뒤에 optional timestamp 가 붙을 수 있다 ("name 1 1690000000000") —
        # 첫 토큰만 값으로 취한다.
        parts = rest.split()
        if not parts:
            continue
        try:
            value = float(parts[0])
        except ValueError:
            continue
        previous = values.get(name)
        # 한 번 nan 이 되면 뒤 샘플로 되돌리지 않는다 — 순서 의존 제거.
        if not math.isfinite(value) or (
            previous is not None and not math.isfinite(previous)
        ):
            values[name] = math.nan
            continue
        values[name] = value if previous is None else max(previous, value)
    return values


def gauge_bool(value: float | None) -> bool | None:
    """0/1 게이지 → bool. 부재·비유한값(NaN/Inf)은 None ('알 수 없음' 표시)."""
    if value is None or not math.isfinite(value):
        return None
    return value >= 1.0


def gauge_int(value: float | None) -> int | None:
    """수치 게이지 → int. 부재·비유한값(NaN/Inf)은 None — int(NaN) 예외 방지."""
    if value is None or not math.isfinite(value):
        return None
    return int(value)


async def fetch_node_gauges(url: str | None = None) -> dict[str, float] | None:
    """node /metrics 를 읽어 대상 게이지 dict 를 반환. 모든 실패는 ``None``."""
    target = url or node_metrics_url()
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(timeout=_PHASE_TIMEOUT_SECONDS) as client:
                res = await client.get(target)
                res.raise_for_status()
    except Exception:
        return None
    return parse_gauges(res.text)
