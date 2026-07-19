#!/usr/bin/env python3
"""§4-4-2 라이브 재현: 오디오 중단 → STT 일시정지 → 오디오 재유입 → 재개 검증.

2026-07-19 장애의 'STT 좀비화'를 mic 경로로 모사한다 (RTMP 경로와 동일한
StreamOrchestrator 를 지나므로 검증 대상 코드는 같다):
  1. POST /api/mic/start → sessionId
  2. ws /api/mic?sessionId=... 접속, 무음 PCM(16kHz mono 16bit) 8초 전송
  3. 소켓은 유지한 채 전송만 중단 → Google STT 가 ~10초마다 타임아웃 에러
     → 4연속 후 node 로그 'Stt paused: ... pausing rotation until audio returns'
     (수정 전 구 코드: 'Stt giving up' 후 영구 좀비 — 재개 불가)
  4. 무음 PCM 재전송 → 'Stt resuming: audio returned' 로그 = PASS
  5. POST /api/mic/stop 정리

Google STT 자격증명(.env.dev)이 유효해야 한다. 로그는 스크립트가 docker logs
node 를 폴링해 자동 판정한다.

실행 (호스트에서, dev 스택 기동 후):
  cd services/python && uv run python ../../scripts/repro_stt_pause_resume.py
"""
import asyncio
import json
import subprocess
import sys
import time
import urllib.request

import websockets

BASE = "http://localhost:8080"
WS_BASE = "ws://localhost:8080/api/mic"
FRAME = bytes(3200)  # 100ms of 16kHz 16bit mono silence
START_TS = time.strftime("%Y-%m-%dT%H:%M:%S")


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode() or "{}")


def find_session_id(obj) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "sessionId" and isinstance(v, str):
                return v
            found = find_session_id(v)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = find_session_id(item)
            if found:
                return found
    return None


def node_log_has(pattern: str) -> bool:
    out = subprocess.run(
        ["docker", "logs", "node", "--since", START_TS],
        capture_output=True, text=True, timeout=15,
    )
    return pattern in (out.stdout + out.stderr)


async def send_silence(ws, seconds: float) -> None:
    for _ in range(int(seconds * 10)):
        await ws.send(FRAME)
        await asyncio.sleep(0.1)


async def wait_for_log(pattern: str, timeout_s: int, label: str) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if node_log_has(pattern):
            print(f"[ok] {label}: '{pattern}' 로그 확인")
            return True
        remain = int(deadline - time.time())
        print(f"[wait] {label} 대기 중... (남은 {remain}s)")
        await asyncio.sleep(10)
    print(f"[FAIL] {timeout_s}s 안에 '{pattern}' 로그가 나타나지 않음")
    return False


async def main() -> int:
    started = post("/api/mic/start", {"sourceLang": "ko-KR", "targetLang": "en-US"})
    session_id = find_session_id(started)
    if not session_id:
        print(f"[FAIL-환경] mic/start 응답에서 sessionId 를 못 찾음: {started}")
        return 2
    print(f"[repro] session={session_id}")

    try:
        ws = await websockets.connect(f"{WS_BASE}?sessionId={session_id}")

        # 1. 오디오 흐름 확립 (STT 스트림이 살아있음을 보장)
        print("[audio] 무음 8초 전송")
        await send_silence(ws, 8)

        # 2. 소켓 유지 + 전송 중단 → 연속 타임아웃 → 일시정지
        #    (websockets 라이브러리가 node 의 15s heartbeat ping 에 자동 pong)
        print("[cut] 오디오 전송 중단 (소켓은 유지) — STT 타임아웃 유도")
        paused = await wait_for_log(
            "Stt paused", 180, "일시정지(§4-4-2 수정 동작)"
        )
        if not paused:
            if node_log_has("Stt giving up"):
                print("[FAIL] 구 코드 동작 감지: 'Stt giving up' (영구 좀비)")
            return 1

        # 3. 오디오 재유입 → 재개
        print("[audio] 무음 재전송 — 재개 유도")
        resume_task = asyncio.create_task(send_silence(ws, 15))
        resumed = await wait_for_log("Stt resuming", 60, "재개")
        resume_task.cancel()
        await ws.close()
        return 0 if resumed else 1
    finally:
        try:
            post("/api/mic/stop", {"sessionId": session_id})
            print("[cleanup] mic/stop 완료")
        except Exception as e:
            print(f"[warn] mic/stop 실패: {e}")


if __name__ == "__main__":
    code = asyncio.run(main())
    print("[PASS] STT 일시정지→재개 검증 성공" if code == 0 else "[결과] 실패 — node 로그 확인 필요")
    sys.exit(code)
