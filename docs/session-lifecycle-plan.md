# 세션 수명주기 정리 계획 (세션 핸드오프 문서)

작성: 2026-07-30 (prod 로그 모니터링 세션에서 발견)
선행 문서: `docs/monitor-page-v2-plan.md` (WU1~WU5, 별개 작업)

---

## 1. 배경 — 무엇이 발견됐나

2026-07-29 예배 방송(10:34~11:21 UTC)을 실시간으로 모니터링하다가, 방송이
끝난 뒤 디스코드 경보가 멎지 않는 것을 발견했다. 25시간 뒤인 7/30 13:00 UTC
까지 10분·30분 간격으로 계속 발화하고 있었다.

```
🚨 ffmpeg 10초 무진행 95~97회 — RTMP 수신 정체 의심     (10분 간격)
⏰ stt_paused 장애 지속 중 (1501분 51초)                (30분 간격)
⏰ heartbeat 장애 지속 중 (1501분 51초)                 (30분 간격)
```

1501분 = 25시간 1분. 21:23 KST에서 거슬러 올라가면 7/29 20:22 KST — 방송이
끊긴 시각(11:21 UTC = 20:21 KST)과 일치한다.

### 근인

**방송이 끝나도 세션을 닫는 주체가 없다.** RTMP 경로에서 파이썬 세션을
종료시키는 건 `POST /api/sessions/stop` 하나뿐이고, 이건 HTTP 엔드포인트다.
스트림이 끊겼다고 자동 호출되는 경로가 없다 (`rtmp.ts:53-67`의 정리 로직은
*다음* 세션이 시작될 때 이전 걸 치우는 것이라 해당 없음).

연쇄:

1. OBS 송출 중단
2. 아무도 stop 을 부르지 않음 → python hub `active_session` 이 1로 고정
3. node 의 ffmpeg 가 없는 publisher 를 향해 60초 백오프로 무한 재시도
4. 매 사이클 `not updated for 10s` 를 여러 번 찍어 카운터가 10분에 ~95씩 증가
5. `ffmpeg_stale` 은 쿨다운(600초)마다, `heartbeat`·`stt_paused` 는 조건이
   latch 되어 `REMINDER_SECONDS`(1800초)마다 반복 발화

### 상시 문제다

DB 에 고아 세션이 4개 있었다 (조치 전):

| session_id | started_at | 방치 기간 |
|---|---|---|
| 7eb714fc-65a9-4a84-881a-7b4a4c3f82c9 | 2026-07-15 10:38 UTC | 15일 |
| 29a5a3cc-1083-45ac-bd26-4c4e974bfd9f | 2026-07-19 02:45 UTC | 11일 |
| f152c706-72da-488e-8f84-0401f5d53d80 | 2026-07-22 10:27 UTC | 8일 |
| 2c2d72c8-2e10-4f2a-a167-977ea9394db1 | 2026-07-29 10:34 UTC | 1일 (조치 완료) |

7/15·7/19·7/22 방송도 전부 종료되지 않았다. 그때 알림 폭풍이 안 온 이유는
7/26 컨테이너 재시작이 메모리 상태를 지웠기 때문이고, DB 행만 계속 쌓였다.

---

## 2. 확정 사실 (2026-07-30 prod 실측)

조치 전 지표:

```
neemba_hub_active_session      1.0          ← 25시간째 활성
neemba_hub_last_broadcast_...  1785324068   ← 7/29 11:21 UTC 이후 번역 0건
neemba_stt_paused              1
neemba_ffmpeg_stale_total      14741        ← 계속 증가
neemba_rtmp_auth_enabled       1
```

- **비용은 0이었다.** STT 가 pause 상태라 Google STT 를 호출하지 않고 DeepL
  호출도 없다. CPU 도 node 0.63% / python 0.17% 로 무시할 수준. 실제 피해는
  알림 소음뿐이지만, 이게 계속되면 진짜 장애 알림이 묻힌다.
- **RTMP 인증은 켜져 있다.** 키를 검증하는 건 rtmp 컨테이너가 아니라 node 다
  (`on_publish` → `http://node:3000/api/rtmp/on-publish`). `rtmp` 컨테이너에
  `RTMP_PUBLISH_KEY` 가 없는 건 정상이다.
- **`rtmp_auth_disabled` 일일 경보는 오탐이었다.** `neemba_rtmp_auth_enabled`
  게이지를 `setRtmpAuthEnabled` 로 세우는 곳이 `on_publish` 핸들러 안뿐이라
  (`rtmp.ts:28`), node 재시작 후 첫 publish 전까지 prom-client 기본값 0 이
  노출된다. 7/29 10:33 첫 publish 로 1이 된 뒤 13:29 UTC 일일 틱에서 경보가
  안 떴다 — 이 설명의 반증 조건을 통과했다.
- **WS 순단 처리는 정상이다.** 7/29 10:53:23 에 51초 순단이 있었고 pending
  18건이 재접속 후 전량 flush 됐다(유실 0). `app.ws_blips` 에도 기록됐다.

### prod 는 develop 보다 5커밋 뒤처져 있다

```
origin/main  = 2fa0c06 (release #57) = WU1 + PR #56(ws_blips)
origin/develop = 41ba2b6 = + WU2, WU3, WU4
```

**`close_stale_sessions` 가 prod 에 없다.** 이 함수는 lifespan startup 에서
`ended_at IS NULL` 인 세션을 **전부** 닫는다(D4-a, WU2 에서 추가). prod 에
있었다면 7/26 재시작 때 고아 3개가 정리됐을 것이다. 이것이 고아가 살아남은
이유이고, **동시에 release 배포만으로 기존 고아 3개가 자동 정리된다는 뜻이다.**

---

## 3. 확정된 결정

| # | 결정 | 근거 |
|---|---|---|
| D1 | publisher 종료 후 **120초 유예** 뒤 세션 자동 종료 | 7/29 관측된 WS 순단이 51초, ffmpeg 재시작 백오프가 20~60초 단위. 120초면 일반적 네트워크 순단을 덮으면서 종료 판정은 충분히 빠르다 (사용자 승인 2026-07-29) |
| D2 | 기존 고아 3개는 수동 UPDATE 하지 않는다 | release 배포 시 `close_stale_sessions` 가 startup 에서 자동 정리 |
| D3 | 즉시 조치는 `POST /api/sessions/stop` 로 수행 | 설계된 정상 경로. 재시작 없이 active=0·ffmpeg 종료·DB `ended_at` 까지 한 번에 되돌아감 (2026-07-30 실행 완료) |

---

## 4. 작업 단위

의존: WU-A 는 독립. WU-B 는 지금 바로. WU-C 는 release 에 종속. WU-D 는 독립.

### WU-A — [rtmp+node] publisher 종료 시 세션 자동 종료 **(1순위)**

이 계획의 본체. 근인을 없앤다.

**시작 전 반드시 확인할 것 — 디렉티브 이름**

`on_done` 을 쓰면 **안 된다.** node 의 내부 ffmpeg 는 같은 `application live`
의 **player** 로 붙는다(`rtmp/nginx.conf` 주석: "Play (node's internal ffmpeg
pull) is NOT gated"). `on_done` 은 play 종료에도 발화하므로 ffmpeg 재시도마다
세션이 죽는다. publisher 종료만 잡는 디렉티브(nginx-rtmp 기준
`on_publish_done` 으로 알려져 있음)를 **모듈 문서·실제 설정으로 먼저 확인**한
뒤 진행할 것. 확인 전에는 구현하지 말 것.

**변경 파일**

1. `infra/rtmp/nginx.conf` — `application live` 안에 publisher 종료 콜백 추가.
   호스트가 config parse 시점에 해석되는 제약은 기존과 동일하므로
   `depends_on: node` 로 이미 충족된다. 콜백 응답 코드는 무시된다는 점을
   확인할 것(알림용이라 2xx 강제가 아님).

2. `services/node/src/router/rtmp.ts`
   - 새 라우트 `POST /rtmp/on-publish-done`
   - **즉시 종료하지 않는다.** 유예 타이머를 건다.
   - 종료 로직을 함수로 추출한다. 현재 teardown 이 `/sessions/start` 의 이전
     세션 정리(53-67)와 `/sessions/stop`(112-150) **두 곳에 중복**돼 있고,
     타이머 경로가 붙으면 셋이 된다. 반드시 하나로 뽑고 세 경로가 그것을
     호출하게 할 것.

**반드시 처리할 레이스** — 이게 이 작업의 실질적 난이도다

| 상황 | 요구 동작 |
|---|---|
| 유예 중 `on_publish` 재수신 (publisher 복귀) | 타이머 취소, 세션 유지 |
| 유예 중 새 세션 start | 옛 타이머가 **새 세션을 죽이면 안 됨** — 타이머 생성 시 sessionId 를 캡처하고 만료 시 `currentSessionId` 와 비교 |
| 유예 중 수동 stop | 타이머 취소 (만료 시 no-op 이어도 무방하나 명시적으로) |
| `on_publish_done` 중복 수신 (flapping) | 기존 타이머 clear 후 재설정 |
| 유예 중 node 프로세스 재시작 | 타이머 유실 — 메모리 상태도 함께 초기화되므로 알림은 멎는다. DB 행은 다음 python startup 의 `close_stale_sessions` 가 정리. 문서화만 하고 별도 대응 없음 |

**설정값**: 유예 초를 env 로 노출 (`RTMP_PUBLISH_DONE_GRACE_SEC`, 기본 120).
교회 네트워크 사정에 따라 조정 가능해야 한다.

**계측**: 자동 종료가 발화하면 카운터를 하나 올릴 것(수동 stop 과 구분).
"운영자가 stop 을 누르지 않는 빈도"를 알 수 있어야 이 기능의 효과를 잰다.

**테스트** (`services/node/test/`, vitest, fake timers)

1. 유예 안에 publisher 가 돌아오면 세션을 유지해야 한다
2. 유예가 지나면 파이프라인 정리와 python stop 을 호출해야 한다
3. 유예 중 새 세션이 시작되면 옛 타이머가 새 세션을 종료하지 않아야 한다
4. 수동 stop 이후 타이머가 만료돼도 아무 일도 하지 않아야 한다

기존 `test/rtmpOnPublish.test.ts` 가 on_publish 라우트 테스트 패턴을 갖고
있으니 그 형태를 따를 것.

**완료 기준** — dev compose 에서:
- ffmpeg 로 publish → 중단 → 120초 후 `neemba_hub_active_session` 0, node
  내부 ffmpeg 프로세스 0개, DB `ended_at` 기록
- 중단 후 60초 시점에 다시 publish → 세션이 유지되고 번역이 이어짐

---

### WU-B — PR #60 머지 **(2순위, 코드 작업 없음)**

`fix: 방송 종료를 장애로 오인하는 monitor 경보 차단`
(https://github.com/pro047/Neemba_Back/pull/60)

`neemba_stt_paused` 를 '정상 무오디오' 신호로 승격해 `heartbeat` 를 게이트하고
`ffmpeg_stale` 을 90초 유예 후 억제한다.

**WU-A 와 보완 관계다.** WU-A 가 들어가도 "오디오 끊김 ~ publish_done 도착 +
유예" 구간의 `ffmpeg_stale` 은 남으므로 #60 이 그 구간을 덮는다. 반대로 #60
만으로는 세션이 계속 열린 채 남고 고아가 계속 쌓인다.

**배포 후 즉시 확인할 것**: `docker logs monitor` 로 첫 틱이 정상인지.
`state.json` 이 디스크에 영속되는데 기존 상태에는 `pending_since` 키가 없다.
`setdefault` 로 읽게 해뒀지만 실배포에서 한 번은 눈으로 볼 것.

---

### WU-C — 고아 세션 정리 **(3순위, release 에 종속)**

**대부분 자동으로 해결된다.** release(develop→main) 배포 시 python startup 의
`close_stale_sessions` 가 `ended_at IS NULL` 인 3개를 전부 닫는다. 수동 DB
UPDATE 불필요 (D2).

남는 설계 질문 하나: `close_stale_sessions` 는 **startup 1회**만 실행된다.
프로세스가 장기 구동되는 동안 생기는 고아는 치우지 못한다. WU-A 가 들어가면
신규 고아가 거의 안 생기므로 그대로 둬도 되지만, 유예 중 재시작 같은 틈은
남는다. 주기 실행(예: 1시간)으로 바꿀지는 §5 열린 질문.

**배포 후 확인**: `SELECT count(*) FROM app.sessions WHERE ended_at IS NULL`
이 0 인지. 그리고 lifespan 로그에 정리된 session id 3개가 찍히는지.

---

### WU-D — [node] 게이지 종료 경로 **(4순위)**

오늘 같은 유형의 결함이 두 개 더 확인됐다. **성공 경로에서만 상태를 세우고
종료 경로에서 내리지 않는** 패턴이다. 둘 다 node, 작아서 한 PR 로 묶는다.

1. **`setSttPaused(false)` 가 종료 경로에 없다.**
   `StreamOrchestrator.ts:109` 에서 "오디오가 돌아왔을 때"만 false 로 되돌린다.
   세션이 종료돼도 `neemba_stt_paused` 는 1로 남는다 (7/30 실측: 세션 stop 후에도 1).
   stop closure(`StreamOrchestrator.ts:119-127`)에 추가할 것.

2. **`setRtmpAuthEnabled` 부팅 초기화가 없다.**
   호출처가 `rtmp.ts:28` 하나뿐이라 재시작 후 첫 publish 전까지 게이지가 0 →
   monitor 가 매일 "RTMP 인증 꺼짐" 오탐을 낸다. **배포할 때마다 재발한다.**
   `app.ts` 의 `createApp()` 안, dotenv 로드 이후에 1회 호출:
   `setRtmpAuthEnabled(Boolean(process.env.RTMP_PUBLISH_KEY))`

   **함정**: 모듈 import 시점(파일 최상단)에 넣으면 dotenv 보다 먼저 실행돼
   `undefined` 를 읽는다. 반드시 dotenv 이후여야 한다.

---

### WU-E — 통합 리뷰 → release

`docs/monitor-page-v2-plan.md` §4-5 가 요구하는 WU1~WU5 통합 리뷰.
**구현과 다른 세션에서** 수행할 것 (자기검증 편향 차단).

```
neemba/docs/monitor-page-v2-plan.md 읽고 WU1~WU5 통합 리뷰해
```

그다음 release PR (develop → main). 배포 시:
- SSH SG 룰을 임시로 열어야 함 (§6 참조)
- monitor 컨테이너 재생성 후 첫 틱 확인 (WU-B)
- `ended_at IS NULL` 이 0 인지 확인 (WU-C)

---

## 5. 열린 질문 (다음 세션에서 사용자 결정)

1. **자동 종료 시 디스코드 알림을 보낼까?** 정보성(ℹ️)으로 "방송 종료 감지 —
   세션 자동 종료" 한 줄. 운영자가 stop 을 안 눌렀다는 사실 자체가 신호일 수
   있다. 다만 매 방송마다 오면 그것도 소음이다.
2. **`close_stale_sessions` 를 주기 실행으로 바꿀까?** startup 1회 → 1시간
   간격. WU-A 가 들어가면 필요성이 크게 줄지만 안전망은 된다.
3. **WU-A 를 #60·#61 과 같은 release 에 묶을까, 따로 갈까?** 묶으면 배포가
   한 번이지만 문제 생겼을 때 원인 분리가 어렵다.
4. **리뷰 잔여 8건 중 순단 탭 페이지네이션 2건을 이 릴리스에 넣을까?**
   순단 이력은 장애 조사용인데 행이 중복되거나 최신 50건이 누락되면 조사를
   오도한다. 나머지 6건보다 우선순위가 높다.

---

## 6. 참고 — prod 접근

SSH 는 보안그룹에 `/32` 로 핀돼 있다. 현재 인바운드는 `220.124.99.7/32
(operator ssh pin)` 하나뿐이고, 접속하려면 **현재 IP 를 임시 룰로 추가한 뒤
작업이 끝나면 회수**해야 한다.

```bash
IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --region ap-northeast-2 \
  --group-id sg-0b0429634f7e70af2 \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$IP/32,Description=\"temp ssh\"}]"
# ... 작업 ...
aws ec2 revoke-security-group-ingress --region ap-northeast-2 \
  --group-id sg-0b0429634f7e70af2 --security-group-rule-ids <반환된 sgr-id>
```

SSM 에이전트는 미등록이고 이 인스턴스의 CloudWatch 로그 그룹도 없다 —
**SSH 가 유일한 경로다.** 키는 `~/Downloads/neemba.pem`
(`~/.ssh/neemba-key.pem` 은 Permission denied).

로그 델타 모니터링에 쓴 스크립트 패턴: 원격에서 `docker logs -f` 를 컨테이너별
프리픽스와 함께 한 SSH 스트림으로 합쳐 로컬 파일에 append 하고, 오프셋 기준
델타만 읽는다. **`sed -u` 필수** — 그냥 `sed` 면 블록 버퍼링 때문에 저용량
스트림(python)이 버퍼에 갇혀 2분씩 늦게 나온다.

---

## 7. 진행 상태

| WU | 상태 | 비고 |
|----|------|------|
| 즉시 조치 | 완료 (2026-07-30) | `2c2d72c8` stop 호출 → active=0, ffmpeg 종료, DB `ended_at` 기록. 컨테이너 5개 healthy 확인 |
| WU-A | **구현·dev 검증 완료** (2026-07-30) | `feature/rtmp-publish-done`. 아래 §10 참조 |
| WU-B | **완료** (2026-07-30) | PR #60 develop 머지 (`5faed0c`) |
| WU-C | release 대기 | 배포만 하면 자동 정리 |
| WU-D | 미착수 | |
| WU-E | 미착수 | 통합 리뷰는 별도 세션 |

## 8. 미확인 항목

- ~~**`translation_count` 가 0 인 것이 버그인지 미확인.**~~ → **해소 (버그 아님).**
  2026-07-30 dev 실측으로 세 경우를 대조했다: 진행 중 세션 `65566942` = 0,
  정상 종료 + 오디오 있음 `7919277f` = **20**, 정상 종료 + 오디오 없음
  `78eb424a` = 0. count 는 종료 시 recompute 되므로 **열린 세션이 0인 건
  정상**이고, 고아 3개가 0인 것도 정상이다.
- ~~**`on_publish_done` 디렉티브 이름 미검증**~~ → **해소.** §10 참조.
- prod 의 `/api/sessions/stop` 은 nginx `/api/` 블록을 타므로 Basic Auth 가
  걸리지 않는다 (`/api/monitor/` 만 인증). 인터넷에서 세션 종료를 호출할 수
  있다는 뜻 — 별개 검토 대상으로 기록만 해둔다.

## 9. 세션 로그

- 2026-07-29~30 (prod 모니터링 세션): 예배 방송을 5분 간격으로 실시간 모니터링
  하다가 종료 후 경보가 멎지 않는 것을 발견. 원인 규명·즉시 조치·본 문서 작성.
  같은 세션에서 PR #60(monitor 경보 정상 종료 구분), PR #61(monitor-v2 WU5)
  생성. 다음 세션: `neemba/docs/session-lifecycle-plan.md 읽고 WU-A 진행해`
- 2026-07-30 (WU-A 세션): 디렉티브 실측 검증 → 구현 → dev 라이브 검증 → PR #60
  머지 → rebase. 다음 세션: WU-D, 그다음 WU-E 통합 리뷰 → release.

---

## 10. WU-A 검증 기록 (2026-07-30)

### 디렉티브 — 실측으로 확정

`alfg/nginx-rtmp` (nginx-rtmp-module **1.2.2**) 컨테이너에 네 훅을 모두 걸고
publisher/player 를 각각 붙였다 뗐다.

| 행위 | 발화 훅 |
|---|---|
| publish 시작 | `on_publish` (`call=publish`, `key=…` 전달됨) |
| player 접속 | `on_play` |
| **player 종료** | `on_play_done` **만** — `on_publish_done` 은 안 뜸 |
| publisher 종료 | `on_publish_done` (`call=publish_done`, `key`·`clientid` 전달됨) |

반증 통제: 가짜 이름 `on_publish_finished` 는 `[emerg] unknown directive` 로
죽는다 → parse 통과가 곧 디렉티브 존재의 증거다.

§4 가 우려한 "`on_done` 은 play 종료에도 발화" 는 `on_publish_done` 에는 해당
없음이 확인됐다. **부수 소득**: `publish_done` payload 에 `key` 와 `clientid` 가
실려 온다 — 각각 인증과 아래 레이스 방어에 쓴다.

### 문서에 없던 레이스 하나 추가

| 상황 | 요구 동작 |
|---|---|
| **publish / publish_done 순서 역전** — OBS 재접속으로 새 소켓의 `on_publish` 가 먼저 오고, 죽은 옛 연결의 `publish_done` 이 뒤늦게 도착 | 마지막 허용된 `on_publish` 의 `clientid` 를 기억해뒀다가 다르면 무시. 없으면 살아있는 방송을 유예 후 죽인다 |

### dev 라이브 검증 결과

**1차 (유예 120초 = 기본값)**

| 시각 | 사건 | ffmpeg | auto_stopped |
|---|---|---|---|
| 13:54:06 | publisher 중단 | 1 | 0 |
| 13:55:01 | 중단 +55s | **1** | **0** (유예 내 생존) |
| 13:55:01 | publisher 복귀 → `ffmpeg restart: publisher returned` | 1 | 0 |
| 13:55:22 | 재중단 | 1 | 0 |
| **13:57:22** | 자동 종료 (**정확히 120초**) | **0** | **1** |

DB `ended_at` 기록, `ended_at IS NULL` = 0, monitor 디스코드 ℹ️ 1회.

**2차·3차 (유예 20초, `RTMP_PUBLISH_DONE_GRACE_SEC` 노브 검증)**

WS 클라이언트를 붙여 `neemba_hub_active_session` 을 실제로 1로 만든 뒤:
+12초 `active=1`, +28초 **`active=0`** · ffmpeg 0 · 카운터 증가.
python 로그에 `hub: detached` → `set_active_session(False)` 까지 확인.
한국어 음성 → STT → DeepL → WS 로 실제 번역 도달 (`We sincerely welcome
everyone who has come to today's service.`).

### 알아둘 것 — `neemba_hub_active_session` 의 의미

이 게이지는 **세션 시작이 아니라 WS 클라이언트 접속** 시점에 1이 된다
(`ws/websocket.py:64` `attach`), 내려가는 건 `detach`(:113). 즉 사건 당시
25시간 1로 고정돼 있었다는 건 운영자 앱이 붙은 채였다는 뜻이다. 클라이언트
없이 세션만 열면 이 값은 0이라, 라이브 검증 때 WS 클라이언트를 반드시 붙여야
한다 (안 붙이면 검증이 조용히 무의미해진다).
