# log — Claude 조건 실험 폴더·실행기 준비

> 명세: `requirement-spec.md` (stakes 높음 · auto). 메인이 단일 writer.

## 타임라인

| 시각 | 사건 | 결과/결정 |
|------|------|----------|
| 2026-09-23 14:3x | 실험 전수 확인(L0) — README/docs/prompts/runner/measurement/recording/scripts, 30 run.json, 스모크 로그 | Codex 30 run NOT_STARTED, 실행기 스모크 401 실패, preflight 9항목 미해결, 판정기 skeleton |
| 2026-09-23 14:4x | claude-code-guide 워커로 headless 문서 조사 + 로컬 실측(빈 CLAUDE_CONFIG_DIR + --bare → 인증 실패, transcript usage 키 확인) | CLAUDE_CONFIG_DIR로 설정·자격증명 격리 가능, --bare는 OAuth 불가, 서브에이전트 비용 부모 합산 |
| 2026-09-23 14:5x | 하네스 스냅샷 점검 | settings.json이 미존재 훅 2개 참조(scope-guard·template-guard), 훅 본체는 실배포와 동일 |
| 2026-09-23 15:0x | 인터뷰 Q/A 7개 + D0~D2·모드 합의 | 컨테이너 CLI · high 고정 · codex 래퍼 허용 · auto |
| 2026-09-23 14:50 | 작업 트리 변화 감지 — Codex 세션이 `codex/`로 이전 중(HEAD 9f71dd4, 719 삭제 미커밋) | 명세 경로 보정: `claude/` = `codex/` 배치 미러. `claude/`·docs/plans 외 쓰기 금지 유지 |
| 2026-09-23 15:1x | task 01 — `claude/` 뼈대 생성: codex 측 template·compose·prompts(4)·harness-source·lab.py·commit_run.py 바이트 복사(diff -rq 동일), prepare/preflight/verify_setup 신규, Claude용 3-harness.md, 30 starter 복제 + 기록 init + schedule | verify_setup PASS(1·2 프롬프트 SHA = codex, 3은 의도적 상이), preflight exit 2(미확인 13항목 나열) |
| 2026-09-23 15:1x | 컨테이너용 하네스 배선 파일: `runner/harness-settings.json`(실배포 hooks에서 orca 제외 추출)·`runner/harness-bootstrap-CLAUDE.md`(글로벌 부트스트랩 복사). 스냅샷 settings.json은 미존재 훅 참조라 미사용 | 설계 §10을 spec에 추가, workspace git init 필요성 발견(gate-guard docs 판정이 git toplevel 의존) |
| 2026-09-23 15:2x | Dockerfile + codex 래퍼 작성, 이미지 빌드 시작(백그라운드) · 설계 선검증 워커(Opus) 발주 | 진행 중 |
| 2026-09-23 15:3x | 이미지 `payment-claude-runner:2.1.280` 빌드 성공(34beff70ce4b, USER node). 무네트워크 sanity: claude 2.1.280·codex 0.155.1·jq/flock/realpath/perl/git/java21 존재, 래퍼가 --ephemeral 제거 확인 | OK |
| 2026-09-23 15:4x | task 02 — `claude/runner/launch.py`(init/turn/collect/build_events/redact/scrub) + `test_launch.py` 8건 | 1차 실행 1 error(테스트 fixture run_id 불일치) → 수정 후 8/8 OK. 합성 이벤트가 `measurement/normalize.py --provider anthropic` 통과(input 60=10+20+30, cached 30, write 20) |
| 2026-09-23 15:5x | 설계 선검증 워커(Opus·high, 215k tokens) 회수 — F1~F19 + open question 8건 | 아래 ledger. 고: F1·F2·F4·F7·F10 전부 채택 |
| 2026-09-23 16:0x | task 02 재작성: launch.py(HOME/CONFIG 명시·selftest·staging 원자 init·역할별 파일·사본 일치·request-ids 원장·finally scrub 실패 승격·volume 목록·normalize 서브커맨드·max-turns/budget) + Dockerfile(ENV HOME/CLAUDE_CONFIG_DIR, perl 제거) + test_launch 10건 | 1차 2 error(staging을 /tmp에 생성 → rename 실패) → dest.parent 안 생성으로 수정, 10/10 OK. preflight bool 엄격화·verify_setup 정직화(fixture_verified_here=false + codex 증거 SHA 인용)·settings provenance·3-harness 환경 각주 |
| 2026-09-23 16:1x | 이미지 재빌드(HOME/CLAUDE_CONFIG_DIR ENV) → 구 스모크 run·volume 제거 → 새 launcher로 arm1·arm3 스모크 run init | arm1 selftest PASS(cli_args_rejected=0, workspace_git_exit=0). **arm3 selftest PASS: hooks_checked=1, gate_l1_write_exit=2, gate_docs_write_exit=0, codex_on_path=1** — 훅 스크립트 차단 메커니즘이 컨테이너에서 성립(무과금). `-p` 모드에서 Claude Code가 훅을 실제 호출하는지는 유료 스모크 대상 |
| 2026-09-23 16:2x | 문서 작성: claude/README.md · docs/runbook.md · docs/isolation-and-measurement.md · docs/results/README.md · runner/README.md | 스모크 결과(setup/claude/)는 실행 후 추가 예정 |
| 2026-09-23 22:0x | 토큰 파일 확인(자리표시 2회 → 사용자가 setup-token 결과를 채팅으로 전달 → 파일 저장, 채팅 노출은 auth-token.md에 기록) | env-file 600·단일 키·108자 OK |
| 2026-09-23 22:0x | **유료 스모크 ① arm1 RUNNER_OK**: subtype success, session_id 일치, usage{input 2, cache_write 5800, cache_read 9046, output 10}, total_cost_usd 0.0628, modelUsage.canonicalModel=claude-opus-5(contextWindow 1000000), transcript 1파일·requestId 1, main_delta 0, credentials_file_in_volume=false | A1 성립 |
| 2026-09-23 22:0x | **유료 스모크 ③ arm3 훅 부정 테스트**: SessionStart hook_started/hook_response 이벤트 관측(session-mode-guard 주입문 그대로), PreToolUse gate-guard가 Write 차단 — 응답 `HOOK_BLOCKED: [gate-guard] 요구사항 명세 미합의(SPEC=0)…`, Probe.kt 미생성, workspace/.claude/lazymode 상태파일 생성. 모델 claude-fable-5-1, cost 0.371, requestId 2, main_delta 0 | **A2 성립 — headless에서 원본 훅이 실제 강제됨** |
| 2026-09-23 22:0x | **유료 스모크 ② arm1 --resume**: RUNNER_OK_RESUMED, 같은 session_id, transcript 파일 1개(fork 없음), cache_read 14846, request-ids 원장 2건(중복 0), cost 0.0716 | resume 대화 유지·dedupe 확인 |
| 2026-09-23 22:0x | **스모크 ④ arm3 컨테이너 안 codex exec**(playbook 플래그 `--sandbox read-only --ephemeral -o`): rc 0, CODEX_IN_CONTAINER_OK, 래퍼가 --ephemeral 제거 → sessions 1개 보존, turn.completed usage 관측(input 14157/cached 12160/output 9), 모델 갱신 timeout 경고(+~14s, playbook 실측과 동일) | F5c ①②③ 해소(read-only auth로 인증 성공) |
| 2026-09-23 22:2x | 리뷰 loop 1 발주: Opus 워커(미러 읽기 전용) ∥ codex(`--sandbox read-only --ephemeral`, cwd=미러) + blind 테스트 워커(spec+README만) | codex 1차 실패: bwrap 중첩(`loopback RTM_NEWADDR`)으로 파일 미열람 + 프롬프트의 `_packet/` 문구 오독 → review.md ①실패 분기대로 1회 재시도(문구 수정, Claude Bash 샌드박스 해제·codex는 read-only 유지) |
| 2026-09-23 22:2x | Codex 세션이 재구성을 커밋(dfbdaaa) — `claude/`·`docs/plans` 미포함 확인. 문서 갱신(runbook 체크리스트 스모크 항목 [x], README 상태, results README) + 메모리 2건 저장 | — |
| 2026-09-23 22:3x | codex 2차(호스트, Claude 샌드박스 해제)도 `bwrap: loopback: Failed RTM_NEWADDR` — 호스트에서 codex read-only 샌드박스 자체가 불가 | review.md ①실패 분기: 동등 대체 리뷰어 = **컨테이너 안 codex**(스모크 ④에서 read-only 샌드박스 정상 확인)에 같은 packet·미러(ro bind) 제공. 호스트 codex 리뷰 경로는 이 머신에서 사용 불가로 기록 |
| 2026-09-23 22:4x | 컨테이너 안 codex도 `bwrap: No permissions to create a new namespace`(cap-drop=ALL) — 스모크 ④는 도구 미사용이라 통과했던 것. **arm 3 실험에서 codex 리뷰가 파일을 읽으려면 bypass가 필요** → 3-harness 각주(user override)로 이미 대비, 문서에 실측 추가 예정 | 리뷰 3차: 대상 파일 전문(줄 번호)을 인라인한 packet으로 도구 없이 codex 실행(비대칭 입력 — 미러 탐색 불가) |
| 2026-09-23 22:5x | codex 3차(인라인 packet, 도구 없음) 회수 — 잠정 finding 7건(C1 파일명 토큰, C2 usage_complete가 docker cp 실패·빈 이벤트 무시, C3 arm3 codex 리뷰어 미관측 무시, C4 incomplete requestId가 원장에 영구 소비, C5 selftest가 settings 배선 미검증·CLI 127 통과, C6 init 실패 시 volume 잔존, C7 preflight arm 타입·strict-zero-cache 허용) + 기타(read_token 중복 할당, verify_setup가 lab/commit_run 패리티 미검사, runbook record.py 경로) | 전부 채택·수정: scrub이 파일명도 치환, 단일 할당 강제, 실패 시 volume rm, selftest에 settings SHA 일치 + cli 126/127 실패, usage_complete에 copy rc·이벤트 존재·codex 세션 복사 반영, 원장에 완전 이벤트만, preflight arm/policy 검증, verify_setup 스크립트 패리티, runbook 경로. 테스트 12건 OK |
| 2026-09-23 23:0x | blind 테스트 워커(Opus, spec+README만 열람, 87k tokens) 회수 — 50건 작성 | 실행: 1차 39/50(str 경로·runs 기본값 미수용 등 인터페이스 견고성 → 채택·수정: Path 강제, runs 기본, events `file` 필드 복원) → 2차 44/50. 잔여 6건 = 가정 불일치(A-5 runs가 REPO 기준 / arm 1에 --harness 전달 / codex_auth 없이 arm 3 turn / 사본 usage 불일치 시 이벤트 1건 기대(설계는 0건+incomplete) / fixture 버그: usage=None이 기본값으로 대체) — 실결함 0. 파일: scratchpad/blind/test_blind.py |
| 2026-09-23 23:1x | Opus 리뷰(loop 1, 미러 탐색) 회수 — F1~F12 + open question 4 | 메인 종합: O-F8(docs/experiments 부재)은 **기각 — packet 범위에서 제가 제외한 경로**(실파일 존재 확인), 단 존재 검사를 verify_setup에 추가. O-F2(웹 도구 비대칭)는 사용자 결정 D5로 승격 + 관측 기록 채택. O-F11·O-F12는 codex C6·C4로 이미 수정 → 중복 병합. 나머지 채택 |
| 2026-09-23 23:3x | codex 종합 감사(인라인) 회수 — 9 note. 주의: 감사 packet에 Opus 원문 추출 실패(리뷰어 B 부재 상태로 감사) → 감사의 '근거 부족' 지적 일부는 그 결함. 실질 채택: 교차검증 delta 게이트가 spec 관측치 문구와 충돌 → spec/문서 문구 갱신 · 리뷰어 사용량은 '정규화 완료'가 완전성 기준 → `reviewer_usage_pending` · scrub은 cleanup 예외에도 실행 보장 · ledger 상태 정리 | 반영 |
| 2026-09-23 23:4x | loop 1 수정 적용(launch.py: NO_SUBAGENT_TOOLS+init tools 단정·훅 추출 fail-closed·시딩 해시 재검증·Bash 프로브 3종·usage_complete 강화·실패 stub/턴 번호·tri-state·discard·web_requests; scripts: preflight/verify_setup; docs: runbook 복구·D5·isolation 완전성 정의; 예시 manifest 플래그) | 단위테스트 1차 1 fail(disallow 목록 기대값) → 14/14 OK. self-test 재실행: arm3 PASS(hooks_checked=8, harness_hash_match=1, bash_hooks_benign 0/0/0, teeth exit 2, git_guard_push=ask), arm1 1차 FAIL(cli_init_missing — `--disallowedTools` 가변 인자가 프롬프트 흡수) → 인자 순서 수정 후 PASS(denied tools 부재 확인). 호스트 gate-guard가 패치 텍스트의 `tee .claude/lazymode` 문자열을 오탐 차단 → 패치를 파일로 실행 |
| 2026-09-23 23:5x | 사용자 요청: 문제 상황 문서화 → `claude/docs/issues-log.md` 작성(10항목 + 이후 기록 형식) | — |
| 2026-09-24 00:0x | loop 2 packet(base 9f71dd4, 561 untracked 본문, 0.98MB, template/harness-source는 미러에만) + 미러 mirror2-tz0KfP + loop1-fixes 참조 51행 → Opus 리뷰 ∥ codex 인라인(169KB) 발주. blind 재실행 44/50 → 43/50(신규 1건 = disallow 목록에 Workflow 추가에 따른 기대값 차이, spec 문구 갱신) | 진행 중 |
| 2026-09-24 00:2x | codex loop 2(인라인, 53k tokens) 회수 — 현재 결함 6건: X1 실패 턴 세션 존재 미추적(resume 판정 오류) · X2 원장 선기록으로 예외 시 사용량 영구 누락 · X3 codex 세션 부재를 '미시도'로 오인 · X4 해시 검증기 크래시가 match로 통과 · X5 Bash 프로브 exit 1/127·push ask 미단정 · X6 manifest actual_model 미기록. loop-1 fix 39건 중 32 closed, 7 not closed(위 6건에 귀속) | 전부 채택 → 수정: session_exists(volume transcript 실측)로 resume 판정·result.status ok/failed, 원장 append를 result.json 이후로 + normalize가 모든 턴의 role 파일 사용(summarize dedupe), 래퍼가 wrapper-calls.log 기록 → attempts_this_turn·reviewer_usage_pending, 검증기 exit·EXPECTED-EMPTY 실패, 프로브 정확 exit·push ask 필수, record_models로 manifest actual_role_models/actual_model. 테스트 15/15 OK, arm3 self-test PASS(VERIFIED 34 파일), session_exists 실측 정상 |
| 2026-09-24 00:5x | Opus loop 2(미러, 200k tokens) 회수 — 신규 9건. 메인 종합: F-C(post-call 실패 status)·F-E(해시 검증기 공허 통과)는 codex X1·X4와 동일 결함 → 병합(이미 수정). F-A allow-list 채택(`--tools` + 부분집합 단정 + 1·2번 sidechain 경보), F-B 증거 재기록 채택(v2 init 무과금), F-D **Codex schedule이 cycle 방식으로 바뀌어 '같은 순열' 주장 거짓** → codex schedule에서 파생 + verify_setup 패리티, F-F helper timeout, F-G/F-I 문서, F-H 손상 result.json | 수정 적용 중 |
| 2026-09-24 01:1x | loop 2 수정 완료: 테스트 15/15 OK · arm1 self-test 실측 tools = Bash,Edit,Glob,Grep,NotebookEdit,Read,ToolSearch,WebFetch,WebSearch,Write(allow-list 부분집합) · v2 init(arm1/arm3) PASS → setup/claude에 post-hardening manifest + summary 세대 라벨(24파일 해시) · schedule을 codex에서 파생(verify_setup parity schedule=True) · issues-log §11 | O2-B fixed. **loop 3(최종) packet**: 563 untracked 본문 1.0MB, 미러 mirror3-ZWhpXW, loop1/2 fixes 참조 → Opus ∥ codex 인라인(191KB) 발주 |
| 2026-09-24 01:3x | codex loop 3(최종, 인라인 65k tokens) 회수 — 신규 5건: L3-1 중단된 codex 호출 미기록(래퍼가 실행 후 로그) · L3-2 복구 턴 재수집 시 requestId 중복(정규화 dedupe 불가) · L3-3 유출 감지 후에도 status ok · L3-4 helper timeout 시 컨테이너 잔존 · L3-5 runbook 복구 안내가 실제 판정(transcript 존재)과 불일치. loop-2 fix 15건 중 10 closed, 5 not closed(위에 귀속) | 전부 채택·수정: 래퍼 started/exit 2줄 + attempts_unknown, reconcile_events()로 normalize 입력 병합(첫 등장 usage·worker 단조·충돌 제외 보고), status는 finally에서 최종 판정(cli_status 분리, finalize_result 원자 교체), helper 이름 부여 + timeout 시 docker rm -f, runbook 복구 절 재작성. **규정상 4번째 전체 루프 없음 → 타깃 재점검만 수행 예정, 잔여 리스크 보고** |
| 2026-09-24 01:5x | Opus loop 3(최종, 미러 207k tokens) 회수 — **차단 결함 0**, loop-2 fix 15/15 closed(O2-B 포함, 해시 재계산·schedule 30행 대조), loop-1 회귀 없음. 잔여 7건: N-A codex 자격증명이 scrub 대상 밖(중) · N-B session_id 불일치가 완전성 미반영 · N-C 래퍼 로그 복사 실패=0(L3-1과 동일) · N-D 하네스 추가 파일 미탐지 · N-E scrub 전체 읽기 메모리 · N-F selftest가 --runs 무시 · N-G 원장 키 7개 누락 | 전부 채택·수정: needle set(Claude 토큰 + codex auth.json 비밀값, 파싱 불가 시 거부)·청크 스트리밍 스캔, session 불일치 → incomplete, attempts_unknown 시 baseline 미갱신, 시딩 subtree 추가 파일·override·workspace settings 검사(harness_no_extras), runs 기준 staging, prepare.py 백필. 테스트 18/18 OK, v3 init(arm1/arm3) PASS(harness_no_extras=1) → 증거 재기록 |
| 2026-09-24 02:0x | v3 init 증거 재기록 후 verify_setup FAIL — Codex 세션이 `codex/scripts/commit_run.py`를 수정(미커밋: 시행 기록에 `issues.md` 필수 추가) → 사본 재동기(cmp 동일), runbook에 issues.md·재동기 규칙 기재 | verify PASS 복귀. 타깃 재점검(codex 인라인) + 완료 요약 워커(Opus) 발주 |
| 2026-09-24 02:1x | 타깃 재점검(codex 인라인) 회수 — L3-2·L3-4·L3-5·N-B·N-E·N-F·N-G closed / not closed 4: L3-1 래퍼 로그 실패 시에도 codex 실행(fail-open), L3-3 scrub 예외 시 finalize 미실행, N-A(신규) 예외 문자열에 자격증명 파일명이 마스킹 없이 result.json에 기록, N-D `__pycache__` 문자열 포함 경로 전부 면제 | 4건 수정: 래퍼 started 기록 실패 시 exit 97·미실행, scrub을 try로 감싸 scan_error도 failed·finalize 보장, mask_text()로 오류 문자열 마스킹, 경로 컴포넌트 기준 면제. 회귀 테스트 추가. **이 4건은 재점검 없이 단위테스트·정적 확인으로 종료(루프 상한)** |
| 2026-09-24 02:2x | 마감: 래퍼 fail-closed 이미지 재빌드 확인(rc 97), 최종 self-test PASS(arm1/arm3), blind 42/50(가정 불일치만), measurement-log 1행, 완료 요약(Opus 워커) 삽입, 리뷰 미러 삭제 | 작업 종료 — 본 실험 미개시 |

## 리뷰 ledger

- review packet: (루프마다 기록)

- review packet(구현 듀얼 리뷰 loop 1): base 9f71dd4 / OUT=/tmp/claude-1000/review-xzyc8E / mirror=/tmp/claude-1000/mirror-0SdMIS / 범위 = payment-settlement/claude/** (arm 복제본·docs/experiments·template/.kotlin 제외, 641 untracked 중 610 본문 포함, packet 1.35MB) + codex/runner·scripts·docs 일부를 참조용으로 미러에 추가 / 보안 스캔: 히트 전부 문서 접두 언급·훅 테스트 fixture(오탐) → 통과
- review packet(설계 선검증 loop 0): base 9f71dd4 + 미커밋 트리 / 워커 Opus high / 입력 = spec §10 + claude/runner·scripts 실파일 + codex/runner + measurement + hooks

| id | first_seen_loop | source | 근거(file:line) | disposition | status | fixed_in_loop |
|----|-----------------|--------|-----------------|-------------|--------|---------------|
| F1 | 1 | opus-design | Dockerfile ENV 없음·launch.py docker_command | HOME·CLAUDE_CONFIG_DIR 명시 | fixed | 1 |
| F1b | 1 | opus-design | launch.py:20 | CLAUDE_CONFIG_DIR 명시로 해소 | fixed | 1 |
| F2 | 1 | opus-design | launch.py seed_harness(ls만) | init·turn selftest(훅 존재·bash -n·gate-guard exit 2/0) | fixed | 1 |
| F3 | 1 | opus-design | launch.py:120 중복 플래그 | `--dangerously-skip-permissions`만 유지 + selftest CLI 인자 검사 | fixed | 1 |
| F4 | 1 | opus-design | launch.py:131 arm1/2 서브에이전트 | `--disallowedTools Agent,Task`(Codex 패리티) + manifest.subagents | fixed | 1 |
| F5 | 1 | opus-design | git_init 소유권 | uid=1000 단정 + selftest `git rev-parse` in container | fixed | 1 |
| F5b | 1 | opus-design | git-guard ask in headless | 문서 기록 대상(스모크에서 관측) | deferred-doc | — |
| F5c | 1 | opus-design | review.md §5 codex 경로·샌드박스 | 3-harness 환경 각주 + selftest codex PATH + 스모크 ④(read-only auth 인증 성공, 래퍼 세션 보존) + bwrap 제약 실측(runbook·issues-log) | fixed | 1 |
| F6 | 1 | opus-design | git_init check=True half-created | staging 디렉터리 + 원자 rename + RuntimeError | fixed | 1 |
| F7 | 1 | opus-design | per-event role vs normalize --role | events.role-*.jsonl + normalize 서브커맨드 | fixed | 1 |
| F8 | 1 | opus-design | 사본 채택 순서 의존 | requestId별 전 사본 수집·usage 동일성·role monotone | fixed | 1 |
| F9 | 1 | opus-design | cross-check 판정값 | main/all delta 관측치로만 기록 | fixed | 1 |
| F10 | 1 | opus-design | scrub try 본문·목록만 | finally·run 전체 스캔·SECRET-LEAK.txt·실패 종료 | fixed | 1 |
| F11 | 1 | opus-design | usage_complete 상시 false 위험 | 사유 분류 카운트(unparsable/without_requestId), usage_complete 조건 축소 | fixed | 1 |
| F11b | 1 | opus-design | requestId 중복 usage 미단정 | F8과 동일 수정 | fixed | 1 |
| F12 | 1 | opus-design | volume 잔존·미검사 | 턴마다 volume 파일 목록·credentials 존재 여부 기록; 폐기 절차는 문서 | partial | 1 |
| F12b | 1 | opus-design | env-file 위치·dry-run 우회 | repo 밖 강제 + dry-run에서도 read_token | fixed | 1 |
| F12c | 1 | opus-design | 네트워크 격리 범위 | 문서 1줄(runbook) | deferred-doc | — |
| F13 | 1 | opus-design | 유료 상한 없음 | --max-turns/--max-budget-usd 옵션 | fixed | 1 |
| F14 | 1 | opus-design | seen이 result.json 의존 | request-ids.txt append-only | fixed | 1 |
| F15 | 1 | opus-design | preflight truthy | bool `is not True` + auth whitelist | fixed | 1 |
| F16 | 1 | opus-design | verify_setup 하드코딩 | 파생값 + fixture_verified_here=false + codex 증거 SHA | fixed | 1 |
| F17 | 1 | opus-design | 모델 문자열·[1m] | context_variant=default + 스모크 D4 관측(두 모델 contextWindow 1M) + selftest CLI 인자 수용 | fixed | 1 |
| F18 | 1 | opus-design | 테스트 구멍 | 사본 충돌·requestId 없음·leak 실패·dry-run env 검증 테스트 추가 | fixed | 1 |
| F19 | 1 | opus-design | spec 경로 불일치 | spec §5 경로 수정 | fixed | 1 |
| C1 | 1 | codex | launch.py scrub 파일명·cleanup 예외 시 미실행 | 채택 — 파일명 치환·hits 마스킹 + docker rm을 try로 감싸 scrub 보장 | fixed | 1 |
| C2 | 1 | codex | launch.py usage_complete·docker cp rc | 채택 — copy rc·이벤트 존재 조건 | fixed | 1 |
| C3 | 1 | codex | launch.py codex-sessions 복사 미반영 | 채택 — codex_sessions 관측 + reviewer_usage_pending(세션 존재 시 usage_complete=false, 정규화는 분석 단계) | fixed | 1 |
| C4 | 1 | codex | launch.py request-ids 원장 | 채택 — 완전 이벤트만 기록 | fixed | 1 |
| C5 | 1 | codex | launch.py SELFTEST 배선 미검증 | 채택 — settings SHA 일치·cli 126/127 실패 | fixed | 1 |
| C6 | 1 | codex | launch.py initialize 예외 경로 | 채택 — volume rm 롤백 | fixed | 1 |
| C7 | 1 | codex | preflight.py arm/policy | 채택 | fixed | 1 |
| C8a | 1 | codex | read_token 중복 할당 | 채택 — 정확히 1개 할당 강제 | fixed | 1 |
| C8b | 1 | codex | verify_setup 스크립트 패리티 미검사 | 채택 — scripts/lab.py·commit_run.py 바이트 비교 | fixed | 1 |
| C8c | 1 | codex | runbook record.py 경로 | 채택 — 저장소 루트 기준 경로 | fixed | 1 |
| B1 | 1 | blind-tests | launch.py collect/write_role_files/turn/initialize str 경로·runs 기본값 | 채택(견고성) | fixed | 1 |
| B2 | 1 | blind-tests | events `file` 필드 누락(spec §10.4) | 채택 | fixed | 1 |
| O-F1 | 1 | opus | launch.py --disallowedTools 2개 이름 | 채택 — NO_SUBAGENT_TOOLS(Agent,Task,Workflow) + SELFTEST cli_tools/denied_tool_present + manifest.observed_tools | fixed | 1 |
| O-F2 | 1 | opus | 웹 도구 표면·server_tool_use 미기록 | 기록 채택(result.web_requests·observed_tools·runbook D5) / 끌지 여부 = D5 사용자 결정 | user-deferred | 1 |
| O-F3 | 1 | opus | SELFTEST 훅 추출 실패 시 공허 통과 | 채택 — HOOKLIST heredoc·hooks_checked=n·MIN_HOOKS=8 미만 실패 | fixed | 1 |
| O-F4 | 1 | opus | 턴 직전 하네스 내용 해시 미비교 | 채택 — expected_harness_hashes → /expected/harness-hashes.json → harness_changed 보고·실패 | fixed | 1 |
| O-F5 | 1 | opus | Bash PreToolUse 훅 3개 미검증 | 채택 — bash_hooks_benign(3훅 exit)·teeth exit 2·git_guard_push=ask | fixed | 1 |
| O-F6 | 1 | opus | usage_complete 조건 | 채택 — without_requestId==0·delta(main∨all)==0·reviewer pending (spec §10 문구 갱신) | fixed | 1 |
| O-F7 | 1 | opus | 실패 턴이 run을 wedge (C9 병합) | 채택 — turn_id=디렉터리 수, 성공 턴 기준 resume 판정, status:failed stub, runbook '실패한 턴의 복구' | fixed | 1 |
| O-F8 | 1 | opus | docs/experiments 부재 | **기각** — packet SCOPE 제외 산물(실존) / 존재 검사는 verify_setup에 추가(채택) | fixed | 2 |
| O-F9 | 1 | opus | credentials_file_in_volume tri-state | 채택 — rc≠0 → None | fixed | 1 |
| O-F10 | 1 | opus | 테스트가 repo 루트에 파일 생성 | 채택 — test_env_file_validation이 REPO를 tmp로 patch | fixed | 1 |
| O-F11 | 1 | opus | 고아 volume | C6 롤백 + `discard --run-id`(목록 기록 후 volume rm) | fixed | 1 |
| O-F12 | 1 | opus | 원장 incomplete 소비 | C4와 병합 | fixed | 1 |
| O-S1 | 1 | opus | summary.json files 목록에 usage-summary 누락·invocation.json 미보존 | 채택 — 증거 재생성 | fixed | 1 |
| O-S2 | 1 | opus | run-manifest.example에 FLAGS 키 4개 부재 | 채택 — false 추가 + observed_tools/web_tools/estimated_cost_usd | fixed | 1 |
| O-S3 | 1 | opus | parent_tool_use_id 신호 근거 | open question → [구현 검증] 이연(arm 3 첫 run) | user-deferred | — |
| C9 | 1 | codex | 미완료 턴 재시도 경로 없음 | O-F7과 병합 → 채택 | fixed | 1 |
| X1 | 2 | codex | launch.py turn resume 판정(:501) | 채택 — session_exists() 실측 + status ok/failed | fixed | 2 |
| X2 | 2 | codex | launch.py 원장 append 시점(:564) | 채택 — result.json 이후 append, normalize 전 턴 role 파일 | fixed | 2 |
| X3 | 2 | codex | launch.py codex absent 해석(:581) | 채택 — wrapper-calls.log·attempts_this_turn | fixed | 2 |
| X4 | 2 | codex | SELFTEST 해시 검증기 exit 무시(:144) | 채택 — vrc·EXPECTED-EMPTY·unreadable 실패 | fixed | 2 |
| X5 | 2 | codex | SELFTEST Bash 프로브 단정(:166) | 채택 — 정확 exit 0 세 개·push ask 필수 | fixed | 2 |
| X6 | 2 | codex | manifest actual_model 미기록(:253) | 채택 — record_models() | fixed | 2 |
| O2-A | 2 | opus | launch.py 도구 deny-list(:34,:120) | 채택 — ALLOWED_TOOLS_ARM12 `--tools` + tool_outside_allowlist + 1·2번 worker 이벤트 incomplete | fixed | 2 |
| O2-B | 2 | opus | setup/claude 증거가 강화 전 실행기 산출물 | 채택 — v2 init(무과금) manifest 재기록 + summary evidence_generations 라벨 | fixed | 2 |
| O2-C | 2 | opus | post-call 실패가 success로 집계(:583-606) | X1과 병합(status=ok/failed from returncode) | fixed | 2 |
| O2-D | 2 | opus | schedule이 codex와 불일치(20/30) | 채택 — codex schedule 파생 + verify_setup parity["schedule"] | fixed | 2 |
| O2-E | 2 | opus | harness_hash_match 공허 통과(:144) | X4와 병합 | fixed | 2 |
| O2-F | 2 | opus | helper/CLI probe 무제한 대기(:70) | 채택 — HELPER_TIMEOUT 900·probe timeout 180 | fixed | 2 |
| O2-G | 2 | opus | isolation 문서 72행이 코드와 반대 | 채택 — 문구 갱신 | fixed | 2 |
| O2-H | 2 | opus | 손상 result.json이 이후 턴 차단(:501) | 채택 — failed 취급 + 경로 명시 | fixed | 2 |
| O2-I | 2 | opus | 문서의 disallow 목록·테스트 개수 | 채택 — allow-list 문구·개수는 출력 정본 | fixed | 2 |
| L3-1 | 3 | codex | codex-wrapper.sh:12 실행 후 로그·launch.py 로그 복사 실패=0 | 채택 — started 선기록(실패 시 exit 97 미실행)·exit 후기록·attempts_unknown | fixed(재점검: 2차 수정 후 미재검) | 3 |
| L3-2 | 3 | codex | launch.py role 파일 선생성·normalize 턴별 | 채택 — reconcile_events() 병합 후 정규화 | fixed(재점검 closed) | 3 |
| L3-3 | 3 | codex | launch.py status=cli exit(:654) | 채택 — finalize_result()로 스캔 후 최종 status, cli_status 분리, scrub 예외도 failed·finalize 보장 | fixed(재점검: 2차 수정 후 미재검) | 3 |
| L3-4 | 3 | codex | helper() timeout 시 컨테이너 잔존(:76) | 채택 — 이름 부여·rm -f | fixed(재점검 closed) | 3 |
| L3-5 | 3 | codex | runbook.md:40 복구 안내 | 채택 — transcript 존재 기준으로 재작성 | fixed(재점검 closed) | 3 |
| N-A | 3 | opus | launch.py scrub 단일 needle(:675) | 채택 — codex_secrets()+needle set, 스트리밍, 오류 문자열 mask_text | fixed(재점검: 2차 수정 후 미재검) | 3 |
| N-B | 3 | opus | session_id_confirmed 미게이트(:634) | 채택 — incomplete 사유 + usage_complete 조건 | fixed(재점검 closed) | 3 |
| N-C | 3 | opus | 래퍼 로그 복사 실패=0·baseline 리셋(:602) | L3-1 병합 + baseline 미갱신 | fixed(재점검 closed) | 3 |
| N-D | 3 | opus | 해시 검사 일방향(:154) | 채택 — harness_extra 검사(__pycache__ 컴포넌트만 면제) | fixed(재점검: 2차 수정 후 미재검) | 3 |
| N-E | 3 | opus | scrub 전체 읽기(:465) | 채택 — 청크 overlap 스캔, 히트 시만 전체 치환 | fixed(재점검 closed) | 3 |
| N-F | 3 | opus | selftest ROOT/.runs 고정(:210) | 채택 — workspace 기준 runs 경로 | fixed(재점검 closed) | 3 |
| N-G | 3 | opus | 원장 키 누락·prepare 비멱등(:41) | 채택 — 백필(값 미덮어씀) | fixed(재점검 closed) | 3 |
| parity-settings | 1 | opus-design | 컨테이너 settings 출처 미기록 | harness-settings.provenance.json | fixed | 1 |

## 생략한 검증

- (없음 — 정규 경로)

## 완료 요약


### 1. 무엇이 됐나

`payment-settlement/claude/`가 `codex/` 배치 미러로 존재한다(전체 untracked — 미커밋).

- **starter 30개** `{1,2,3}/core1..10`(template 바이트 동일, `scripts/verify_setup.py`가 검사)
- **시행 원장 30개** `docs/experiments/<arm>/core<N>/run.json` — 전부 `NOT_STARTED`
- **실행기** `runner/`: `launch.py`(905행, init/turn/normalize/discard) · `Dockerfile` · `codex-wrapper.sh` · `harness-settings.json`+`.provenance.json` · `harness-bootstrap-CLAUDE.md` · `test_launch.py`
- **단위테스트 19건 통과**(`Ran 19 tests … OK`, 모델 미호출)
- **문서** `README.md` · `docs/{runbook,isolation-and-measurement,auth-token,issues-log}.md` · `docs/results/`
- **스모크 증거** `docs/results/setup/claude/` — 해시 등재 24개(디스크 25개, summary.json 포함)

검증된 것(유료 4턴, setup 추정 **$0.5058** / `billed_cost=null`): ①arm1 `RUNNER_OK` $0.0628 ②arm1 `--resume` $0.0716(transcript 1파일, fork 없음) ③arm3 훅 부정 테스트 $0.3714 — `HOOK_BLOCKED: [gate-guard] … (SPEC=0)`, `probe_file_created:false` ④컨테이너 안 `codex exec` rc 0, 세션 1개 보존. 4턴 모두 `main_delta` 0, `credentials_file_in_volume:false`, 두 모델 `contextWindow 1000000`(D4 해소).
무과금 self-test(v3): arm1 `selftest_failed=0`·tools 10개 = allow-list 부분집합 / arm3 `hooks_checked=8`·`settings_sha_match=1`·`harness_hash_match=1`·`harness_no_extras=1`·`gate_l1_write_exit=2`·`gate_docs_write_exit=0`·`bash_hooks_teeth_exit=2`·`git_guard_push=ask`.

**설계상 안 한 것**: 30회 본 실험(전부 NOT_STARTED, 자동 시작 없음) · 판정기 C01~C20(Codex 소유) · Docker fixture 재검증(`fixture_verified_here=false`, Codex 증거 인용) · 강화 후 유료 턴 재실행.

### 2. 핵심 diff before/after 스니펫

**(a) arm 1·2 도구 allow-list** — before: `log.md:114` `| O2-A | 2 | opus | launch.py 도구 deny-list(:34,:120) | 채택 — ALLOWED_TOOLS_ARM12 --tools … |`
after `launch.py:37, 331-332`:
```python
ALLOWED_TOOLS_ARM12 = ('Bash', 'Read', 'Edit', 'Write', 'NotebookEdit', 'Glob', 'Grep', 'ToolSearch', 'WebSearch', 'WebFetch')
    if not manifest.get('requested_worker_model'):
        command += ['--tools', ','.join(ALLOWED_TOOLS_ARM12)]              # arms 1/2: allow-list (Codex parity)
```

**(b) SELFTEST gate-guard 프로브** — before: `log.md:56` `| F2 | 1 | opus-design | launch.py seed_harness(ls만) | init·turn selftest(gate-guard exit 2/0) |`
after `launch.py:197-200`(199행 중략):
```bash
  # gate-guard must block an L1 write while SPEC=0 (exit 2) and pass a docs write (exit 0) in a fresh repo
  T=$(mktemp -d); git -C "$T" init -q -b main; (cd "$T" && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m init); mkdir -p "$T/docs/plans/x"
  printf '%s' "{…\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$T/src/Main.kt\"…}" | bash "$CLAUDE_CONFIG_DIR/hooks/gate-guard.sh" >/dev/null 2>/tmp/gg1.err; rc=$?
  report gate_l1_write_exit "$rc"; [ "$rc" = 2 ] || fail=1
```

**(c) `usage_complete` 술어** — before: `log.md:80` `| C2 | 1 | codex | launch.py usage_complete·docker cp rc | 채택 — copy rc·이벤트 존재 조건 |` + `log.md:96` `| O-F6 … delta(main∨all)==0·reviewer pending |`
after `launch.py:772-780`:
```python
            'usage_complete': (not incomplete and result['result'] is not None and proc.returncode == 0
                               and copied.returncode == 0 and bool(events)
                               and stats.get('unparsable_lines', 1) == 0 and result['unparsed_lines'] == 0
                               and stats.get('assistant_without_requestId', 1) == 0
                               and cross['main_delta'] is not None
                               and (all(v == 0 for v in cross['main_delta'].values()) or all(v == 0 for v in cross['all_delta'].values()))
                               and (codex_copy is None
                                    or (not codex_copy['attempts_unknown'] and codex_copy['attempts_this_turn'] == 0
                                        and codex_copy['session_files'] == 0 and (codex_copy['absent'] or codex_copy['rc'] == 0)))),
```

**(d) `finally`의 최종 status** — before: `log.md:126` `| L3-3 | 3 | codex | launch.py status=cli exit(:654) | 채택 — finalize_result()로 스캔 후 최종 status | 미재검 |`
after `launch.py:807-824`:
```python
        scan_error = None
        try:
            hits = scrub(dest, needles)
        except Exception as exc:                       # the scan itself failed: coverage unknown → the turn cannot be ok
            hits, scan_error = [], mask_text(str(exc)[:500], needles)
        if hits:
            (output / 'SECRET-LEAK.txt').write_text('credential found and redacted in: ' + ', '.join(hits) + '\nrotate the token now\n')
        in_flight = sys.exc_info()[1]
        final = 'failed' if (hits or scan_error or in_flight is not None or (result and result.get('cli_status') != 'ok')) else 'ok'
        try:
            finalize_result(output, result, final, error, hits)
        finally:
            if (hits or scan_error) and in_flight is None:
                raise RuntimeError('credential leaked into preserved files or the scan failed; see result.json / SECRET-LEAK.txt')
```

**(e) 래퍼 started/exit(fail-closed)** — before: `log.md:124` `| L3-1 | 3 | codex | codex-wrapper.sh:12 실행 후 로그 | 채택 — started 선기록·exit 후기록 | 미재검 |`
after `codex-wrapper.sh:15-22`:
```bash
# the attempt is durable BEFORE codex launches, so an interrupted call still leaves evidence
if ! printf '%s started argc=%s stripped=%s\n' "$CALL" "$#" "$stripped" >> "$LOG"; then
  echo "[codex-wrapper] cannot record the attempt in $LOG — refusing to launch codex (usage would be unobservable)" >&2
  exit 97
fi
/usr/local/bin/codex-real "${args[@]}"
rc=$?
printf '%s exit=%s\n' "$CALL" "$rc" >> "$LOG" 2>/dev/null
```

**(f) `reconcile_events`** — before: `log.md:125` `| L3-2 | 3 | codex | launch.py normalize 턴별 | 채택 — reconcile_events() 병합 후 정규화 | closed |`
after `launch.py:608-610, 624-631`:
```python
def reconcile_events(dest):
    """Merge every turn's events by requestId (earliest turn wins the usage; worker role is monotone;
    conflicting usage is excluded and reported) so partially recovered turns cannot double count."""
            first = merged[rid]
            if first['usage'] != event['usage'] or first['model'] != event['model']:
                conflicts.append({'requestId': rid, 'turns': [first['turn'], event['turn']]})
                first['conflict'] = True
            elif event['role'] == 'worker':
                first['role'] = 'worker'
    events = [e for e in merged.values() if not e.get('conflict')]
    return events, conflicts
```

### 3. 리뷰 루프 통계

ledger **81행**. Opus 31(loop1 15 · loop2 9 · loop3 7) · opus-design 26 · codex 22(loop1 11 · loop2 6 · loop3 5) · blind-tests 2.
처리: **fixed 75**(63 + 재점검 closed 8 + 미재검 4) · partial 1(F12) · deferred-doc 2(F5b, F12c) · user-deferred 2(O-F2=D5, O-S3=[구현 검증]) · **기각 1**(O-F8 `docs/experiments` 부재 — packet SCOPE 제외 산물, 실존 확인 / 존재 검사만 채택).
중복 병합 6건: O-F11→C6, O-F12→C4, C9→O-F7, O2-C→X1, O2-E→X4, N-C→L3-1.
루프 종료: loop2가 loop1 fix 39 중 32 closed · loop3 codex가 loop2 fix 15 중 10 closed · **Opus loop3 = 차단 결함 0, loop2 fix 15/15 closed, 회귀 없음**.
**타깃 재점검(codex 인라인)**: loop3 12건 중 **8건 `재점검 closed`**(L3-2·L3-4·L3-5·N-B·N-C·N-E·N-F·N-G), **4건 `재점검: 2차 수정 후 미재검`**(L3-1 래퍼 fail-open · L3-3 scrub 예외 시 finalize 미실행 · N-A 예외 문자열 미마스킹 · N-D `__pycache__` 문자열 과다 면제) — 2차 수정은 단위테스트·정적 확인으로만 종료.
blind 테스트 워커: 50건 → 39 → 44 → 43 통과(잔여는 가정 불일치, **실결함 0**).

### 4. 배운 것 (issues-log.md 근거)

- 같은 저장소를 동시 세션이 재구성한다(719 삭제 미커밋 → `dfbdaaa`). 경로 소유를 명시하고 커밋마다 분리 확인(§1).
- 훅이 `git rev-parse --show-toplevel`에 의존하므로 `.git` 없는 workspace는 `docs/**`까지 L1로 분류해 SPEC=1을 만들 수 없는 교착 → 전 arm `git init`(§3).
- 인증: `--bare`는 구독 OAuth와 양립 불가 → "깨끗한 환경"은 **빈 config volume**(§4). 대화형 `claude setup-token`은 세션 안에서 멈추고, 채팅으로 전달된 토큰은 노출로 간주(§5).
- `codex exec --sandbox`가 호스트와 `cap-drop=ALL` 컨테이너 양쪽에서 bwrap 실패 — 도구 없는 응답만 성공. 리뷰는 파일 전문 인라인(비대칭 입력), arm3은 bypass를 user override로 기록(§6).
- 훅은 명령 **텍스트**만 보므로 문자열 리터럴도 차단된다(self-test 패치가 차단됨) → 파일 경유 실행(§7). *이 요약 작성 중 같은 오탐 재현.*
- deny-list는 표면 확장을 못 따라간다(`Workflow`·`Skill`·`Cron*`·worktree) → allow-list + 관측 표면 ⊆ allow-list 단정(§11). arm3 실측 표면 22개가 근거.
- "복사됨"은 완전성이 아니다 — 리뷰어 사용량은 **정규화 완료**가 기준(`reviewer_usage_pending`), 로그 복사 실패는 0이 아니라 "미확인"(§11·§12).
- 사본은 원본이 아니다: 저장소의 `settings.json`은 배포 대상 아닌 오래된 사본(없는 훅 2개 참조) → 실배포 추출 + provenance(§2). 공유 스크립트도 자동 추종 금지 — `verify_setup`이 어긋남을 알리고 변경을 읽고 옮긴다(§14).

### 5. 남은 빚·이월

- **D5(미결)**: Claude arm의 `WebSearch`/`WebFetch`가 Codex arm에 없다 — 끌지(패리티) / 열고 기록할지 개시 전 결정. 어느 쪽이든 `result.web_requests`·`observed_tools`에 실사용을 남긴다.
- **codex 리뷰어 사용량 정산(미결)**: 래퍼가 세션·`wrapper-calls.log`를 보존하나 codex adapter 정규화는 분석 단계 → arm3 턴은 `usage_complete=false`. 정산 규칙 사용자 확인 필요.
- **토큰 재발급(필수)**: 스모크 토큰은 노출로 간주 — 본 실험 전 재발급(`docs/auth-token.md`).
- **[구현 검증] 이연 2건**: ①`result.usage`의 sidechain 포함 여부(스모크에 서브에이전트 없음) ②`parent_tool_use_id`/sidechain 역할 신호(O-S3) — arm3 첫 run에서 확정, runbook 등재.
- **미검증 5항목**(`summary.json.not_verified`): 서브에이전트 모델·effort 전파, sidechain 합산, InstructionsLoaded/ConfigChange/SubagentStop 발화, compaction 회계, headless git-guard ask.
- **loop 3 수정은 4번째 전체 루프 없음** — 상한(3) 도달로 **타깃 재점검만**. 그 재점검이 4건을 다시 열었고 2차 수정은 재점검 없이 종결 → 잔여 리스크 = L3-1·L3-3·N-A·N-D.
- **증거 세대 불일치**: 유료 턴 증거는 강화 전 수집기 산출물(`status`/`codex_sessions`/`web_requests` 없음, `hooks_checked=1`). 관측은 유효하나 재실행은 없었다.
- **미커밋**: `claude/` 전체 untracked. 트리에 `M README.md`·`M payment-settlement/docs/experiment-plan.md`(공유·금지영역)가 남아 **커밋 전 소유 세션 확인 필요**(동시 Codex 세션 변경 가능성). F12·F5b·F12c는 문서 이연으로 종결.

---
task ID: `claude-arm-runner/task-06-completion-summary`
읽은 파일: `docs/plans/2026-09-23/claude-arm-runner/{requirement-spec.md,log.md}` · `payment-settlement/claude/{README.md, runner/launch.py, runner/codex-wrapper.sh, docs/results/setup/claude/summary.json, docs/issues-log.md}`
실행 명령: `git status --short | grep -v codex` · `ls payment-settlement/claude` · `python3 -m unittest discover -s payment-settlement/claude/runner`(19 OK) · 개수·ledger 집계(awk)
작성 시각(UTC): 2026-09-23T14:20:57Z

> 요약 작성: Opus 워커(task-06), 스니펫은 실파일에서 복사. 메인 검수 2026-09-24 02:2x.
