# 요구사항 명세서 — Claude 조건 실험 폴더·실행기 준비

> 작성일: 2026-09-23 · 작업 폴더: `docs/plans/2026-09-23/claude-arm-runner/`
> 대상 프로젝트: `need-haness-lab` (branch `setup/payment-experiment`)

---

## 0. 요구사항 원문 (인터뷰 기록)

- 원문: "현재 해당 실험을 codex로 실험중인데 별도 claude폴더를 만들어서 클로드로도 해당 실험 진행하자. 총 독립적으로 10회씩 진행해서 완성도를 확인하는건데 우선 해당 실험 확인부터 해볼래?"
- Q/A (2026-09-23):
  1. 폴더 위치 → "해당 폴더 내 claude폴더 만들고 그 안에서 문서화 클로드용으로 처리하고 똑같이 문서화할꺼야. 이후 코덱스도 완료후 코덱스 폴더로 이전시킬꺼고, 지금 코덱스로 조금씩 하고 있고"
  2. 반복 수 → "조건당 10번씩 독립실행" (총 30)
  3. 모델 → "Opus로 하고 PM은 fable로 처리" (구현·워커 = Opus, 3번 PM = Fable). **effort는 미지정 → 아래 가정 A1**
  4. 인증 → "구독으로 지금쓰는 claude code로만 처리" (API 키 없음 → `billed_cost=null`, `total_cost_usd`는 추정치)
  5. 리뷰 → "원본 그대로 허용" (Opus 워커 ∥ codex 듀얼 리뷰, codex 호출 허용)
  6. 격리 → "똑같이 처리해서 최대한 캐시를 사용하지않도록" (Codex 실행기와 동일한 컨테이너·세션 격리, observed-cache)
  7. 순서 → "판정기를 지금 코덱스로 만들고 있는데 똑같이 claude도 해당 판독기를 활용" + "실행기는 클로드용으로도 만들어놔야될 꺼같고"

### 조사로 확정한 사실 (2026-09-23)

- 이 저장소의 Codex 30 run은 전부 `NOT_STARTED`. 실행기 스모크 1턴은 401로 실패(auth 마운트). preflight 차단 9항목 미해결. 판정기는 k6 skeleton(C01/02/04/05/06/10/12 일부)만 존재.
- Claude Code 2.1.280 (`~/.local/bin/claude`). `-p --output-format stream-json`, `--session-id/--resume`, `--model`, `--effort`, `--max-budget-usd` 제공. 세션 transcript(`projects/<cwd>/<session>.jsonl`)의 assistant 메시지에 `requestId`·`model`·`usage{input, cache_creation_input_tokens, cache_read_input_tokens, output}`·`isSidechain` 존재(로컬 실측).
- `CLAUDE_CONFIG_DIR`는 settings·`.credentials.json`·projects·memory를 통째로 옮긴다. 빈 디렉터리 + `--bare`는 인증 실패(실측, 비용 0). OAuth 자격증명은 복사본으로 headless 사용 가능하나 토큰 갱신에 **쓰기 권한** 필요(문서). `--bare`는 OAuth와 양립 불가(문서) → 1·2번의 깨끗한 환경은 "빈 config dir + 자격증명만"으로 구성.
- 서브에이전트 비용은 부모 `total_cost_usd`에 포함(문서). 역할별 분리는 transcript `parent_tool_use_id`/sidechain 파일에서 추출해야 함(문서 미기재 → [구현 검증]).
- 하네스 스냅샷 `harness-source/settings.json`은 존재하지 않는 훅 2개(scope-guard, template-guard)를 참조 — 실배포 `~/.claude/settings.json`과 다름. 훅 스크립트 본체는 스냅샷 = 실배포(cmp 동일). 원본 repo HEAD는 스냅샷 SHA `1754f16`과 일치.
- `playbooks/review.md §5`의 codex 리뷰 호출은 `--ephemeral -o` → 세션 미보존 → 리뷰어 토큰 관측 불가. 3번 `usage_complete`에 영향.

---

## 1. 목표·대상 (필수)

> **경로 보정(2026-09-23 14:5x)**: 동시 진행 중인 Codex 세션이 저장소를 `payment-settlement/codex/`(arm·template·compose·prompts·runner·scripts·harness-source·docs/{runbook,isolation,experiments,results}) + 루트 공유(`docs/` 계약·`evaluation/`·`infra/`·`measurement/`·`recording/`)로 재구성 중(미커밋). Claude 조건은 **`payment-settlement/claude/`를 `codex/`와 동일 배치**로 만든다. 아래 경로 표기는 이 배치를 따른다.

`payment-settlement/claude/` 아래에 Codex 실험과 **동일 프로토콜·동일 판정기·동일 계약 입력**을 쓰는 Claude 조건 실험 환경(폴더 구조·30개 starter·Claude용 프롬프트·격리 실행기·기록·문서)을 만들고, **유료 스모크로 실행기 전 경로(컨테이너 인증·stream-json 사용량·resume·arm 3 훅 강제)를 1회 실증**하면 끝. 30회 본 실험 실행과 판정기(C13~C20) 완성은 **이 작업 범위 밖**(판정기는 Codex 쪽에서 제작 중, 공유).

## 2. 경계·불변식 (필수)

- **입력 동일성**: 1·2번 최초 입력은 Codex와 바이트 동일(`prompts/common.md`·`1-minimal.md`·`docs/requirements.md`·`resources.md`·`slo.md` 공유, SHA 기록). 3번 프롬프트만 Claude용으로 별도 작성(`claude/prompts/3-harness.md` — "Sol/high·Codex 이식" 문구 대신 "네이티브 훅·Fable PM·Opus 워커" 규칙).
- **starter 동일성**: `claude/{1,2,3}/core1..10`은 `template/`과 바이트 동일(`lab.py verify` 수준 검사 통과).
- **격리**: run마다 새 컨테이너·새 workspace 복사본·새 `CLAUDE_CONFIG_DIR` volume(자격증명 복사본만)·새 session ID·새 DB/은행 volume. 호스트 HOME·다른 run·정답·평가 로그·Docker socket 미마운트. 1·2번 컨테이너에는 훅·CLAUDE.md·memory·플러그인 없음. 3번 컨테이너에만 하네스(core.md·CLAUDE.md 부트스트랩·hooks·playbooks·templates·**실배포 기준 settings.json 훅 목록**) 배포 + codex CLI·auth.
- **모델 통제**: 1·2번 구현자 = Opus, 3번 PM = Fable, 3번 워커(구현·테스트·리뷰) = Opus, 3번 리뷰 2호 = codex(원본 그대로). 실제 모델은 transcript `model`·`modelUsage`로 사후 확인해 manifest `actual_model`에 기록. 요청 별칭을 실제 ID로 오인하지 않음.
- **캐시**: observed-cache. run 사이 세션 초기화, 같은 run 안 수정 턴은 `--resume`로 대화 유지. cache_read/creation을 요청 단위로 기록. 완전 차단 주장 금지.
- **집계**: 요청 단위 `requestId`를 event_id로 사용, 부모·자식(sidechain) 중복 합산 금지. 미관측(codex `--ephemeral` 리뷰 등)은 `usage_complete=false`로 표시하고 0으로 채우지 않음. 구독이므로 `billed_cost=null`, `estimated_cost=total_cost_usd`.
- **자격증명**: Claude 토큰은 사용자가 지정한 env 파일(repo 밖)에서 `CLAUDE_CODE_OAUTH_TOKEN`으로만 주입, codex `auth.json`은 읽기 전용 bind. repo·`.runs/`·로그·이미지·invocation 기록에 토큰 값 기록 금지(`.runs/`는 gitignore).
- **Codex 실험 무간섭**: `codex/**`와 루트 공유 모듈의 바이트를 바꾸지 않음. `claude/template`·`claude/prompts/{common,1-minimal,feedback,slo}.md`·`claude/harness-source`는 `codex/`의 바이트 동일 복사본(SHA 기록). 공유 모듈(measurement/recording)은 **읽기만** — Claude 실행기는 기존 `anthropic` adapter 입력 형식(최종 message + usage)으로 이벤트를 조립해 넘긴다.

## 3. 기준소스 (필수)

- 프로토콜·계약·판정: `payment-settlement/docs/*.md`, `prompts/*.md`, `evaluation/`, `infra/`, `template/`(Codex와 공유, protocol `payment-settlement-v1`).
- 하네스: `payment-settlement/harness-source` (commit `1754f16`) + 훅 배선은 실배포 `~/.claude/settings.json`의 hooks 섹션(orca 훅·model 핀 제외).
- Claude CLI 동작: `claude --help`(2.1.280) + 공식 문서(headless/sessions/authentication/costs). 문서 미기재 항목은 스모크 실측이 정본.
- 격리·기록 설계 준거: 기존 `runner/launch.py`·`runner/README.md`·`docs/isolation-and-measurement.md`.

## 4. 금지영역 (필수)

- `payment-settlement/codex/**`(arm·template·compose·prompts·runner·scripts·harness-source·docs) 및 루트 공유 `docs/`·`evaluation/`·`infra/`·`measurement/`·`recording/`·`README.md` — **수정 금지**(읽기·복사 원본으로만 사용). 루트 README의 Claude 안내 1단락 추가는 마감 시 사용자 확인 후.
- 30회 본 실험 실행 금지(스모크 1~2턴만). 판정기 작성 금지(Codex 쪽 소유).
- 사용자 실계정 자격증명을 repo·로그·이미지·transcript 사본에 남기지 않음. 원본 `~/.claude/.credentials.json` 수정 금지.
- git push·PR·원격 브랜치 생성 금지. 커밋은 setup 커밋으로만, docs와 실행물 분리.
- `~/.claude/` 실배포(하네스) 변경 금지.

## 5. 검증 방법 (필수)

1. **구조**: `claude/` verify 스크립트로 30 starter = template 바이트 동일 PASS; 1·2번 입력 SHA = Codex `lab.py prompt --arm 1|2` 출력 SHA.
2. **실행기 단위테스트**(모델 미호출): run 상태 분리·덮어쓰기 거부·resume 순서·auth/volume 마운트 규칙·Docker socket 부재·dry-run 무실행·원시 stream-json 보존·requestId 기반 event 조립·anthropic adapter 통과(`measurement/normalize.py --provider anthropic`로 정규화 성공).
3. **유료 스모크(사용자 승인 후, 구독 사용)**: ①1번형 컨테이너에서 `RUNNER_OK` 1턴 → `result.usage` 캐시 필드·`session_id`·`actual_model` 확인 ②같은 run `--resume` 1턴 → 대화 유지 확인 ③3번형 컨테이너에서 "spec 없이 파일 쓰기" 부정 테스트 → gate-guard 차단 확인(훅이 컨테이너에서 실제 강제됨을 실증). 각 턴 원시 로그 보존, 토큰·비용은 setup 비용으로 기록.
4. **머지 전 안전선**: diff self-review(금지영역 바이트 변화 0 — `git status`·`lab.py manifest` 해시 전후 동일), 자격증명 grep 0건(`.runs`·docs·코드), rollback = `claude/` 폴더 삭제로 복구 가능.
5. 완료 문서: `claude/README.md`·`claude/docs/`(runbook·격리·측정·개시 체크리스트)·`claude/docs/results/schedule.json`·30개 `experiment/run.json`(NOT_STARTED) + preflight가 미확인 항목을 나열(가짜 true 금지).

## 6. stakes (필수)

- 판정: **높음** — 근거: 실계정 OAuth 자격증명을 컨테이너에 복사하는 보안·권한 요소 + Docker 안 OAuth headless·훅 강제가 문서에 없는 낯선 영역(문서 갭 다수). 손실은 복구 가능(폴더 삭제)이지만 표의 "보안·권한·낯섦" 항목에 해당.
- 적용 강도: 설계 선검증 + 듀얼 리뷰 루프(≤3) + blind 테스트 워커(실행기 코드 대상). 외부 검색 의무는 위 문서 조사로 충족.

---

## 7. 자율성 (모드)

- [x] auto (합의 후 자율 실행 — 단 유료 스모크 착수·토큰 주입 경로는 개별 확인)
- [ ] lazy

## 8. load-bearing 가정 (스모크로 조기 실증)

- **A1**: `CLAUDE_CODE_OAUTH_TOKEN`(setup-token)만 주입한 run별 `CLAUDE_CONFIG_DIR` volume으로 컨테이너 안 `claude -p`가 구독 인증에 성공하고, `stream-json` result에 `input/cache_creation/cache_read/output`·`total_cost_usd`·`session_id`가 오며 `--resume`이 같은 대화를 잇는다. (effort `high` — D1 합의.)
- **A2**: 3번 컨테이너에 배포한 훅(gate-guard 등)이 headless `-p` 모드에서도 실행되어 SPEC=0 상태의 L1 쓰기를 실제로 차단한다. (실패 시 3번은 "절차 준수" 조건으로 강등하고 문서에 기록 — Codex 이식과 같은 위치.)

## 9. task 분해

| task | 목표 | 의존 | acceptance |
|------|------|------|-----------|
| 01 | `claude/` 뼈대(`codex/` 배치 미러): README·docs/{runbook,isolation}·docs/experiments/<arm>/coreN 기록 초기화·docs/results(schedule·manifest 예시)·prompts(공통 복사 + Claude용 3-harness)·template 복사·30 starter·compose·scripts(lab/prepare/preflight/verify) | — | 검증 1·5 |
| 02 | 실행기 `claude/runner/`: Dockerfile(node22+temurin21+claude-code 2.1.280+codex 0.155.1) · `launch.py init/turn/collect` · 자격증명 volume 시딩 · arm 3 하네스 배포 · 단위테스트 | 01 | 검증 2 |
| 03 | 사용량 조립기: transcript+stream-json → anthropic adapter 입력(requestId 단위, 역할 태깅, 미관측 표시) | 02 | 검증 2(정규화 통과) |
| 04 | 유료 스모크 3턴(사용자 승인) + 결과를 `claude/docs/results/setup/`에 기록, preflight 항목 갱신 | 02·03 | 검증 3 |
| 05 | 설계 선검증·듀얼 리뷰·blind 테스트·log 마감·measurement-log 1행 | 04 | 리뷰 종료식 |

### 사용자 결정 (2026-09-23 합의 — 원문: "1번 컨테이너 2번 고정하자. 2번은 opus high로 고정하는게 맞을 꺼 같아. 3번 3번 허용할게 4번 auto로 진행하자")

- **D0** 실행 위치: **컨테이너 안 `claude` CLI**. 인증은 `claude setup-token`으로 만든 구독용 장기 토큰을 `CLAUDE_CODE_OAUTH_TOKEN` 환경변수로 주입(자격증명 파일 복사 없음 → 구 D3 폐기). API 키 사용 안 함.
- **D1** effort: 전 역할 **`high` 고정** (1·2번 구현자 Opus high, 3번 PM Fable high, 3번 워커 Opus high).
- **D2** codex 리뷰 토큰: 컨테이너 PATH의 `codex` 래퍼가 `--ephemeral`만 제거(세션 파일 보존, 출력 동일)하는 방식 **허용**. 래퍼 존재와 원본 명령 차이를 run 문서에 기록.
- **모드**: auto.

---

## 승인 상태

- [x] 필수 6칸 전부 기입
- [x] 사용자 합의 → SPEC=1 (2026-09-23)
- [x] 자율성 선택 → MODE=auto

---

## 10. 실행기 설계 (높음 stakes — 설계 선검증 대상)

1. **이미지** `payment-claude-runner:2.1.280`: `node:22-bookworm-slim` + Temurin 21 JDK(codex 이미지와 동일 구성) + `git python3 curl ca-certificates bash unzip jq util-linux(flock) perl procps`; `npm i -g @anthropic-ai/claude-code@2.1.280 @openai/codex@0.155.1`; `/usr/local/bin/codex`는 래퍼(인자에서 `--ephemeral`만 제거 후 `codex-real` exec — D2) ; `USER node`(uid 1000, codex auth 0600 호환) ; `WORKDIR /workspace`.
2. **init**(run_id, arm, core, [--network]): starter → `.runs/<run>/workspace` 복사(SKIP 집합 동일, symlink 거부) ; `manifest.json` = {provider claude-code, requested_model(1·2 = opus, 3 = fable PM) , requested_worker_model(3 = opus), effort high, **session_id = uuid4 고정**, config_volume `payment-claude-<hex>`(→ `/home/node/.claude`), codex_volume(3만), image, harness(3만), network}. **arm 3 시딩**: 네트워크·인증 없는 일회성 컨테이너가 `/harness`(ro)에서 `src/core.md→core.md`·`playbooks/`·`templates/`·`hooks/` + `runner/harness-bootstrap-CLAUDE.md→CLAUDE.md` + `runner/harness-settings.json→settings.json`을 config volume에 복사. 1·2번 volume은 비워 둔다(훅·CLAUDE.md·memory 없음). **모든 arm의 workspace에 `git init` + 초기 커밋**(gate-guard의 docs/** L0 판정과 git-guard가 git toplevel을 요구 — 없으면 arm 3 PM이 spec을 쓸 수 없는 교착. Codex 실행기와의 차이로 문서에 기록).
3. **turn**(run_id, prompt, --env-file, [--resume], [--codex-auth](3), --cache-policy observed-cache, --timeout-seconds): `docker create --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=512 --cpus=2 --memory=3g --env-file <path>` (파일에는 `CLAUDE_CODE_OAUTH_TOKEN`만; 경로만 기록, 내용 미기록·0600 요구) + `--env CLAUDE_CODE_SUBAGENT_MODEL=<worker>`(3만) ; 마운트: workspace rw, config volume rw, (3) codex volume rw + auth.json ro ; CLI: `claude -p --verbose --output-format stream-json --model <m> --effort high --permission-mode bypassPermissions --dangerously-skip-permissions` + 첫 턴 `--session-id <고정>` / 이후 `--resume <고정>` ; prompt는 stdin. `--bare`·`--no-session-persistence` 미사용. Docker socket·호스트 HOME·다른 run 미마운트.
4. **collect**: `stdout.jsonl` 원문 보존 ; `result` 이벤트(subtype·session_id·total_cost_usd·usage·modelUsage·num_turns·duration_ms) → `result.json` ; `docker cp /home/node/.claude/projects` → `turns/NNNN/projects/`(누적 transcript, sidechain 포함) ; `events.jsonl` = `type=assistant`이고 `requestId`·`message.usage`가 있는 줄을 **requestId 단위로 1개**(이전 턴 `seen_request_ids` 제외) anthropic adapter 형식 `{type:"message", id:requestId, model, usage{input, cache_creation_input_tokens, cache_read_input_tokens, output}}` + 부가 필드(role: pm|implementer|worker — isSidechain/agentId 기준, file, turn)로 조립 ; usage·캐시 키 결측 줄이 있으면 `usage_complete=false`. (3) `/home/node/.codex/sessions` → `turns/NNNN/codex-sessions/` 원문 보존(codex adapter는 분석 단계). 교차검증: 신규 요청 usage 합 vs `result.usage` — 불일치는 result에 표시([구현 검증] — 스모크로 의미 확정).
5. **불변식**: run 덮어쓰기 거부 · 첫 턴 `--resume` 금지/이후 필수 · session_id 턴 간 불변 · Docker socket 절대 미마운트 · env-file 존재+0600+이름만 기록 · timeout은 호출 컨테이너만 중지 · volume·workspace 보존 · 토큰 값이 invocation/result/stderr 사본에 나타나면 실패(수집 후 grep 자체 검사).

### §10 개정 (2026-09-23 설계 선검증 반영 — 리뷰 ledger 참조)

- **HOME·CLAUDE_CONFIG_DIR 명시**(Dockerfile ENV + `docker create --env`) — 훅 배선 `~/.claude/hooks/*`와 transcript 경로가 암시값에 기대지 않는다.
- **무과금 self-test를 불변식으로 승격**: init(모든 arm)과 매 turn 직전(arm 3)에 네트워크·토큰 없는 helper 컨테이너가 ①CLI 인자 수용 ②workspace git toplevel 판독 ③settings.json이 참조하는 훅 전부 존재·`bash -n` ④gate-guard에 합성 PreToolUse를 파이프해 SPEC=0 L1 쓰기 exit 2 / docs 쓰기 exit 0 ⑤`codex` PATH 존재를 단정. 실패 시 init 거부(staging 원자 생성) / turn 거부. 결과는 manifest·invocation에 기록 → A2는 유료 스모크가 아니라 상시 단정.
- **arm 1·2 서브에이전트 금지**(`--disallowedTools Agent,Task,Workflow` — loop 1에서 `Workflow` 추가, self-test가 init 이벤트의 tools 표면에 셋 다 없음을 단정하고 manifest `observed_tools`에 기록) — Codex 실행기가 arm 1·2에서 multi_agent를 끄는 것과 패리티. arm 3만 `CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-5`. manifest `subagents` 필드에 기록.
- **역할별 adapter 파일**: collect가 `events.role-{pm|implementer|worker}.jsonl`을 분리 출력하고 `normalize` 서브커맨드가 파일마다 `--role`·`--session-id <session>/<role>`로 공유 adapter를 호출 → 역할 태깅이 adapter 계약에 실린다.
- **사본 일치 단정**: 같은 requestId의 모든 transcript 사본을 모아 usage 동일성·모델 단일성을 검사, 불일치는 `incomplete`. 역할은 어느 사본이든 sidechain이면 worker. `request-ids.txt` append-only 원장으로 턴 간 dedupe.
- **교차검증**: `result.usage` vs main 합 / 전체 합의 delta를 기록한다. (loop 1 개정) 스모크 4턴에서 delta 0이 실증된 뒤 `usage_complete`의 조건으로 승격 — main 또는 전체 중 하나가 0이면 통과(sidechain 포함 여부는 arm 3 파일럿에서 확정).
- **토큰 스캔은 `finally`에서 run 디렉터리 전체(workspace 포함)**, 히트 시 `SECRET-LEAK.txt` + 실패 종료. inspect Env 값은 마스킹. config volume의 파일 목록(내용 아님)과 `.credentials.json` 존재 여부를 매 턴 기록.
- **env-file은 repo 밖·0600·단일 키**를 dry-run에서도 검증. 호스트 uid=1000 단정(컨테이너 node와 동일 — workspace git 소유권).
- `--max-turns`·`--max-budget-usd` 옵션 제공(스모크에서 사용, 본 실험 값은 사용자 확인 후 고정).
- 컨테이너 settings는 실배포 hooks 섹션 추출본 — `runner/harness-settings.provenance.json`에 원본 SHA·제거 항목 기록. **미결 D4**: 실배포 모델 핀은 `claude-fable-5-1[1m]`(1M 컨텍스트)이나 실험 manifest는 `context_variant=default`. 어느 쪽을 쓸지 사용자 확인 필요.
- arm 3 프롬프트에 컨테이너 환경 사실(codex PATH, read-only 샌드박스 실패 시 bypass를 user override로 기록) 추가 — Claude 전용 프롬프트이므로 입력 동일성 불변식과 충돌하지 않음.

### 스모크 결과 (2026-09-23 22:0x — 유료 4턴, setup 비용)

- **A1 성립**: env-file 토큰만으로 컨테이너 `claude -p` 인증 성공. `result.usage`에 4개 카운터 + `total_cost_usd` + `modelUsage(canonicalModel, contextWindow, costUSD)` 제공. `--resume`은 같은 session id·같은 transcript 파일을 잇는다(fork 없음). volume에 `.credentials.json` 없음(`.claude.json`·policy-limits·remote-settings만).
- **A2 성립**: headless `-p`에서 SessionStart·PreToolUse 훅이 실제 실행되어 gate-guard가 SPEC=0 상태의 Write를 차단했다(stream에 hook_started/hook_response 이벤트, 도구 결과에 차단 메시지, 파일 미생성).
- **D4 관측**: modelUsage가 두 모델 모두 `contextWindow: 1000000`을 보고 — 실배포의 `[1m]` 접미 없이도 1M 컨텍스트가 적용된다. `context_variant=default`로 유지하고 이 관측을 문서화(추가 결정 불필요).
- codex 리뷰 경로: 컨테이너 안 `codex exec --sandbox read-only`가 read-only auth로 성공(F5c 해소). 래퍼로 세션·usage 보존 확인.
- 교차검증: 4턴 모두 `result.usage` = transcript 요청 합(delta 0). sidechain 포함 여부는 서브에이전트가 없는 스모크라 미확정 — 본 실험 arm 3 첫 run에서 확정([구현 검증] 이연, runbook 체크리스트 등재).
