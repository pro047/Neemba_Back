# 2026-08-30 주일예배 관측 + 유령 세션 재발

세션 `0125749a-9c39-4ffe-b5e3-267a111708ed` · 10:39:58~11:48 KST (69분) · 브로드캐스트 875건.
5분 tick 15회(`watch-service` 스킬)로 실시간 관측했고, 종료 후 로그를 회수해 오프라인 재분석했다.

**회수한 원본** (다음 배포가 컨테이너 로그를 지우므로 먼저 내렸다):

| 파일 | 규모 | 내용 |
|---|---|---|
| `~/neemba-source-2026-08-30.txt` | 3,457줄 / 95KB | node `published :` — 번역 **전** 한국어 |
| `~/neemba-translations-2026-08-30.txt` | 876줄 | python `hub: broadcast:` — 타임스탬프 포함 |
| `~/tr-2026-08-30.csv` | 875행 | 위를 스캔 스크립트 입력 형식으로 변환 |

> **이 3개는 이 맥에만 있다.** prod 에는 없다(8/30 배포로 컨테이너 로그가 지워졌다).

## 1. 자막 중복 송출 (release #111) — 실전 검증 통과, 단 근거 등급 주의

```
start KST      session     rows   min  pairs  blocks  replayed sentences
08-30 10:39:58 0125749a     875    69     11       0  -

0 block re-sends / 0 sentences over 69 min, 875 rows, 1 sessions (0 affected)
```

**블록 재송출 0건.** 8/23 전수 스캔에서 실예배 9세션·413분에 3회(약 138분당 1회)
나오던 것이 69분간 0회다.

기각된 클러스터 1건은 판정 로직이 제대로 작동한 사례다 — 설교자가 실제로 반복한 말이라
원본이 연속적이지 않다:

```
0125749a @08-30 11:11:55 x3 — originals not contiguous (spanned=80)
    11:11:55 delay     1s  It's not there.
    11:12:00 delay     6s  It's not there.
    11:12:03 delay   263s  Shall we take a look?
```

**⚠️ 이 판정은 확정이 아니다. 한계 2가지:**

1. **입력이 다르다.** `scan_duplicate_resend.py` 원안은 DB `app.translations.source_text`
   (한국어)를 본다. 이번엔 SG 를 닫은 뒤라 DB 접근이 안 돼 **컨테이너 로그의 영문
   브로드캐스트**로 돌렸다. 스크립트가 검증된 입력이 아니다
2. **`hub: broadcast` 는 전송 성공분만 찍힌다**(`websocket.py:190`). 아래 §2 의 순단 중
   버려진 23건은 스캔 대상에 아예 없다

**확정하려면** SSH 를 열고 `scripts/scan_duplicate_resend.py` 를 인자 없이(=DB 조회) 다시
돌린다. 보존 30일 안이면 8/30 세션이 아직 DB 에 있다.

## 2. 자막 순단 2회 — 둘 다 자동 복구, 서버 결함 아님

| 시각(KST) | 지속 | close code | 유실 | 원인 |
|---|---|---|---|---|
| 10:52:03 | **2분 6초** | `1006` | 번역 **23건** | **미확정** — 강제 단절(OS 절전·네트워크·앱 종료) |
| 11:25:29 | **13초** | `1005` | 없음 | [정지] 버튼 (node `stop ignored` 와 **0.4초** 차) |

**교차검증됨.** 두 로그가 독립적으로 같은 값을 낸다:
- python 로그: `no connected listener` 23줄 (10:52:07~10:54:13)
- 브로드캐스트 타임스탬프: 최대 공백 **130.6초** (10:52:03 → 10:54:13)

### 스킬 문서가 틀린 지점 — 순단분은 큐잉되지 않았다

`.claude/skills/watch-service/SKILL.md` §4 는 순단 시
`hub: client disconnected, waiting for reconnect ... pending=N` 으로 큐에 쌓이니
**유실이 아니라고** 적혀 있다(2026-08-05 소스 확인 근거). 그런데 이번 1006 에서는
**그 줄이 한 번도 안 나왔다.** 실제 경로는:

```
main : websocket disconnected code=1006 reason=''
hub: listener dropped <id> session: <sid> by: client_disconnect
keepalive: cancelled <id>
hub: no connected listener <sid> en-us     ← 23줄, 큐잉 없이 폐기
```

**`hub_send_failed_total` 은 +0 이었다** — 이 유실을 세지 않는다. 메트릭만 보면 안 보인다.

> **다음 조사거리**: `waiting for reconnect` 경로와 `listener dropped ... client_disconnect`
> 경로를 가르는 조건이 무엇인가. 1006(close 프레임 없음)이면 즉시 drop 인지, 아니면
> 다른 요인인지. 소스(`websocket.py`)를 봐야 한다. **이번엔 조사하지 않았다.**

10:40~10:50 에 40~80초 공백이 4회 더 있으나 찬양·기도 구간이라 발화 자체가 없었던
것으로 본다(**추정** — 원문 로그로 대조하지 않았다).

## 3. 고유명사 오인식 — 큐 1번 데이터 (원문 3,457줄 전수 집계)

설교 예화가 중국 춘추전국 고사(중산국·사마자기)라 **성경 고유명사가 아닌** 오인식이
대량으로 나왔다. 그래도 `transcriptNormalization` 적용 대상이라는 성격은 같다.

| 정답 | 틀린 형태 | 횟수 |
|---|---|---|
| **중산국**(中山國) | `중상국` | **6** |
| | `준산국` | 1 |
| | (`중산국` 정답) | 1 |
| **사마자기**(司馬子期) | `사마작` | 2 |
| | `삼아지기` | 1 |
| 양갱/양고기 국물 | `양국이` | 3 |
| 한문 | `반문` | 1 |

**`중산국` 은 8회 중 1회만 맞았다.** 같은 문장 안에서도 흔들린다:

> …`준산국`이라는 조그만한 나라가 있었습니다 이 `중산국`에 절차라는 왕이…

**entry 후보 (긴 것 우선 — 큐 1번의 정합성 조건 그대로):**

```
삼아지기 → 사마자기
중상국  → 중산국
준산국  → 중산국
사마작  → 사마자기
```

`사마작`⊂`사마작이`(조사 결합형)이 실제로 로그에 있으므로 순서를 지켜야 한다.

**대조군 — 과보정 위험의 기준선**: `아멘` 21회·`다윗` 2회 **전부 정상 인식**이다.
큐 1번이 경고한 "아멘↔아무"·"다윗↔다음" 과보정을 A/B 로 잴 때 이 값을 before 로 쓴다.

**미확인 1건**: 케냐 선교사 이름 `Belix`/`Nansi` 의 한글 표기. 원문에 `케냐` 는 1회
나오지만 이름 부분을 특정하지 못했다.

## 4. 번역 품질 — 양호. 오역은 전부 STT 책임이었다

STT 가 제대로 받은 구간은 번역이 온전하다:

- `전도서 12:13` → `Ecclesiastes 12:13` · `마가복음 16:17-18` → `Mark 16:17–18` — 장절 정확
- `백 배 육십 배 삼십 배` → `a hundredfold, sixtyfold, and thirtyfold` — 성경 관용어 정확

**tick 중 번역기 결함으로 의심했다가 철회한 것 1건**: `Data: No data available` 은
플레이스홀더가 아니라 원문 `자료 자료 없습니다` 의 직역이었다. SOURCE 섹션이 없었으면
번역기 버그로 오판할 뻔했다 — 스킬 §4 의 "SOURCE 를 나란히 읽으라"가 실제로 작동한 사례다.

찬양 구간의 단어 단위 파편화(`My body and`)는 예년대로 반복 확인됐다.

## 5. 유령 세션 재발 (13:27) — 8/24 인시던트와 동일

**예배와 무관한 별건이다.** 예배 세션은 정상 종료됐다(`0125749a stopped` / `detached
sockets=0`). 오늘 다른 세션 3개(`ec9d75a7`·`dd2e15b7`·`abbe6c2e`)도 전부 정상 종료됐다.

| 항목 | 값 |
|---|---|
| 세션 | `9d5c8c6a-f3d8-4a3e-b4e9-5e48304d7cfd` |
| 시작 | **13:27:23 KST** ([시작] 눌림) |
| STT 일시정지 | 13:28:09 — `4 consecutive errors with no audio` |
| 마지막 출력 | 13:31:48 `hub: broadcast: Connect!` (핸드셰이크지 번역이 아니다) |
| rtmp 컨테이너 로그 | **200분간 완전히 비어 있음** — publisher 가 한 번도 안 붙었다 |
| 지속 | 약 2시간 (monitor 가 30분마다 알림) |
| `ffmpeg_stale_total` | **1239**, 5초 간격 재측정 1238→1239 (초당 약 0.2 증가) |
| 회수 API 응답 | `{"ok":true,"ended":true,"translationCount":0}` |

**`ffmpeg_stale_total` 은 아침 베이스라인(10:39)에서 0 이었다.** 1239 는 전부 이
세션이 만든 것이다. `translationCount=0` 이 "publisher 가 끝내 안 붙었다"를 확정한다.

**회수** — 인시던트 문서 §회수 절차 그대로. 조치 후 실측:

| 지표 | 조치 전 | 조치 후 |
|---|---|---|
| `hub_active_session` | 1 | 0 |
| `stt_paused{9d5c8c6a}` | 1 | 시리즈 제거 |
| `ffmpeg_stale_total` | 1239 (증가 중) | 0 |
| ffmpeg 정체 루프 | 5초마다 | 멈춤 |
| monitor 알림 | 30분마다 | 없음 |

## 6. 배포 — release #113 (PR #112 를 prod 로)

재발 직후 배포했다. 8/26 예배 전날 밤을 피하려 미뤄뒀던 그 배포다.

- 워크플로 `33297472252` **completed success**, 컨테이너 11개 재생성
- **배포 확인**: `dist/router/rtmp.js` 에 `RTMP_NO_PUBLISHER_GRACE_SEC` 존재
- **유예 기본값 1800초(30분)** — env 미설정이라 `DEFAULT_NO_PUBLISHER_GRACE_SEC`
  (`services/node/src/router/rtmp.ts:24`) 를 쓴다

**배포 후 관측이 어렵다 — 증분 0 이 정상이다.** 빈도가 낮아(8/24·8/30 두 건)
정상 동작을 수동으로 확인할 수단이 없다. **확인하려면 의도적으로 재현한다** —
publisher 없이 [시작] 을 누르고 30분 뒤 세션이 스스로 닫히는지 본다.

**회귀 위험은 남아 있다.** `publisher_done` 타이머 경로를 건드렸고 이건 방송 중인
세션을 죽일 수 있는 경로다. **다음 예배(수요 저녁)에서 세션 조기 종료 여부를 확인할 것.**

## 7. 부수 발견 — 앱 close code (큐 13번)

§2 의 10:52 순단 원인을 **끝내 확정하지 못한 이유**가 여기 있다. `mvp/lib` 의 소켓
close 2곳이 **둘 다 코드·사유 없이 닫는다**:

```
mvp/lib/ws_client.dart:122       await channel?.sink.close();        # 다운링크(자막)
mvp/lib/node_ws_client.dart:320  await _webSocket?.close();          # 업링크(node)
```

`lib` 전체에서 소켓 close 는 이 2개가 전부다. 수정 시 두 곳을 함께 손봐야 한다.
상세·검증 근거는 큐 13번 행에 있다.
