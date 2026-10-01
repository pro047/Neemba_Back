먼저 시도: ON

# 파이프라인 런처 프로토콜 (serial-agent-pipeline)

이 리포에는 파이프라인 설정(`.sap/`)이 있고, 셸 오케스트레이터는 전역 명령 `sap` 이다. 세션(LLM)은 **런처**다 — 실행·전달·중계만 하고 판단하지 않는다.

- 준비: `sap worktree <feature>` 로 격리 worktree 를 만든다. 설정 확인은 `sap doctor`
- 실행: worktree 에서 `sap run <feature>` 를 백그라운드로. 진행 중에는 `.pipeline/<feature>/STATE.md` 만 읽는다 (`*.stream.jsonl` tail 금지 — 컨텍스트 오염)
- 멈추면 STATE.md 의 `## 다음 행동` 블록을 그대로 따른다. exit 4 = 사람 승인 대기(실패 아님), 2 = 게이트 위반·프로세스 사망(사인은 FAIL_LOG.md 마지막 항목), 3 = BLOCKED, 5 = 사람 조치 필요(codex 설치·로그인·codex 체인 소진 — 설치와 엔진 전환은 사람에게 y/n 으로 묻고, 로그인은 사람에게 `! codex login` 입력을 안내한다. 런처가 대신 로그인하거나 묻지 않고 엔진을 바꾸지 않는다)
- **게이트 승인은 y 중계다. 판단 금지.** exit 4 에서 세션이 하는 일은 셋뿐이다 — ① 검토 대상 파일을 **그대로** 보여준다 (요약·추천·"괜찮아 보인다" 금지) ② AskUserQuestion 으로 "승인? (y/n)" 하나만 묻는다 ③ 사람의 답이 **정확히 y** 일 때만 STATE.md 가 준 승인 명령(`SAP_ROOT=… sap approve … --relayed y` 또는 `mv …`)을 실행하고 재실행한다. "알아서", "괜찮으면 해" 는 y 가 아니다 — 다시 묻는다. n 이면 중단을 보고하고 기다린다
- 사람이 묻지 않았는데 승인 명령을 실행하거나, `.approved` 파일을 직접 쓰거나, 사람의 답을 바꿔 전달하는 것은 금지. 승인 기록은 `.pipeline/<feature>/APPROVALS.md` 에 남는다
- 상담은 이 세션이 STATE.md·FAIL_LOG.md·산출물을 읽어서 한다. 사람에게 터미널을 더 열라고 안내하지 마라 (상담역 advisor.sh 는 2026-09-30 제거)
- **파이프라인 실행 중(STATE.md phase 가 DONE·DIED 가 아닐 때)에는 worktree 의 파일을 수정하지 않는다.** 사람이 "고쳐줘"라고 해도 파이프라인이 끝나거나 멈춘 뒤에 한다 — 단계 사이의 수정은 다음 단계 기준선에 흡수돼 게이트가 못 잡는다. 이 규칙은 기계로도 막혀 있다 — Edit/Write 는 전역 훅(`pipeline-launcher-guard.sh`)이 거부하고, 그 밖의 수정은 봉인 대조가 다음 단계 직전에 exit 2 로 멈춘다. 훅에 막히면 우회하지 말고 사람에게 보고한다
- `.sap/config`·`.sap/prompts/` 는 파이프라인 실행 중 보호 파일이다. 설정을 바꾸려면 실행이 끝난 뒤 메인에서 고치고 커밋한다 — worktree 는 커밋된 설정만 가져간다