#!/usr/bin/env python3
"""§4-3 라이브 재현: /ws 클라 주도 끊김 → 번역 유실 방지 검증.

2026-07-19 장애 시나리오를 그대로 모사한다:
  1. /ws?sessionId=... 접속 → NATS 전사 publish(마커 1~3) → 번역 수신 (베이스라인)
  2. 소켓을 TCP 수준에서 강제 절단(close frame 없이 abort — 망 순단 모사)
  3. 절단 직후 전사 3건(마커 4~6) publish — 수정 전엔 이 구간이 통째로 유실됐다
  4. 같은 sessionId 로 재접속 → pending flush 로 마커 4~6 이 전부 도착하면 PASS

실행 (호스트에서, dev 스택 기동 후):
  cd services/python && uv run python ../../scripts/repro_ws_disconnect.py

수정 전(구 코드) 기대 결과: FAIL — 서버 로그에 'hub: send failed: Unexpected
ASGI message ...' 가 찍히고 마커 4~6 이 재접속 후에도 오지 않는다.
수정 후 기대 결과: PASS — 끊김 즉시 'hub: client disconnected, waiting for
reconnect' → 'hub: queued send ...' → 재접속 시 'hub: flushed pending'.
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import nats
import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
WS_BASE = "ws://localhost:8080/ws"
SESSION_ID = f"repro43-{int(time.time())}"
TARGET_LANG = "en-US"


def load_nats_url() -> str:
    env_path = REPO_ROOT / ".env.dev"
    for line in env_path.read_text().splitlines():
        if line.startswith("NATS_URL="):
            url = line.split("=", 1)[1].strip()
            # 컨테이너 호스트명(nats) → 호스트 노출 포트(localhost:4222)
            return url.replace("@nats:", "@localhost:")
    raise SystemExit(".env.dev 에서 NATS_URL 을 찾지 못했습니다")


def ensure_session_row() -> None:
    """python 컨테이너 안에서 /internal/sessions/start 호출 (DB 세션 행 생성).

    nginx 는 /internal 을 노출하지 않으므로 docker exec 로 우회한다. 실패해도
    허브 검증 자체에는 영향이 없어 non-fatal.
    """
    code = (
        "import json,urllib.request;"
        f"body=json.dumps({{'sessionId':'{SESSION_ID}','sourceLang':'ko-KR',"
        f"'targetLang':'{TARGET_LANG}'}}).encode();"
        "req=urllib.request.Request('http://localhost:8000/internal/sessions/start',"
        "data=body,headers={'Content-Type':'application/json'});"
        "print(urllib.request.urlopen(req,timeout=5).status)"
    )
    try:
        subprocess.run(
            ["docker", "exec", "python", "python3", "-c", code],
            check=True, capture_output=True, timeout=15,
        )
    except Exception as e:
        print(f"[warn] ensure_session 실패(무시하고 진행): {e}")


def stop_session_row() -> None:
    code = (
        "import json,urllib.request;"
        f"body=json.dumps({{'sessionId':'{SESSION_ID}'}}).encode();"
        "req=urllib.request.Request('http://localhost:8000/internal/sessions/stop',"
        "data=body,headers={'Content-Type':'application/json'});"
        "print(urllib.request.urlopen(req,timeout=5).status)"
    )
    try:
        subprocess.run(
            ["docker", "exec", "python", "python3", "-c", code],
            check=True, capture_output=True, timeout=15,
        )
    except Exception as e:
        print(f"[warn] stop_session 실패(무시): {e}")


async def publish_markers(js, start: int, count: int) -> None:
    for n in range(start, start + count):
        payload = {
            "sessionId": SESSION_ID,
            "segmentId": n,
            "sequence": n,
            "transcriptText": f"이것은 {n}번 문장입니다.",
            "targetLanguage": TARGET_LANG,
            "sourceLanguage": "ko",
        }
        await js.publish(
            f"transcript.session.{SESSION_ID}", json.dumps(payload).encode()
        )
    print(f"[pub] 마커 {start}~{start + count - 1} publish 완료")


async def collect_until(ws, markers: set[str], timeout_s: float) -> tuple[set[str], list[str]]:
    """markers 의 숫자가 전부 보이거나 timeout 까지 수신 텍스트를 모은다."""
    seen: set[str] = set()
    received: list[str] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while markers - seen:
        remain = deadline - loop.time()
        if remain <= 0:
            break
        try:
            msg = str(await asyncio.wait_for(ws.recv(), remain))
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            break
        received.append(msg)
        for m in markers:
            if m in msg:
                seen.add(m)
    return seen, received


async def main() -> int:
    print(f"[repro] session={SESSION_ID}")
    ensure_session_row()

    nc = await nats.connect(load_nats_url())
    js = nc.jetstream()

    # --- 1. 베이스라인: 정상 연결에서 번역이 흐르는지 ---------------------
    ws = await websockets.connect(f"{WS_BASE}?sessionId={SESSION_ID}")
    await asyncio.sleep(1)
    await publish_markers(js, 1, 3)
    seen, received = await collect_until(ws, {"1", "2", "3"}, 30)
    if seen != {"1", "2", "3"}:
        print(f"[FAIL-환경] 베이스라인 수신 실패 seen={seen} recv={received}")
        print("  → dev 스택/DeepL/NATS 자격증명(.env.dev)을 먼저 확인하세요")
        await nc.close()
        return 2
    print(f"[ok] 베이스라인 수신 {len(received)}건")

    # --- 2. 망 순단 모사: close frame 없이 TCP abort ----------------------
    transport = getattr(ws, "transport", None)
    if transport is not None:
        transport.abort()
        print("[cut] TCP abort (close frame 없음)")
    else:
        await ws.close()
        print("[cut] graceful close (transport 미노출 폴백)")

    # --- 3. 끊긴 사이 번역 생산 — 유실 위험 구간 --------------------------
    await asyncio.sleep(0.5)
    await publish_markers(js, 4, 3)
    await asyncio.sleep(3)  # 서버가 큐잉할 시간

    # --- 4. 재접속 → pending flush 검증 ----------------------------------
    ws2 = await websockets.connect(f"{WS_BASE}?sessionId={SESSION_ID}")
    seen2, received2 = await collect_until(ws2, {"4", "5", "6"}, 30)
    await ws2.close()
    await nc.close()
    stop_session_row()  # keepalive 가 5분간 재연결을 기다리지 않게 정리

    print(f"[recv-after-reconnect] {len(received2)}건: {received2}")
    if seen2 == {"4", "5", "6"}:
        print("[PASS] 끊김 구간의 번역 3건이 모두 재접속 후 방류됨 — 유실 0")
        print("  서버 로그 확인: docker logs python --since 5m 2>&1 | "
              "grep -E 'client disconnected|queued send|re-queued|flushed'")
        return 0
    print(f"[FAIL] 유실 발생 — 도착한 마커: {seen2}")
    print("  docker logs python --since 5m 2>&1 | grep 'send failed' 로 에러 확인")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
