# log — claude-only-run

> 메인이 단일 writer. 워커 산출은 packet 원시각을 명시해 메인이 append한다.

## 타임라인

| 시각 | 사건 | 결과/결정 |
|------|------|----------|
| 2026-11-17 | 조사 워커(Opus×1, 읽기 전용) 보고 회수 | Claude 쪽 30 run NOT_STARTED · v1 기준 · 스크립트 5종 없음 · Codex 1사이클 중단 · 실제 토큰 값 0건. 상세는 `rules/docs/plans/2026-11-17/org-publish/log.md` |
| 2026-11-17 | 저장소 공개·이름·위치 변경 | 조직 `agent-discipline/need-harness-lab` public, 로컬 `/home/jun/project/agent-discipline/need-harness-lab` (기록: rules 저장소 `docs/plans/2026-11-17/`) |
| 17:2x | 인터뷰 마감 | 하네스 v4.3 · 웹 도구 세 조건 허용·기록 · 두 번째 리뷰어 Fable 5.2 · 보고 단위 = 준비·파일럿·1사이클 뒤 승인. 나머지 4항은 메인 기본값(명세 §0) |
| 17:31 | 명세 합의 | "합의 — 준비 시작" · SPEC=1 · MODE=auto · stakes 높음 |
| 17:3x | 준비 워커(Opus) 기동 — task 01·02·03 | 기준 SHA `94cbc30`, 하네스 기준 `7b0ef7b`. 유료 실행·push 금지 |
| 17:55 | 준비 워커(Opus×1, 353k 토큰) 보고 회수 — packet: 시작 HEAD `94cbc30`, 하네스 `7b0ef7b`, 시작 17:32:33·종료 17:55(워커 보고 시각) | task 01·02·03 완료 보고. 브랜치 `setup/claude-only-v3`, 커밋 12개(`c30729e`..`54dc7b0`), push 없음. 워커가 실행한 검증: runner 단위 테스트 24 OK, scripts 8 OK, `verify_setup` passed, 무과금 self-test 1번·3번 passed, 이미지 안 하네스 훅 테스트 261 passed, 이미지 `payment-claude-runner:2.1.326` 빌드 |
| 17:56 | 메인 교차 확인 | 커밋 12개 확인 · `94cbc30..HEAD`에 codex 경로 diff 0 · codex 외 작업 트리 깨끗 · runner·scripts 단위 테스트 재실행 통과 · `launch.py` `MODELS` = opus-5-5 / fable-5-2 확인. 주의: task 01의 "있는 그대로" 커밋(`c30729e`)에 이 작업 폴더의 명세·log도 들어갔다(당시 untracked였음) |
| 17:56 | 워커가 올린 결정 필요 항목(사용자 대기) | ①동결 순서: 명세 task 06은 "preflight 전 항목 true"인데 preflight가 `pilot_passed`를 요구해 파일럿(07) 전에는 불가 ②상한(2,000만·8시간·30회)을 실행기가 강제하지 않고 진행자 몫 ③`CLAUDE_CODE_SUBAGENT_MODEL`만으로는 에이전트 정의의 모델을 못 덮을 수 있음 — 하네스 v4.3 `explorer`는 `model: sonnet`이라 명세 §2 "워커 Opus"와 어긋날 수 있음 ④3번 조건 init tools에 Glob·Grep 없음(1·2번엔 있음 — 9월부터의 비대칭) ⑤`result.web_requests`가 메인 세션 WebSearch만 잡음 ⑥대리 사용자 모델 미정(워커 제안: opus-5-5) |
| 17:57 | 설계 선검증 | 생략됨 — 준비 구현을 먼저 시작했다. 동작 불변식(리뷰어 사용량 분리 집계 등)은 이번 듀얼 리뷰와 blind 테스트로 검증한다(절차 이탈 기록) |
| 17:57 | 리뷰 루프 1 — packet 생성 | base `c30729e`(있는 그대로 커밋) → HEAD `54dc7b0`, 73파일 +2256/−514, codex 경로와 ledger 제외. untracked 0건. 보안 스캔: 키 패턴 매칭 8파일 전부 하네스 사본의 스캐너 정의·테스트 더미·review.md 설명(공개 저장소 harness와 같은 내용) → 오탐 판정. password/token 값 패턴 0건, 이메일 0건. 홈 경로는 이미 공개된 내용이라 그대로 둠 |
| 17:58 | 리뷰 루프 1 — 기동 | Opus 리뷰어(미러 Read 전용) ∥ codex(cwd=미러, read-only, effort medium) + blind 테스트 워커(Opus, 새 테스트 파일만, 이력 열람 금지) |
| 18:0x | 리뷰 루프 1 — codex 회수 | finding 3건(F1 상한 미집행 높음, F2 볼륨 정리, F3 역할 모델 검증) + open question 2 |
| 18:0x | 리뷰 루프 1 — Opus 리뷰어 회수 | finding 8건 + open question 9. 리뷰어는 Glob·Grep 없이 diff 줄 번호로 위치를 잡음(새 파일 줄 번호는 근사) |
| 18:1x | 메인 종합 | 핵심 지적을 미러 실파일에서 재현 확인(launch.py 턴 인자·start_cycle:60·freeze_readiness:102-107·consult_contract:61,69·fable-review.sh:33-39). 채택 15(R1~R11, Q3·Q4·Q5·Q7), 기각 1, open question 3, 범위 밖 1. codex 종합 감사 기동 |
| 18:1x | codex 종합 감사 회수 | 올림 2건(Q9→R12 파일럿 통과 증명이 파일 존재·해시만 확인, Q6→R13 워커 웹 사용 기록 경로 없음) · 근거 부족 지적 3건(Q3·R10·Q7 → 채택 철회) · 새 후보 3건: 세 조건 첫 턴 병렬 실행(기각 — 명세 §0의 "하나씩"은 부하 측정 대상), preflight가 파일럿 전에도 `pilot_passed` 요구(채택 R14), 대리 사용자 사용량이 run 결과에 미합산(채택 R15) · 충돌 지점 2건(R2↔R4 삭제 순서, R1은 전 역할 장부가 먼저) |
| 18:1x | R14 처리 방침(메인) | 명세 §9 task 06의 "preflight 전 항목 true"는 파일럿 통과 항목과 순서가 맞지 않는다 → 파일럿 시작 게이트는 파일럿 통과를 요구하지 않고, 본 사이클 게이트는 요구하도록 나눈다. 명세의 목표·불변식 변경이 아니라 task 인수 조건의 모순 해소로 판단. 사용자에게 보고 예정 |
| 18:1x | 수정 워커 재개(준비 워커와 같은 에이전트) | R1·R2·R3·R4·R5·R6·R7·R8·R9·R11·R12·R13·R14·R15·Q4·Q5 + 문서 정정 3건 지시 |
| 18:06 | blind 테스트 워커(Opus×1, 188k 토큰) 회수 — packet: 시작 HEAD `54dc7b0`, 17:58:36~18:06:18 | 새 테스트 파일 2개(미커밋): runner 23개 전부 통과, scripts 17개 중 4개 실패. 실패 → 결함 3건: T1 역할 모델 검증이 요청값 대비 상대 비교(freeze_readiness.py:74-76,104-107), T2 동결 해시가 달라도 사이클 시작(start_cycle.py:31-40, preflight.py:18), T3 대리 사용자 미관측 사용량을 0으로 기록(consult_contract.py:105,116). 변이 확인 3건에서 테스트가 결함을 검출. 단위 검증 불가로 보고된 것: 상한 집행 코드 부재(R1과 같은 관측), 진행자·판정 어댑터 토큰 기록 경로 부재 |
| 18:2x | 수정 워커에 T1·T2·T3 추가 지시 | blind 테스트 파일은 워커가 수정·커밋하지 않고 메인이 관리 |
| 18:2x | 사용자 결정 2건 → 수정 워커에 전달, 명세 §0·§2에 기록 | 대리 사용자 = `claude-opus-5-5` · 탐색 워커 = 하네스 정의대로 Sonnet |
| 18:27 | 수정 워커 회수(같은 에이전트 재개, 누적 564k 토큰) — packet: 시작 HEAD `54dc7b0`, 18:08:54~18:27:22 | 커밋 21개(`54dc7b0`..`f984104`), push 없음. 채택 지적 전부 + T1·T2·T3 + 결정 2건 반영 보고. 워커 실행 검증: runner 58 OK(명세 테스트 포함) · scripts 36 중 실패 1·오류 4(전부 `scripts/test_spec_invariants.py` — 인터페이스 변경: readiness에 대리 사용자 모델 필수, `consult_contract --output` 제거) · `verify_setup` passed · 무과금 self-test 1번·3번 passed(3번 도구 = 1·2번 목록 + `Task`) · 이미지 재빌드 `sha256:e9f24260…` · codex 경로 무변경. ID별 커밋이 완전히 나뉘지는 않음(R4+R15, R13+R5, R3+T1+R9 묶음) |
| 18:27 | 수정 워커가 올린 결정 필요 항목 | R1 피드백 셈법: "첫 성공 턴 이후의 성공 턴 수"로 구현 → 하네스 조건의 대리 사용자 답변 전달 턴도 30회에 들어감(사용자 확인 대기). 유료 실행으로만 확인 가능한 것 4건(탐색 워커 실제 모델, 서브에이전트 effort 기록, 재개 세션 비용이 누적인지, WebSearch 내부 요청 토큰) — 파일럿 확인 항목 |
| 18:3x | blind 테스트 워커 재개 | 실패 5건을 현재 인터페이스로 갱신(기대값은 명세 유지) + 새로 단위 검증 가능해진 불변식(상한 집행, 역할별 분리 기록, 도구 표면) 테스트 추가 지시 |
| 18:5x | blind 테스트 워커 회수(재개) | 두 파일 갱신(미커밋, 메인 관리). HEAD `f984104`에서 runner discover 68 OK · scripts discover 43 OK. 워커 지적: 명세의 "input+output 2,000만"에 캐시 토큰이 들어가는지 모호 |
| 22:4x | 토큰 상한 범위 확인 | `docs/experiment-plan.md:67` "input+output에는 cached input이 포함된다" — 현재 구현(캐시 포함)이 기존 계획서와 일치. 명세 §0에 기록 |
| 22:4x | 사용자 결정 — R1 피드백 셈법 | "판정 피드백만 셈". 질의응답 턴은 상한 밖, 횟수 별도 기록. 수정 워커에 지시 → 그 뒤 blind 테스트 갱신 → 2회차 리뷰 |
| 22:40 | 수정 워커(Opus×1, 183k 토큰) 회수 — task `R1-feedback-count`, packet: `f984104`→`b1bda85`(커밋 1개, push 없음), 22:35:34~22:40:53 | 재개 턴에 `--kind feedback|answer|recovery` 필수(없으면 유료 호출 전 거부), `invocation.json`에 `turn_kind`·`counts_as` 저장, 상한 30은 성공한 feedback만, answer 턴 수 별도 기록. 메인 교차 확인: `test_launch.py` 41 OK · scripts 43 OK · runner 전체 74 중 실패 7·오류 1(전부 `runner/test_spec_invariants.py` — 픽스처가 옛 인터페이스) |
| 22:4x | 메인 판단 2건(워커가 올린 결정 항목) | ①실패한 feedback 전달은 세지 않고 그 recovery가 성공하면 1회 — 이전 "성공 턴만 센다" 규칙 유지, 채택. ②answer를 미전달 consult 수에 묶은 안전장치(지시에 없던 추가) — 판정 피드백을 answer로 선언해 상한을 피하는 경로를 막으므로 채택. 사용자에게 보고 |
| 22:4x | blind 테스트 워커 기동(task `T-blind-3`, 구현 본문·diff 미열람 계약) | 실패 8건을 새 인터페이스로 갱신 + 새 셈법 테스트 추가 지시 |
| 22:46 | blind 테스트 워커(Opus×1, 116k 토큰) 회수 — task `T-blind-3`, HEAD `b1bda85`, 22:42:33~22:46:42. 구현 본문·diff 미열람(grep에 본문 2줄이 딸려 나왔다고 보고) | 실패 8건을 새 인터페이스로 갱신, 새 셈법 테스트 20개 추가(runner 17·scripts 3). 명세대로 짰는데 현 코드에서 실패하는 테스트 0건. 메인 재실행: runner 91 OK · scripts 46 OK |
| 22:47 | 명세 테스트 파일 2개 커밋(메인) | `a6d3349`. push 없음 |
| 22:47 | 2회차 리뷰 packet — base `c30729e` → HEAD `a6d3349`(index+작업트리), `payment-settlement/codex` 제외(보존 대상·범위 밖) | `$OUT`=`/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/review2.2iVX` · 미러=`/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/mirror2.hxfz`(1회차 미러 삭제). 77 files +5237/−552, untracked 0. 보안 스캔: 적중은 테스트용 가짜 토큰(`sk-ant-oat01-SPECINVARIANT-*`, 하네스 훅 테스트 케이스)과 setup 결과 JSON의 로컬 경로(이미 공개 저장소에 있는 내용) — 오탐 판정, 통과 |
| 22:48 | Opus 리뷰어 ∥ codex 기동(같은 프롬프트, 원문 격리) | 회수 대기 |
| 22:53 | 2회차 리뷰 회수 — codex(exit 0, finding 3·질문 2) · Opus 리뷰어(189k 토큰, 22:48:27~22:53, finding 5·질문 7) | 원문 `/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/review2.2iVX/results/codex-review-2.md`·`opus-review-2.md` |
| 23:0x | 메인 종합(코드 직접 확인: consult_contract.py:15-125, launch.py:812-826·903-928, experiment-plan.md:26) → codex 감사(exit 0) | `main-synthesis-2.md`·`codex-audit-2.md`. 감사 반영: Q4·OQ3 승격(S8·S7), S9 채택 철회, S3는 보고 usage 의미 확인 전 보류. A2는 기각. ledger 갱신(1회차 채택분 20건 fixed_in_loop 1) |
| 23:0x | 수정 워커 기동(Opus, task `L2-fixes`) | S2·S4·S5·S6·S7·S8. S1(대리 사용자 입력)·S3(보고 usage 하한)·명세 테스트 파일은 범위 밖으로 지시 |
| 23:1x | 사용자 결정 2건 | ①대리 사용자 입력: 사용자가 4개 유지 쪽 의견을 묻고, 메인이 `docs/answer-policy.md:3`(4개를 명시 허용)을 확인 → 4개 유지, S1 기각. ②상한 초과 완료 run = "완료 + 초과 표시" → 수정 워커에 추가 지시(Q10). 명세 §0에 기록 |
| 23:08 | 수정 워커(Opus×1, 295k 토큰) 회수 — task `L2-fixes`, `a6d3349`→`f783070`(커밋 7개, push 없음), 22:57:49~23:08:37 | S2·S7·S6·S8·S4·S5·Q10 반영 보고. 새 스크립트 `scripts/record_evaluation_cost.py`·`scripts/finish_run.py`. 메인 재실행: scripts 53 OK · runner 97 중 실패 3(전부 `runner/test_spec_invariants.py` — 상담 픽스처에 `exit_code` 없음). 커밋 메시지 금지어 0건 |
| 23:1x | 수정 워커가 올린 결정 항목 | ①진행자의 피드백 작성 토큰 귀속: `prompts/feedback.md:13`·`measurement/README.md`는 `prompt-author` 역할로 실험군 비용에 넣는다고 적고, 이번 구현은 진행자 전체를 평가 비용(상한·조건 비용 밖)으로 처리 → 사용자 결정 대기. ②진행자 사용량이 run별로 나뉘지 않을 때의 배분 원칙 없음 → 사용자 결정 대기. ③`turn --dry-run`이 실패한 첫 턴 뒤 recovery를 미리 확인 못 함(범위 밖, 3회차 리뷰에서 확인) |
| 23:1x | blind 테스트 워커 기동(task `T-blind-4`) | 실패 3건 수리 + S2·S4·S5·S6·S7·S8·Q10·대리 사용자 입력 4개 테스트 추가 지시 |
| 23:19 | blind 테스트 워커(Opus×1, 191k 토큰) 회수 — task `T-blind-4`, HEAD `f783070`, 23:10:13~23:19:32. 구현 본문·diff 미열람(def 줄·상수·--help·가짜 docker 실행 결과만) | 실패 3건 수리(상담 픽스처를 실제 호출 결과에서 복사), 불변식 7개 테스트 추가, 변이 점검 3종. 명세 근거로 남긴 실패 0건. 메인 재실행: runner 121 OK · scripts 59 OK. 관찰 3건(테스트 아님): init의 contract hash가 readiness 복사본 · `finish_run` 재실행 시 `cap_overshoot` 덮어쓰기 · requestId 충돌 요청이 상한 계산에서 빠짐 → 3회차 종합에서 판정 |
| 23:2x | 명세 테스트 커밋(메인) `65b23c3` → 3회차 packet — base `c30729e` → HEAD `65b23c3`, codex 경로 제외, 2회차 이후 diff 별도 첨부 | `$OUT`=`/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/review3.PDS6` · 미러=`/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/mirror3.M1m2`(2회차 미러 삭제). 79 files +6690/−554, untracked 0. 보안 스캔 오탐만(테스트용 가짜 토큰, 문서 속 패턴 문자열) |
| 23:2x | Opus 리뷰어 ∥ codex 기동(3회차, 마지막) | 회수 대기 |
| 23:26 | 3회차 리뷰 회수 — codex(exit 0, finding 1·질문 2) · Opus 리뷰어(163k 토큰, 23:21:03~23:26:14, finding 6·질문 5) | 원문 `/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/review3.PDS6/results/codex-review-3.md`·`opus-review-3.md` |
| 23:3x | 메인 종합(코드 직접 확인: launch.py:308-322, finish_run.py:30-62, commit_run.py:30-44, capture_turn.py:34-45) → codex 감사(exit 0) | 신규 채택 10건(U1~U10 — 감사가 U8·U9 승격, U10 추가). 2회차 채택분 6건 fixed_in_loop 2 |
| 23:3x | **종료 판정: `review unresolved`** | 3회차 신규 채택 10건 → 종료 조건 미충족 + 최대 3루프 도달(review.md §1). 미해소 = U1~U10, 보류 = S3·A3(파일럿 뒤). 신규 10건 중 7건(U2·U3·U4·U5·U7·U8·U10)이 run 종료 경로(판정 → 증거 → 기록 → 커밋)·파일럿 기록·대리 사용자 호출에 몰림 — 2회차 수정으로 생긴 새 스크립트의 경계. 감사가 수정 간 충돌 2건 지적(U2↔U3 종료·커밋 순서, U4↔U2·U7 파일럿 재판정). 사용자 보고·결정 대기 — 해소 또는 사용자 보류 결정 전 병합·동결 불가 |
| 23:4x | 사용자 결정 — "종료 경로 설계부터 하고 권고대로 진행" | 진행 방식 = 종료 경로 설계 → 합의 → 구현 → 그 범위만 리뷰 1회 추가(상한 초과분 승인). 진행자 비용 = 실험 전체 단위 평가 비용 장부, 조건별 비용 밖, 피드백 작성분 미분리는 한계 |
| 23:5x | 종료 경로 설계 작성(명세 §10, 합의 대기) — 메인이 직접 읽은 것: commit_run.py 전체, capture_turn.py:30-99, finish_run.py:30-62, run.json, runbook 종료 절, evaluation/README.md:20-30 | 설계 선검증(codex, exit 0): 충돌 4·원천 점검·빠진 경로 3·단순화안 1 → §10에 반영 |
| 23:5x | 사용자 합의 — 명세 §10 "설계대로 구현 진행하자" + 추가 결정: 판정은 메인이 직접 읽고 전부 판정 | §10.1·10.2에 반영(판정표 작성자 = 메인, 진행자는 판정 실행·산출물 저장·관측 메모까지). 도중 피드백은 진행자 유지 — 메인 해석, 사용자 확인 대기 |
| 23:5x | load-bearing 가정 조기 실증 | `codex/docs/pilots/detailed-01/` 판정 산출물 3종을 읽음: 시나리오별 status가 있고 HTTP는 PARTIAL(HTTP_ONLY) → 판정표로 옮길 수 있으나 DB·은행 대조와 합치는 판단이 필요. 가정 성립 |
| 23:5x | 구현 워커 기동(Opus, task `C-closeout`) | 명세 §10 구현 |
| 11-18 00:0x | 사용자 — 토큰 교체 완료 | `~/.config/payment-lab/claude.env` 갱신(00:00:31), 권한 600·1줄·키 1개·접두 일치 확인. `claude/docs/auth-token.md` 표에 교체 일자 기록(미커밋 — 구현 워커가 같은 트리에서 작업 중이라 워커 회수 뒤 커밋). 인증 스모크(task 05)는 종료 경로 리뷰 뒤 |
| 11-18 00:09 | 구현 워커(Opus×1, 306k 토큰) 회수 — task `C-closeout`, `65b23c3`→`1c8dbf3`(커밋 3개, push 없음), 23:55:18~00:09:53 | 명세 §10.2~10.5 구현 보고, 멈춘 항목 없음. `finish_run.py` 전면 개편(판정표 도출·증거 4칸·1회 제한·supersede) · 파일럿 위치 분리 · 평가 비용을 실험 단위 장부로 · U1·U5·U6·U9·U10. 워커 보고: 자체 테스트 통과, `verify_setup` setup_passed true, 명세 테스트는 인터페이스 변경으로 runner 28·scripts 7건 실패(예상) |
| 11-18 00:1x | 메인 판단(워커가 올린 항목) | ①호출 전 차단 + 판정표 전부 PASS → 거부 유지 ②3번 조건의 대리 사용자를 필수 역할로(계획서 "3만 의무 사전 인터뷰") — 명세 테스트 워커에 명세로 전달, 현 코드가 선택 처리면 finding ③워커가 더한 보호장치 3종(예시 판정표 거부, 전부 PASS일 때 INVALID_INFRA 거부, 평가 비용 같은 역할·세션 재기록 거부) 유지 |
| 11-18 00:1x | `auth-token.md` 교체 일자 커밋(메인) · blind 테스트 워커 기동(task `T-blind-5`) | 회수 뒤 이 범위만 리뷰 1회(Opus ∥ codex)·감사 |
| 11-18 00:2x | 사용자 결정 — 진행자 모델 Fable 5.2, 역할 분담 확정(진행 = Fable, 통제·최종 판정 = 메인) | 명세 §0 추가 결정에 기록. runbook·README의 진행자 모델 표기는 리뷰 수정 때 함께 맞춤 |
| 11-18 00:24 | blind 테스트 워커(Opus×1, 235k 토큰) 회수 — task `T-blind-5`, 00:11:49~00:24:04. **저장소에 쓰지 못함**: 같은 세션에서 다른 저장소(compaction-experiment)의 새 명세를 만들면서 게이트가 SPEC=0으로 리셋돼 워커의 Edit이 막힘. 워커는 우회하지 않고 임시 사본(`/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/mirror/`)에서 작업 | 임시 사본 기준 runner 174 중 실패 1 · scripts 71 OK. 남긴 실패 1건 = finding: 3번 조건에서 대리 사용자 호출이 없어도 `all_role_models_verified`가 true(`finish_run.py:35` 필수 역할에 user-proxy 없음) |
| 11-18 00:3x | 종료 경로 리뷰(상한 초과분 1회, 사용자 승인) packet — base `65b23c3` → HEAD `bc89f15`, 명세 테스트 2개·codex 경로 제외 | `$OUT`=`/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/review4.Fs6u` · 미러=`/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/mirror4.93ai`(3회차 미러 삭제). 15 files +1221/−271. 보안 스캔 통과. Opus 리뷰어 ∥ codex 기동 |
| 11-18 00:4x | codex 회수(exit 0) | finding 2(대리 사용자 필수 역할 누락 — blind 테스트 finding과 동일 · 호출 전 self-test 실패가 기록에 남지 않아 격리 칸이 참이 될 수 있음), 질문 2. Opus 리뷰어 대기 |
| 11-18 00:5x | 게이트 복구 — 사용자가 compaction-experiment 명세에 합의("명세대로 진행하자") → set-state spec-approved·mode auto | 메인이 임시 사본의 테스트 파일 2개를 저장소에 복사, 재실행: runner 174 중 실패 1(위 finding) · scripts 71 OK. 커밋(push 없음) |
| 11-18 00:30 | Opus 리뷰어(164k 토큰, 00:26:11~00:30:05) 회수 | finding 1(대리 사용자 필수 역할 — codex·blind 테스트와 동일)·질문 6. 원문 `/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/review4.Fs6u/results/opus-review-4.md` |
| 11-18 01:0x | 메인 종합(코드 직접 확인: launch.py:1146-1162, freeze_readiness.py:143-160, finish_run.py:30-37) → codex 감사(exit 0) | 채택 6건(W1~W6). 감사가 메인 판단 2건을 뒤집음: ①"호출 전 차단 + 전부 PASS는 거부"는 명세 §10.2의 "그 시점까지의 기록으로 판정·종료·커밋"과 충돌 → 철회, 통과는 판정표만으로 정하고 상한 기록은 표시로 ②FAIL 판정의 근거 누락 기각 → 채택. 명세 §10.1(순서)·§10.2(통과 규칙·근거 필수)·§10.4·§0 수정. 3회차 채택분 10건 fixed 처리 |
| 11-18 01:0x | 수정 워커 기동(Opus, task `C-closeout-fix`) | W1~W6. 그 뒤 blind 테스트 갱신 → 타깃 재점검 1회로 마감 |
| 11-18 00:46 | 수정 워커(Opus×1, 283k 토큰) 회수 — task `C-closeout-fix`, `423cce6`→`828d1c7`(커밋 7개, push 없음), 00:34:02~00:46:00 | W1~W6 반영. 워커 보고: scripts 75 OK · test_launch 54 OK · runner 전체 175 중 실패 33(전부 명세 테스트 — 순서 변경으로 fixture가 capture 없이 finish 호출 19개 메서드, 대체된 상한 규칙 3개 메서드). 워커 판단: 결과 파일 없는 끝나지 못한 턴은 종료를 막지 않고 `turns_not_capturable`로 기록 — 메인 채택. 주의: 이 행 위의 "01:0x" 표기는 추정 시각이었다(실제 시계보다 앞섬) |
| 11-18 00:48 | codex 타깃 재점검(exit 0) — 수정 diff `423cce6`..`828d1c7` | W1·W2·W4·W5 해소, W3·W6 부분. 신규 2건: capture가 result.json 복사 뒤 실패해도 종료 검사 통과(W3 보완) · 어댑터 `--item`이 선택값이라 명세 §10.4와 불일치(W6 보완). 워커 판단 (가) 허용, (나) 불일치 |
| 11-18 00:50 | 보완 수정 워커(task `C-closeout-fix2`)와 blind 테스트 워커(task `T-blind-6`, 보완 2건을 명세로 전달) 진행 중 | 회수 뒤 테스트 통과 확인으로 종료 경로 마감 → 인증 스모크·동결 |
| 11-18 00:55 | blind 테스트 워커(Opus×1, 207k 토큰) 회수 — task `T-blind-6`, 00:47:31~00:55:03, 구현 본문·diff 미열람 | fixture를 실제 절차 순서(normalize → capture → finish)로 바꿔 33건 해소, 바뀐 규칙마다 테스트 추가(상한 6종 × 판정, 기록 완료 전 종료 차단, 근거 필수, 호출 전 self-test 실패, 어댑터 건 표시). 남긴 실패 1건 = 어댑터 `--item` 누락 허용(보완 수정 커밋 전) |
| 11-18 00:56 | 보완 수정 워커(Opus×1, 136k 토큰) 회수 — task `C-closeout-fix2`, `828d1c7`→`e4731dd`(커밋 2개), 00:49:08~00:56:07 | ①`capture_turn`이 마지막에 `captured.json`(파일 목록·해시)을 쓰고 종료 기록이 그것을 요구, 도중에 멈춘 기록은 다시 실행하면 복구 ②어댑터 줄의 `--item` 필수 |
| 11-18 00:57 | 메인 재실행: runner 194 OK · scripts 75 OK · `verify_setup` setup_passed true → 명세 테스트 커밋 | **종료 경로 리뷰 마감**: 추가 리뷰 채택 6건 + 재점검 보완 2건 전부 fixed(6건 ledger 갱신). 미해소 = S3·A3(파일럿 뒤로 보류, 사용자 보고함). 다음 = task 05 인증 스모크 → task 06 동결 → 보고 ① |
| 11-18 00:58~01:05 | 진행자(Fable 5.2, 164k 토큰) — task `P-smoke-freeze`: 사전 점검 통과(194·75 OK, setup_passed), 이미지 재빌드(변화 없음)·4개 pull, 세 조건 인증 스모크 1턴씩(유료 CLI 호출 3회, 토큰 합 65,497, CLI 추정 약 0.44 USD) | 인증·모델(1·2번 `claude-opus-5-5`, 3번 PM `claude-fable-5-2`)·self-test·도구 표면·시크릿 스캔 통과. **3번 `usage_complete=false` → 동결 중단**(진행자가 우회 없이 멈춤, 코드·결과 미수정, 스모크 run discard 완료). 새 토큰 인증 확인됨 |
| 11-18 01:07 | 진행자가 올린 막힘 4건 → 메인 판정 | **B1**(결함): `launch.py:796,812` `collect_reviewer`가 `docker cp` 오류 문구 `No such file`에 의존 — Docker 29.1.3은 `Could not find the file …`을 내어 리뷰어 호출 없는 3번 턴이 전부 미완전으로 기록됨(단위 테스트는 옛 문구를 모의). 오류 문구가 아니라 존재 여부를 직접 확인하도록 수정. **B2**: `harness_adapter_verified`·`acceptance_suite_verified`는 `--attest`로만 참이 되는데 작성 주체·근거 절차가 문서에 없음(Codex판은 true로 하드코딩) → 메인이 근거 파일과 함께 작성하는 절차를 runbook에 명시. **B3**: pilot 단계 preflight의 입력을 만드는 명령 없음 → 추가. **B4**: v3 스모크의 프롬프트·턴 수가 runbook에 없음(진행자가 9월 값을 씀 — 인정) → 명시. 스모크로 관측 안 된 항목(탐색 워커 모델·서브에이전트 effort·resume 비용·웹 토큰·리뷰어 수집)은 파일럿으로. 수정 워커 기동(task `P-fix-freeze-path`) → codex 재점검 → 세 조건 스모크 재실행 → 동결 |
| 11-18 01:07~01:27 | 수정 워커(Opus×1, 누적 249k 토큰) — task `P-fix-freeze-path`, `23d5eb9`→`4fcd372`(커밋 5개, push 없음) | B1: 리뷰어 미호출 판정을 오류 문구 대신 리뷰어 디렉터리 복사로(이미지 불변, 실제 Docker 29 컨테이너로 3경우 확인 — 무과금) · B3: `preflight.py --readiness … --stage pilot` 명령, `start_cycle.gate`와 로직 공유 · B2·B4: runbook "v3 인증 스모크와 동결" 절, `export_setup_evidence.py`, 확인서는 메인이 작성. codex 타깃 재점검(exit 0): B1·B3 해소, fail-open 경로 없음. 신규 2건(내보내기 도구가 토큰 하나만 검사 · 임시 사본 삭제 실패를 숨김) → 같은 워커가 수정(`f57f903`·`4fcd372`). 메인 재실행: runner 195 OK · scripts 77 OK, 두 커밋 diff 직접 확인 |
| 11-18 01:29 | 사용자 — "셋 다 다시 돌리고 동결까지 진행하자" → 진행자(Fable) 재기동(task `P-smoke-r2`) | 세 조건 스모크를 새 run id(`smoke-v3r2-arm1~3`)로 재실행 + 증거 내보내기까지. 동결은 메인이 증거를 읽고 확인서를 쓴 뒤 따로 지시 |
| 11-18 01:28~01:32 | 진행자(Fable 5.2, 96k 토큰) — task `P-smoke-r2`: 세 조건 스모크 재실행(`smoke-v3r2-arm1~3`, 유료 CLI 호출 3회, 토큰 합 65,434, CLI 추정 약 0.26 USD), 3번 증거 4개 + 판정기 테스트 출력 내보내기, discard | 세 조건 모두 `usage_complete=true`. 3번: `log_absent`·`projects_absent` true, `reviewer_dir_rc` 0, leftover 없음, 훅 차단(`HOOK_BLOCKED`), 대상 파일 미생성. 막힘 없음, 재시도 0 |
| 11-18 01:34 | 메인이 증거를 직접 읽음(manifest의 init self-test, 호출 전 self-test, Write 시도 → gate-guard `is_error`, 판정기 테스트 8/8 OK, 9월 파일럿의 결함 검출 기록) → `attestation.json` 작성 → 증거·확인서 커밋 `909e3c7` → **동결 실행(메인, 모델 호출 없음)** `freeze_readiness.py` rc 0 → `readiness.json`·`frozen-inputs.json` 커밋 | `preflight --stage pilot` ready true · blockers 없음. cycle 단계 게이트는 rc 2(파일럿 전 — 의도대로). 확인서의 한계: `acceptance_suite_verified`의 근거는 공통 판정 코드의 대조 테스트와 9월 파일럿의 검출 기록이며, 구현마다 쓰는 어댑터의 정확성은 파일럿(②)에서 확인. **task 06 완료 → 보고 ①** |

## 리뷰 ledger (中↑ — 듀얼 리뷰가 돈 작업 필수)

- review packet 루프 1: base `c30729e` / `$OUT` = `/tmp/claude-1000/-home-jun-project-agent-discipline/11dd52af-0e61-45bd-9098-f0247a6c0b5e/scratchpad/review1.LPLC` / mirror = 같은 scratchpad의 `mirror1.CWfw`

- 리뷰 원문·종합·감사: `$OUT/results/`의 `codex-review-1.md` · `opus-review-1.md`(메인이 회수 보고에서 옮겨 적음) · `main-synthesis-1.md` · `codex-audit-1.md`. 워커: Opus 리뷰어 262k 토큰, codex×2.
- 경로는 `payment-settlement/claude/` 기준.

| id | first_seen_loop | source | 근거(file:line) | disposition | status | fixed_in_loop |
|----|-----------------|--------|-----------------|-------------|--------|---------------|
| R1 상한(2,000만·8h·30회)이 실행 경로에서 집행되지 않음 | 1 | codex(F1)·opus(OQ1) | runner/launch.py:336-371,734,935 · scripts/preflight.py:15-21 | 채택(높음) | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R2 run 종료 시 볼륨 정리가 절차에 연결 안 됨 | 1 | codex(F2) | docs/runbook.md 종료와 커밋 절 · runner/launch.py:864-874 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R3 역할별 모델 검증이 관측된 역할만 검사 | 1 | codex(F3) | scripts/freeze_readiness.py:102-107 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R4 리뷰어의 CLI 추정 비용 미수집·누락 표시 없음 | 1 | opus(F1) | runner/fable-review.sh:33-35 · runner/launch.py collect_reviewer | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R5 effort 전파를 실증할 수단 없음 | 1 | opus(F2) | runner/launch.py:358-361 · docs/runbook.md:62 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R6 대리 사용자 모델이 동결 없이 통과 | 1 | opus(F3) | scripts/consult_contract.py:61,69 · scripts/freeze_readiness.py:166 · scripts/preflight.py:8 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R7 3번 조건 도구 표면이 서브에이전트 외에도 넓음 | 1 | opus(F4) | runner/launch.py:255,345 · docs/results/setup/v3/selftest-arm3-manifest.json | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R8 다음 사이클 게이트가 손으로 고친 COMMITTED 값을 믿음 | 1 | opus(F5) | scripts/start_cycle.py:60 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R9 1·3번만 스모크하면 2번의 실제 모델 값이 비어 막힘 | 1 | opus(F6) | scripts/freeze_readiness.py:109 · docs/runbook.md:69 | 채택(낮음) | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R10 모델 표 두 곳, 동등성 단정 없음 | 1 | opus(F7) | scripts/prepare.py:21-22 · runner/launch.py:35 | open question(감사: 현재 불일치·실행 오류 입증 없음 → 채택 철회) | — | |
| R11 start_cycle 부분 실패 시 복구 경로 없음 | 1 | opus(F8) | scripts/start_cycle.py:70-104 | 채택(낮음) | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| Q3 리뷰어 effort가 원본(medium)과 다름 — 차이 기록 | 1 | opus(OQ4) | runner/fable-review.sh:35 · harness-source/playbooks/review.md:114 | 범위 밖(감사: 명세에 기록 요구 없음, high는 명세 §2와 일치 → 채택 철회) | — | |
| Q4 백그라운드 리뷰어가 PM 턴 종료와 함께 죽을 수 있음 | 1 | opus(OQ5) | prompts/3-harness.md:8 | 채택(낮음) | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| Q5 빈 리뷰가 성공으로 끝남 | 1 | opus(OQ6) | runner/fable-review.sh:39 | 채택(낮음) | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| Q7 문서의 "익명화" 주장과 실제 불일치 | 1 | opus(OQ8) | docs/results/setup/v3/README.md:3 · selftest-arm3-manifest.json:24(감사가 줄 번호 정정) | 범위 밖(감사: 명세와 연결 없음) — 문구만 사실대로 정정 | — | |
| Q1 상한 합산 범위 | 1 | opus(OQ2) | scripts/capture_turn.py summarize | open question → 메인 판정: run의 모든 역할 포함(spec §2 "run당 input+output"), 문서화 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| Q2 첫 턴 타임아웃 3600초 | 1 | opus(OQ3) | scripts/start_cycle.py:48 | 범위 밖(조건 간 같은 값 — 한계로 기록) | — | |
| R13 워커·리뷰어의 웹 사용 기록 경로 없음(구 Q6) | 1 | opus(OQ7)·감사 | runner/launch.py:810 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| Q8 task 01 커밋 경계 | 1 | codex(OQ2)·opus(OQ9) | packet 밖 | 기각 — 메인이 원 저장소에서 `94cbc30..HEAD` codex 경로 diff 0 확인 | — | |
| R12 파일럿 통과 증명이 증거 파일 존재·해시만 확인(구 Q9) | 1 | codex(OQ1)·감사 | scripts/freeze_readiness.py:125-138 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R14 preflight가 파일럿 전에도 pilot_passed 요구 — 명세 순서로 통과 불가 | 1 | 감사 | scripts/preflight.py:9-11 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| R15 대리 사용자 사용량이 run 결과에 미합산 | 1 | 감사 | scripts/consult_contract.py:113-120 · scripts/capture_turn.py:54-65 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| A1 세 조건 첫 턴 병렬 실행 | 1 | 감사 | scripts/start_cycle.py:101-102 | 기각 — 명세 §0 "이 머신에서 조건 순서대로 하나씩"은 부하 측정 항목이고, 구현 병렬은 docs/experiment-plan.md 진행과 반복 절의 설계 | — | |
| T1 역할 모델 검증이 요청값 대비 상대 비교 | 1 | blind 테스트 | scripts/freeze_readiness.py:74-76,104-107 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| T2 동결 해시가 현재 입력과 달라도 사이클 시작 | 1 | blind 테스트 | scripts/start_cycle.py:31-40 · scripts/preflight.py:18 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| T3 대리 사용자 미관측 사용량을 0으로 기록 | 1 | blind 테스트 | scripts/consult_contract.py:105,116 | 채택 | fixed_in_loop 1 (2회차 두 리뷰어 재지적 없음) | |
| S1 대리 사용자 입력이 계획서(2개)보다 많은 4개 문서 | 2 | codex(F1) | scripts/consult_contract.py:19 · docs/experiment-plan.md:26 | 기각 — `docs/answer-policy.md:3`이 4개 입력을 명시 허용, 사용자도 4개 유지. 계획서 :26과의 표현 차이와 3번 조건의 정보 이득은 한계로 기록 | — |  |
| S2 실패한 feedback 뒤 새 feedback 허용 → 전달된 피드백 미계수 | 2 | opus(F1) | runner/launch.py:927 | 채택(중간) | fixed_in_loop 2 (3회차 두 리뷰어 재지적 없음) |  |
| S3 상한 게이트가 하한값 토큰으로 판정 | 2 | opus(F2)·codex(OQ2) | runner/launch.py:929-941 | 채택(중간) — 수정 보류: CLI 보고 usage를 하한으로 쓰려면 재개 세션에서 그 값이 호출당인지 누적인지 알아야 함(감사 지적, 파일럿으로만 확인 가능). 파일럿 뒤 결정 | 보류(파일럿 뒤) |  |
| S4 재동결(--replace)이 변경을 흡수하고 pilot_passed를 붙임 | 2 | opus(F3) | scripts/freeze_readiness.py:165-257 | 채택(중간) | fixed_in_loop 2 (3회차 두 리뷰어 재지적 없음) |  |
| S5 진행자·판정 어댑터 토큰의 기록 경로 없음 | 2 | opus(F4) | runner/launch.py:47 · docs/runbook.md | 채택(중간) — 1회차부터 열려 있던 항목 | fixed_in_loop 2 (3회차 두 리뷰어 재지적 없음) |  |
| S6 대리 사용자 호출·selftest가 남은 run 시간으로 제한되지 않음 | 2 | codex(F3)·opus(F5) | scripts/consult_contract.py:75,120 · runner/launch.py:1006-1021 | 채택(낮음) | fixed_in_loop 2 (3회차 두 리뷰어 재지적 없음) |  |
| S7 실패했거나 비어 있는 상담도 answer 턴을 허가 | 2 | opus(OQ3)·codex(F2)·감사 | runner/launch.py:915-920 · scripts/consult_contract.py:102-137 | 채택(중간, 감사가 승격) — 상담 완료 판정과 answer 허가가 같은 기준을 쓰도록. codex F2의 "사용량 누락"은 기각(usage.json이 유료 호출 전에 쓰임) | fixed_in_loop 2 (3회차 두 리뷰어 재지적 없음) |  |
| S8 파일럿 init 이미지 기본값이 태그, runbook에 digest 전달 명령 없음 | 2 | opus(OQ4)·감사 | runner/launch.py:1246 · docs/runbook.md:101-103 | 채택(중간, 감사가 승격) | fixed_in_loop 2 (3회차 두 리뷰어 재지적 없음) |  |
| S9 3번 프롬프트 "모든 워커는 Opus" 문구 | 2 | opus(OQ6) | prompts/3-harness.md:5 | 기각(감사: 문구가 곧바로 구현·테스트 설계·리뷰로 한정) — 메인 채택 철회 | — |  |
| A2 피드백 30회 도달 뒤에도 상담·answer 가능 | 2 | 감사 | runner/launch.py:938-940 | 기각 — 30회까지는 허용이고 검열은 31번째를 전달해야 할 때다. 30번째 피드백 턴에서 PM이 낸 질문에 답을 전하는 것은 정상 경로 | — |  |
| A3 파일럿 통과 조건에 워커 effort 검사 없음 | 2 | 감사 | scripts/freeze_readiness.py:178-190 · docs/runbook.md:94 | 기존 대기 항목(R5) — effort가 기록에 남는지 자체가 유료 실행으로만 확인 가능. 파일럿에서 관측되면 통과 조건에 추가 | 보류(파일럿 뒤) |  |
| Q9 8시간 기준에 평가 시간 포함 | 2 | opus(OQ1) | runner/launch.py:934-936 | 메인 판정: 포함(계획서 :51 "벽시계 8시간") — 사용자 보고 | — |  |
| Q10 마지막 턴이 상한을 넘긴 완료 run의 판정 | 2 | opus(OQ2) | runner/launch.py:970-976 | 사용자 결정: 완료 + 초과 표시 → 수정 | fixed_in_loop 2 (3회차에서 U2·U7·U8로 후속 지적) |  |
| Q11 승인 ①②의 기계적 게이트 · consult/turn 동시 실행 잠금 | 2 | codex(OQ1)·opus(OQ7) | scripts/start_cycle.py:102-123 | 기각 — 승인은 사용자와 메인 사이의 절차, 실행은 진행자 하나가 순서대로 | — |  |
| U1 `--readiness` 경로가 없으면 동결 전 분기로 떨어져 이미지 고정 우회 | 3 | codex(F1)·opus(F4) | runner/launch.py:308-311 | 채택(중간) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U2 종료 기록이 status만 쓰고 커밋 단계가 요구하는 증거 4칸을 쓰는 도구 없음, 판정이 인자로만 들어옴 | 3 | opus(F1)·codex(OQ1) | scripts/finish_run.py:55 · scripts/commit_run.py:38-40 | 채택(중간) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U3 진행자 비용을 run 단위로 기록할 수 없음 → 미기록 또는 중복 합산 | 3 | opus(F2·OQ1)·codex(OQ2) | scripts/record_evaluation_cost.py · docs/runbook.md:89-94 | 채택(중간) — 기록 단위는 사용자 결정 | fixed (실험 단위 평가 비용 장부 — 사용자 결정) |  |
| U4 파일럿의 기록 위치·종료 절차 없음 → 사이클 1의 3번 기록과 섞임 | 3 | opus(F3) | scripts/capture_turn.py:36-43 · docs/runbook.md:128 | 채택(중간) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U5 runbook 종료 상태 표의 깨진 행·누락 행 | 3 | opus(F5) | docs/runbook.md:69 | 채택(낮음) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U6 `--cost-usd` 범위 검증 없음 | 3 | opus(F6) | scripts/record_evaluation_cost.py | 채택(낮음) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U7 종료 기록을 다시 실행하면 종료 상태·초과 표시를 덮어씀 | 3 | opus(OQ2)·blind 관찰 | scripts/finish_run.py:50-58 | 채택(낮음) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U8 대리 사용자 호출 직후의 상한 초과도 "완료 + 초과"가 됨(결정 문구는 마지막 턴) | 3 | opus(OQ3)·감사 | scripts/consult_contract.py:165 · scripts/finish_run.py:32 | 채택(낮음, 감사가 승격) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U9 recovery 턴의 프롬프트 내용에 제약 없음 → 새 판정 피드백을 recovery로 실어 보낼 수 있음 | 3 | opus(OQ4)·감사 | runner/launch.py:1007,1102 | 채택(낮음, 감사가 승격 — 메인 기각 철회). 수정 방향: recovery는 이어 가는 턴과 같은 프롬프트만 | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| U10 대리 사용자 호출이 run manifest의 고정 이미지와 대조하지 않음 | 3 | 감사 | scripts/consult_contract.py:73-90 | 채택(중간) | fixed (종료 경로 재설계 §10 구현 `65b23c3`→`1c8dbf3`, 추가 리뷰에서 재지적 없음) |  |
| V4 --replace가 frozen_at·baseline_sha를 덮어씀 | 3 | opus(OQ5) | scripts/freeze_readiness.py:278-284 | open question — superseded 파일에 원본이 남음 | — |  |
| V5 init의 contract hash가 readiness 복사본 · requestId 충돌 요청이 상한 계산에서 빠짐 | 3 | blind 관찰 | runner/launch.py | 앞의 것은 기각(사이클 게이트가 변경을 막음), 뒤의 것은 S3(파일럿 뒤 보류)에 합침 | — |  |
| W1 3번 조건의 필수 역할에 대리 사용자 없음 | 4(추가) | codex(F1)·opus(F1)·blind 테스트 | scripts/finish_run.py:35 · scripts/freeze_readiness.py:81,124-126 | 채택(높음) | fixed (`423cce6`→`e4731dd`, codex 타깃 재점검 + 보완 2건, 명세 테스트 통과) |  |
| W2 호출 전 self-test 실패가 run에 기록되지 않아 격리 칸이 참이 될 수 있음 | 4(추가) | codex(F2) | runner/launch.py:1152-1154 · scripts/freeze_readiness.py:148-156 | 채택(중간) | fixed (`423cce6`→`e4731dd`, codex 타깃 재점검 + 보완 2건, 명세 테스트 통과) |  |
| W3 근거 해시를 계산한 뒤 capture_turn이 기록 폴더 파일을 다시 씀 | 4(추가) | opus(OQ6)·감사 | scripts/capture_turn.py:86-92 | 채택(낮음) — 파일 이름 금지 대신 순서 변경(capture 뒤에 종료 기록, 명세 §10.1 수정). 감사: 이름 금지는 근거 부족 | fixed (`423cce6`→`e4731dd`, codex 타깃 재점검 + 보완 2건, 명세 테스트 통과) |  |
| W4 호출 전 차단 + 전부 PASS를 거부하면 종료 상태를 얻을 길이 없음 | 4(추가) | opus(OQ1)·감사 | scripts/finish_run.py:95-99 | 채택(중간, 감사가 승격) — 메인의 "거부 유지" 판단 철회. 통과는 판정표만으로, 상한 기록은 표시(명세 §10.2 수정) | fixed (`423cce6`→`e4731dd`, codex 타깃 재점검 + 보완 2건, 명세 테스트 통과) |  |
| W5 판정표의 기준마다 근거 필수(통과가 아닌 종료 상태도) | 4(추가) | codex(OQ1)·감사 | scripts/finish_run.py:71-86,178 | 채택(낮음, 감사가 승격 — 메인 기각 철회) | fixed (`423cce6`→`e4731dd`, codex 타깃 재점검 + 보완 2건, 명세 테스트 통과) |  |
| W6 같은 세션의 어댑터 작성 2건째를 기록할 수 없음 | 4(추가) | codex(OQ2)·감사 | scripts/record_evaluation_cost.py:85-93 | 채택(낮음, 감사가 승격) | fixed (`423cce6`→`e4731dd`, codex 타깃 재점검 + 보완 2건, 명세 테스트 통과) |  |
| X1 timeout으로 끊긴 마지막 턴 뒤의 통과 · 커밋 뒤 정정 절차 · judged_by 인증 | 4(추가) | opus(OQ2·OQ3·OQ4) | scripts/finish_run.py | X1은 W4의 새 규칙에 포함(판정은 산출물에 대한 것). 커밋 뒤 정정은 runbook 절차로(supersede 뒤 commit_run 재실행). judged_by 인증은 기각(기계로 인증할 수단 없음) | — |  |

## 생략한 검증 (긴급 경로 빚 — DEBT=1인 동안 정본)

- 없음(긴급 경로 아님).

## 사용자 대기 항목

- [x] (2026-11-18 00:00 완료 — 파일 형식만 확인, 값 미열람) Claude 구독 토큰 재발급(`claude setup-token`) 후 저장소 밖 env 파일에 보관 — 절차는 `payment-settlement/claude/docs/auth-token.md`·`runbook.md`. 인증 스모크(task 05)의 선행 조건.

## 완료 요약 (마감 시)

