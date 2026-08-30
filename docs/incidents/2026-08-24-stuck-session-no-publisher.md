# 2026-08-24 prod 유령 세션: publisher 미접속 세션은 닫을 방법이 없다

`stt_paused 장애 지속 중` 알림이 30분마다 반복. 장애가 아니라 **종료 트리거가
존재하지 않는 세션**이 원인이었다. 근거 등급: 별도 표기 없으면 전부 **실측**(prod
로그·메트릭·SG 조회, 2026-08-24 21:47~21:55 KST).

## 타임라인 (KST)

1. **20:03** 배포(release #111)로 전 컨테이너 재생성 (`docker inspect` Created 11:03 UTC)
2. **20:05:04** 앱 [시작] → 세션 `ec9d75a7-44c6-4d6e-83b9-ee1ecc6d7f8f` 등록,
   청취자 1명 접속 (python 로그). **운영자가 아니라 교회 쪽 사용자였다** — 사용자 확인
3. OBS(RTMP publisher)는 **한 번도 붙지 않음** — rtmp 컨테이너 3시간 로그에 publish
   0건, node 로그에 `rtmp on_publish` 0건. ffmpeg 는 `Input/output error` 로 10s→60s
   백오프 무한 재시도
4. **20:05:51** 무오디오 4연속 → STT pause, `neemba_stt_paused{ec9d75a7}=1` (node 로그)
5. **20:05:58** 청취자 이탈 + 앱 [정지] → **P1 D4 정책으로 무시** (`stop ignored` 로그)
6. 이후 세션이 영구 잔존: `active_session=1` + `stt_paused=1` → monitor 가 30분마다
   `⏰ stt_paused 장애 지속 중` (90분 경과 시점에 조사 착수)

## 근본 원인 (코드 확인)

세션 종료를 트리거하는 이벤트가 `publisherDone` **하나뿐**인데, publisher 가 애초에
안 붙은 세션은 이 이벤트가 영원히 오지 않는다.

1. auto-stop grace 타이머는 `publisherDone` 에서만 arm 된다 (`SessionLifecycle.ts:264`)
2. 앱 [정지]는 P1 D4 로 no-op (`SessionLifecycle.ts:228-234`) — join 청취자의 [정지]가
   남의 방송을 죽이는 것을 막기 위한 결정이고, 그 자체는 유효하다
3. P1 D4 설계는 "발행자가 왔다가 간" 경우와 "발행 전 OBS 테스트"(`:258-263`)는
   막았지만 **"발행자가 끝내 안 온"** 경우가 빠졌다

시간 기반 만료도 없다 — `close_stale_sessions` 는 startup 1회 DB 전용이라 허브를
안 건드린다 (핸드오프 §2-1).

## 영향

- 시청자 영향 0 (번역 0건짜리 빈 세션, stop 응답 `translationCount: 0` 으로 확정)
- 알림 채널 오염: 발생 1회 + 30분마다 리마인더 (컨테이너 재시작까지 무한)
- ffmpeg 60초 간격 무한 재시도 (STT 는 pause 라 과금 없음 — `Stt paused` 로그로 확인)
- 죽은 세션이 슬롯을 점유: 다음 [시작]은 P1 D1 멱등 join 으로 이 세션을 돌려받는다.
  단 **방송이 실제로 시작되면 자가 복구된다** (**코드 확인** — ffmpeg 재시도가 살아
  있어 오디오 유입 시 pcm 펌프가 STT 를 재개, `StreamOrchestrator.ts:138-148`.
  이 경로 자체는 이번에 실측하지 않았다)

## 수동 회수 절차 (이번에 실행·검증한 것)

```bash
# 1. python 허브 정리 — active_session 을 0 으로
docker exec monitor wget -qO- --post-data '{"sessionId":"<id>"}' \
  --header 'Content-Type: application/json' http://python:8000/internal/sessions/stop
# → {"ok":true,"ended":true,...}

# 2. node 정리 — 파이프라인·join 캐시·메트릭 시리즈 제거
docker restart node
```

**둘 다 해야 한다.** python 만 멈추면 node 의 죽은 join 캐시와 ffmpeg 재시도가 남고,
node 만 재시작하면 python `active_session=1` 이 남아 이번엔 heartbeat 오탐이 뜬다
(`stt_paused` 시리즈가 사라져 `_all_stt_paused` 억제가 풀리므로 —
`infra/monitor/monitor.py:53-77` 코드 확인). 검증: `active_session=0` ·
`stt_paused` 시리즈 0줄 · node healthy.

> **경로 주의**: 사이드카는 `infra/monitor/monitor.py` 다. `services/python/src/ws/monitor.py`
> 는 **다른 파일**이고 이 절의 억제 로직과 무관하다.

## 재발 방지 — **구현 완료 (2026-08-25)**

아래 "방향" 절은 설계 시점의 기록이고, 확정된 것은 이 절이다.

| 항목 | 확정값 | 근거 |
|---|---|---|
| 유예 | **30분** (`RTMP_NO_PUBLISHER_GRACE_SEC`) | 이 세션에는 실제 청취자가 붙어 있을 수 있다 — teardown 이 `hub.detach` 로 그 소켓을 `CLOSE_SESSION_ENDED`(4410) 로 끊고, 계약상 클라이언트는 **재시도하지 않는다**(핸드오프 §2-1). 예배 전에 앱을 켜는 정상 사용을 끊으면 안 된다. 8/24 사례는 90분이었으므로 30분도 2/3 단축이다 |
| 취소 조건 | `on_publish` **또는** pcm 도착 | 아래 §회귀 위험 절 |
| 만료 시 처리 | 창 안에 오디오 **있으면 재무장**, 없으면 종료 | 불리언 영구취소는 그 방송이 끝날 때 다시 갇힌다 |
| 종료 사유 | `no_publisher` (신설) | `publisher_done` 재사용 시 사이드카의 "방송 종료" 알림이 방송 없던 세션까지 포함해 운영 휴리스틱이 무너진다 |
| 사이드카 | `session_stopped_no_publisher` info 규칙 신설 | 없으면 운영자가 받는 유일한 신호가 `stt_paused` 시리즈 소멸로 인한 `✅ 복구` 인데, 그건 "오디오가 돌아왔다"로 읽혀 정반대다 |

**`publisher_done` 타이머에도 오디오 검사를 적용했다.** "첫 writer 승리"
가드(`SessionLifecycle.ts:289`)는 슬롯이 차 있을 때만 유효한데, node 재시작 뒤에는
슬롯이 비어 있어 거절당할 중복 OBS 가 첫 writer 가 된다 — 그 clientid 의
`on_publish` → `on_publish_done` 쌍이 살아 있는 방송에 120초 타이머를 건다.
`:283-288` 주석이 "reproduced, active_session went 1 → 0 with the real publisher
still sending" 으로 기록한 그 실패다. **이건 이번 변경이 만든 것이 아니라 원래
열려 있던 경로다.** 정상 종료 경로의 비용에는 상한이 있다 — ffmpeg 가 EOF 직후
버퍼를 토해내면 창이 한 번 더 돌고(120초 → 240초) 그다음 창에는 청크가 없다.

**"업스트림이 없으면 pcm 은 0" 은 이 인시던트가 실측했다** — 90분간
`stt_paused=1` 이 유지됐고, 청크가 하나라도 왔으면 pcm 펌프가 pause 를
풀었을 것이다(`StreamOrchestrator.ts` `_startWithSession`).

변경 파일: `SessionLifecycle.ts` · `StreamOrchestrator.ts` · `runPipeLines.ts` ·
`router/rtmp.ts` · `monitoring/metrics.ts` · `infra/monitor/monitor.py` + 회귀 8건.

## 재발 방지 방향 (설계 시점 기록)

세션 시작 시점에 publisher 슬롯이 비어 있으면 그때도 grace 타이머를 arm 한다.
기존 `publisherReturned` 가 타이머를 취소하므로 정상 순서(OBS 먼저)는 영향 없다.
마이크 세션은 별도 수명 경로(`micWebSocket.ts` 10s 유예)라 **범위 밖**이다.

#### ⚠️ 이 방향을 그대로 구현하면 방송 중인 세션이 죽는다 (2026-08-25 추가)

**`publisherReturned` 만으로는 취소 조건이 부족하다.** 타이머를 끄는 경로를 전수
확인한 결과(**코드 확인**):

```
cancelAutoStop      호출부 3곳 → teardown(:100) · scheduleAutoStop 자신(:115) · publisherReturned(:247)
publisherReturned   호출부 2곳 → router/rtmp.ts:107 · :111  (둘 다 on_publish 훅)
```

즉 **타이머를 끄는 것은 nginx-rtmp 의 `on_publish` 웹훅 하나뿐이고, 오디오가 실제로
흐르는 것은 타이머를 끄지 않는다.** `on_publish` 는 발행 **시작 시점 1회**만 발화한다.

깨지는 순서 — **이번 인시던트의 회수 절차 자체가 2번 단계다**:

1. OBS 발행 중 (`publisherClientId` 설정됨)
2. `docker restart node` → 인메모리 상태 전소 (`currentSessionId`·`publisherClientId` 둘 다 null)
3. OBS 는 계속 발행 중이지만 `on_publish` 재발화 없음
4. 앱 [시작] → 슬롯이 비어 보임 → 신규 로직이 grace arm
5. ffmpeg 재연결 → 오디오 유입 → 번역 정상 송출
6. grace 만료 → **자막이 나오는 중에 세션이 죽는다**

**대책: 취소 조건에 "오디오가 흘렀다"를 추가한다.** pcm 펌프
(`StreamOrchestrator.ts` `_startWithSession` 의 `for await (const chunk of pcmReadable)`)
가 유일한 오디오 유입 지점이고, 이 루프에 도달한 청크는 ffmpeg 가 RTMP 에서 실제로
받아낸 것이다 — 이번 인시던트처럼 upstream 이 없으면 ffmpeg 는 `Input/output error`
로 죽고 청크가 0건이다. **따라서 청크 도착은 publisher 생존의 신뢰할 수 있는 증거다.**

**부수 효과로 "유예 길이" 판단이 쉬워진다.** 지금 프레이밍에서는 유예가 "사람이
앱 누르고 OBS 켜는 시간"과 정면으로 경쟁하지만, 오디오로도 취소되면 **유예를
넉넉히 잡아도 진짜 방송은 죽지 않는다.** 죽는 것은 오디오가 끝내 안 오는 세션뿐이고
그게 정확히 잡으려는 대상이다. RTMP publisher_done 유예(120s,
`DEFAULT_PUBLISH_DONE_GRACE_SEC`)와 성격이 달라 별도 상수를 쓴다.

**불리언 대신 마지막 오디오 시각을 들 것.** 불리언이면 위 3~5번 경로에서 타이머가
영구 취소돼, 그 방송이 끝난 뒤 `on_publish_done` 도 못 받는 상황(같은 시나리오)에서
다시 닫을 수단이 없어진다. 마지막 청크 시각을 들고 만료 시 "유예 안에 오디오가
있었으면 재무장, 없으면 종료"로 하면 두 경우가 한 규칙으로 덮인다.

## 운영 참고

- 이 알림 패턴("송출 중단 ℹ️ 1회 + 30분마다 ⏰ 지속, heartbeat·ffmpeg_stale 은 침묵)이
  다시 보이면: 예배 시간대가 아니고 `session_stopped_total` 증가가 없으면 이 결함이다.
  위 회수 절차를 그대로 쓴다
- heartbeat·ffmpeg_stale 이 침묵하는 이유는 `_all_stt_paused` 억제다 (`monitor.py:73`·
  `:141`) — 단일 세션이 pause 면 "전 세션 pause" 가 참이 되므로. 알림이 조용하다고
  가벼운 상태가 아니다
- [시작]은 무인증이라 **교회 쪽 누구든 방송 없는 시간에 세션을 열 수 있다** — 이번
  발생 경위이고, 재발은 "실수"가 아니라 정상 사용 패턴이다
