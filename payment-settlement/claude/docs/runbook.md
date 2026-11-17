# Claude 판 구동과 시행 절차

명령은 저장소 루트 기준이다. 공유 fixture(`../../infra`, `../../evaluation`)와 계약(`../../docs`)은 Codex 판과 같다.

## 준비

```bash
python3 payment-settlement/claude/scripts/prepare.py
python3 payment-settlement/claude/scripts/verify_setup.py
docker build -t payment-claude-runner:2.1.326 payment-settlement/claude/runner
```

인증: 구독 계정에서 한 번 `claude setup-token`을 실행해 장기 토큰을 만들고, **repo 밖** 파일(예: `~/.config/payment-lab/claude.env`)에 `CLAUDE_CODE_OAUTH_TOKEN=<토큰>` 한 줄만 넣어 `chmod 600` 한다. 실행기는 이 파일을 `docker --env-file`로만 넘기고 경로만 기록한다(repo 안 경로·다른 키·느슨한 권한은 거부). 3번의 두 번째 리뷰어(`fable-review`)도 같은 토큰을 컨테이너 환경에서 물려받는다. 추가 자격증명 파일은 없다.

## 시행 (한 번에 하나, schedule.json 순서)

```bash
L=payment-settlement/claude/runner/launch.py
# 1·2번
python3 $L init --run-id claude-arm1-core01 --arm 1 --core 1
# 3번: 하네스 시딩 + 훅 self-test
python3 $L init --run-id claude-arm3-core01 --arm 3 --core 1 --harness payment-settlement/claude/harness-source
# 통합 테스트가 필요한 시행은 자기 Compose project의 네트워크를 준다: --network payment-c1a1_default

python3 payment-settlement/claude/scripts/lab.py prompt --arm 1 > /tmp/first-prompt.txt
python3 $L turn --run-id claude-arm1-core01 --prompt /tmp/first-prompt.txt --env-file ~/.config/payment-lab/claude.env --dry-run
python3 $L turn --run-id claude-arm1-core01 --prompt /tmp/first-prompt.txt --env-file ~/.config/payment-lab/claude.env --cache-policy observed-cache
# 평가자가 판정·피드백(prompts/feedback.md 형식)을 쓴 뒤 같은 세션으로 재개 — 재개 턴은 반드시 --kind를 선언한다
python3 $L turn --run-id claude-arm1-core01 --resume --kind feedback --prompt /tmp/next-prompt.txt --env-file ~/.config/payment-lab/claude.env --cache-policy observed-cache
# 3번만: 대리 사용자 답(scripts/consult_contract.py → consults/NNNN/response.txt)을 PM에게 전하는 턴
python3 $L turn --run-id claude-arm3-core01 --resume --kind answer --prompt /tmp/answer-prompt.txt --env-file ~/.config/payment-lab/claude.env --cache-policy observed-cache
python3 $L normalize --run-id claude-arm1-core01     # 역할별 파일 → 공유 adapter → usage-summary.json
```

이미지: 동결 전(인증 스모크)에는 `--image`(기본 `payment-claude-runner:2.1.326` 태그)를 쓴다. `readiness.json`이 생긴 뒤의 `init`(파일럿·사이클)은 그 동결 digest만 쓴다 — `--image`를 생략하면 digest가 들어가고, 다른 값은 거부된다(바꾸려면 새 프로토콜 버전).

`turn`은 Docker 컨테이너를 만들고 `claude -p --output-format stream-json`을 고정 session id로 실행한다(첫 턴 `--session-id`, 이후 `--resume`). `.runs/<run>/turns/NNNN/`에 prompt 원문·invocation·원시 stdout/stderr·마스킹된 inspect·transcript 사본(`projects/`)·`events.jsonl`·`events.role-*.jsonl`·`result.json`을 보존한다. 종료 후 run 디렉터리 전체를 토큰 값으로 스캔해 히트가 있으면 `SECRET-LEAK.txt`를 남기고 실패로 끝낸다(토큰 회전 필요).

시행별 문서화는 Codex 판과 같다: `python3 payment-settlement/recording/record.py --run-dir payment-settlement/claude/docs/experiments/<arm>/core<N> ...`로 프롬프트 전문·응답·분석·증거를 원장에 추가한다. transcript를 직접 편집하지 않는다. `result.json`의 `usage_complete`, `incomplete`, `usage_cross_check`, `config_volume.credentials_file_in_volume`, `hook_selftest_before_call`을 분석에 인용한다.

## 상한 (run당, 모든 역할 합산)

`launch.py turn`과 `scripts/consult_contract.py`는 유료 호출 직전에 같은 게이트(`limit_gate`)를 지난다. 집계 원천은 run의 단일 장부 `.runs/<run>/ledger.json`(turns의 pm·implementer·worker·reviewer + consults의 user-proxy, 요청 단위 중복 제거)이다.

- 토큰: input + cache read + cache creation + output 합 ≥ 20,000,000 → `CENSORED_BUDGET`
- 시간: 첫 턴 시작부터 벽시계 ≥ 28,800초 → `CENSORED_TIME`(턴 하나와 대리 사용자 호출 하나의 timeout도 남은 시간으로 줄인다. 3번은 훅 self-test 뒤 게이트를 다시 지나 남은 시간을 다시 계산한다 — `invocation.json`의 `effective_timeout_seconds`)
- 피드백: 외부 피드백 = **평가자가 판정 결과를 전하는 턴만**(2026-11-17 결정, `docs/experiment-plan.md:51`). 성공(`status: ok`)한 `feedback` 턴(그 복구 포함) 수가 30이면 31번째 `feedback` 전달은 `CENSORED_TURNS`
- 질의응답(3번만): 대리 사용자 답을 PM에게 전하는 `answer` 턴은 상한에 넣지 않고 따로 센다(`experiment-plan.md:25` "질문 횟수·답변 길이를 별도 기록"). 대리 사용자 호출(`consult_contract.py`) 자체는 세션 턴이 아니다

턴 종류는 매 턴 `turns/NNNN/invocation.json`의 `turn_kind`(+`counts_as`)에 남고, 셈은 이 파일들과 `result.json`의 `status`에서 다시 계산된다:

| `turn_kind` | 언제 | 상한 |
|---|---|---|
| `initial` | 첫 턴(`--resume` 없음, `--kind`를 주면 거부) | — |
| `feedback` | 평가자 판정 결과 전달(`prompts/feedback.md`) | 30회 |
| `answer` | 3번: 대리 사용자 답 전달. 1·2번은 거부. 아직 전달하지 않은 **완료된** `consults/NNNN`이 있어야 한다(완료 consult 수 > `answer` 턴 수). 완료 = 대리 사용자 호출이 exit 0·기록된 오류 없음·시크릿 누출 없음·응답 비어 있지 않음(`launch.consult_completed`, `usage.json`의 `completed`). 실패·빈 응답 consult는 answer 턴을 허용하지 않고 장부의 `consults_not_completed`에 남는다. `usage.json` 없는 consult 디렉터리는 장부의 `incomplete_calls`에 들어간다 | 별도 기록 |
| `recovery` | 바로 앞 턴이 `ok`로 끝나지 않았을 때 그 턴을 이어 감. 앞 턴이 `ok`면 거부. **앞 턴이 `ok`가 아니면 재개 턴은 `recovery`만 허용**(`feedback`·`answer` 거부 — 실패한 턴도 모델에 닿았을 수 있으므로 그 턴의 종류로 센다) | 이어 가는 턴의 종류로 센다 |

`--kind` 없는 재개 턴은 유료 호출 전에 거부된다. `turn_kind`가 없는(또는 `invocation.json`이 없는) 턴 디렉터리가 있으면 상한 계산이 실패하고(건너뛰지 않음) 그 run의 다음 호출은 막힌다. `limit_gate` 결과(`invocation.json`의 `limits_before_call`)와 `ledger.json`에 `turn_kinds`(`delivered`: 성공 전달 수 `initial`·`feedback`·`answer` / `declared`: 시도별 선언 종류 수)와 `answer_turns`(limit 상태)가 남는다. 실패한 `feedback` 턴은 세지 않고, 그 `recovery`가 성공하면 1회로 센다(종전 "성공 턴만 센다" 규칙 유지).

상한에 닿으면 `censored.json`(상태·그때의 사용량)을 남기고 호출을 거부하며, 이후 모든 호출도 거부한다. 호출 도중 넘으면 호출 뒤 `censored.json`에 초과분(`overshoot`)을 기록한다. 산출물은 지우지 않는다. 상한은 **다음 유료 호출을 막는 규칙**이다(2026-11-17 결정): `censored.json`은 지우거나 고치지 않으며, 그 run에는 완료 여부와 관계없이 더 이상 호출이 나가지 않는다. 종료 상태는 진행자가 손으로 옮기지 않고 `scripts/finish_run.py --run-id <id> --verdict PASSED|FAILED|INVALID_INFRA`(평가자 판정)로 `run.json`에 쓴다:

| 평가자 판정 | `censored.json` | `run.json` `status` | 함께 남는 것 |
|---|---|---|---|
| `PASSED` | 없음 | `PASSED` | `cap_reached=false` |
| `PASSED` | `CENSORED_BUDGET`/`CENSORED_TIME`, `first.when="after call"`(마지막 호출 도중 초과) | `PASSED` | `cap_reached=true`, `cap_overshoot`(어느 상한·초과량·그때 사용량) — 결과 표에서 "완료 + 초과"로 구분 |
| `PASSED` | 그 밖(`before call` 차단, `CENSORED_TURNS`) | 거부(통과 판정 이후에 호출을 시도했다는 뜻이므로 기록 확인) | — |
| `FAILED` | 있음 | `censored.json`의 `CENSORED_*` | `cap_reached=true` |
| `FAILED`/`INVALID_INFRA` | 없음 | 판정 그대로 | — | 장부가 미완전(`usage_complete=false`)이면 토큰 합은 하한값이다.

## 실패한 턴의 복구

`turn`이 유료 호출 이후 예외로 끝나면 `turns/NNNN/`에 원문과 `result.json{status:"failed"}`가 남는다(`cli_status`는 CLI exit code, `status`는 수집·토큰 스캔까지 끝난 뒤의 최종 판정). 다음 `turn`은 새 번호를 받는다. 실패한 턴을 이어 가는 재개 턴은 `--kind recovery`로 선언한다(위 상한 절의 표 — 실패한 `feedback`의 복구는 성공 시 피드백 1회). **첫 턴/재개 판정은 config volume에 그 session id의 transcript가 존재하는지로 한다**: 실패한 호출이라도 transcript를 남겼으면 다음 턴은 `--resume`이어야 하고, 남기지 않았으면 `--session-id` 첫 턴이다(실행기가 실측해 반대 지정을 거부한다). 실패 턴에서 완전히 수집된 요청은 `request-ids.txt`에 들어가고, 원장에 들어가기 전에 중단된 요청은 다음 턴에서 다시 집계된다. `normalize`는 모든 턴의 이벤트를 requestId로 병합해(첫 등장 usage, worker 역할은 단조) 중복을 막는다. 디렉터리를 손으로 지우지 않는다.

## 종료와 커밋

run 종료 순서: ① 마지막 턴 종료(또는 `censored.json`) → ①′ 평가 비용 기록(아래 절) → ①″ 평가자 판정 뒤 `finish_run.py`로 종료 상태 기록(위 상한 절의 표) → ② `launch.py normalize --run-id <id>` → ③ `scripts/capture_turn.py`로 남은 턴 기록(`ledger.json`이 기록에 복사된다) → ④ `launch.py discard --run-id <id>` → ⑤ `commit_run.py`. ④는 3번 리뷰어 volume의 `raw/<session>.json`이 전부 run 디렉터리(`turns/*/reviewer-raw/`)로 회수됐는지 먼저 확인하고, 아니면 아무 volume도 지우지 않고 실패한다. 제거 실패도 non-zero로 끝나며 `discard-<시각>.json`의 `complete`가 false로 남는다. 다음 사이클의 `start_cycle.py`는 앞 사이클 세 run의 terminal 상태·`experiment: <run_id>` 커밋·미커밋 변경 없음·`complete=true` discard 기록을 증거로 확인한다(`cycles.json`의 상태 문자열은 게이트가 아니다).

`scripts/commit_run.py --arm N --core M`(Codex 판의 바이트 복사본 — Codex 쪽이 바뀌면 `verify_setup.py` 패리티가 깨지므로 그때 다시 복사한다)이 상태·문서(`summary.md`·`transcript.md`·`events.jsonl`·`usage-summary.json`·**`issues.md`** — 그 시행에서 발생한 문제와 해결)·이전 시행 커밋 여부를 확인한 뒤 해당 core와 기록만 커밋한다. 원격 push는 하지 않는다. 실험 종료 후 `launch.py discard --run-id <id>`가 run별 Docker volume의 파일 목록(내용 아님)을 `discard-<시각>.json`에 기록한 뒤 volume을 제거한다. `credentials_file_in_volume`이 `null`이면 조회 실패이므로 "없음"으로 읽지 않는다.

## 평가 비용 기록 (진행자·판정 어댑터)

진행자(실험 진행 에이전트)와 판정 어댑터의 토큰은 **평가 비용**이다. run의 2,000만 상한에도, 조건의 비용 합계에도 넣지 않고 장부(`ledger.json`)의 `evaluation_cost` 블록에 역할별로 따로 남는다. 기록이 없는 역할은 `"not recorded"`로 보이며 0으로 읽지 않는다.

```bash
# 판정 어댑터: 그 시행을 위해 어댑터를 LLM으로 작성·수정했을 때, 작성 직후
python3 payment-settlement/claude/scripts/record_evaluation_cost.py --run-id <id> --role judging-adapter --model <보고된 모델 id> \
  --total-tokens <합> --source "<숫자의 출처: 예 작성 에이전트 완료 알림 usage>"
# 진행자: run 종료 순서 ①′에서, 그 run을 맡은 진행자 세션의 사용량으로
python3 payment-settlement/claude/scripts/record_evaluation_cost.py --run-id <id> --role conductor --model <보고된 모델 id> \
  --input-tokens N --cache-creation-input-tokens N --cache-read-input-tokens N --output-tokens N --source "<출처>"
```

원천이 네 카운터를 주면 넷 다(`usage`), 합계만 주면 `--total-tokens`만 넣는다(분할은 미관측으로 남는다). 한 진행자 세션이 여러 run을 맡아 run별로 나눌 수 없으면 `--source`에 그 세션이 맡은 run id를 함께 적는다. 기록은 `.runs/<id>/evaluation/NNNN/record.json`에 쌓이고 덮어쓰지 않는다. 판정 어댑터를 LLM 없이 썼으면 그 사실을 `issues.md`에 적는다(기록하지 않으면 `not recorded`로 남는다).

## 사이클 준비 실패 복구

`start_cycle.py`는 세 조건의 init·Compose를 모두 끝낸 뒤에만 첫 턴을 함께 시작한다. 한 조건이라도 실패하면 그 run의 `run.json`이 `SETUP_FAILED`(+`setup_error`), `cycles.json`의 해당 사이클이 `SETUP_FAILED`(+`setup_failures`·`prepared_runs`)가 되고 유료 호출은 0건이다. 복구: 원인 확인 → 준비된 run마다 `docker compose -p payment-claude-cNN-aN -f payment-settlement/claude/compose.yaml down -v`와 `launch.py discard --run-id <id>` → 그 run 디렉터리(`.runs/<id>`, 유료 호출 없음)를 `.runs/aborted-<id>-<시각>`으로 옮겨 보존 → 각 `run.json`의 `status`를 `NOT_STARTED`로 되돌리고 실패 내용을 `issues.md`에 기록 → `cycles.json` 해당 행을 `NOT_STARTED`로 되돌린 뒤 `start_cycle.py` 재실행. 첫 턴이 실패한 run은 `initial_turn_exit`·`initial_turn_failed`가 `run.json`에 남고 상태는 `RUNNING`이다 — 위 "실패한 턴의 복구"대로 같은 세션으로 이어 간다(세션 transcript가 남았으면 `--resume --kind recovery`, 없으면 `--resume` 없는 첫 턴).

## 알려진 경계

- 네트워크: `--network`를 주지 않으면 기본 bridge라 다른 컨테이너에 IP로 닿을 수 있다. 격리 주장은 파일시스템·세션·상태 범위다(Codex 판과 동일한 정직 경계).
- headless(`-p`)에서 git-guard의 `permissionDecision: ask`는 승인 UI가 없다. push는 금지 영역이므로 실험 영향은 작지만 관측 결과를 run 문서에 남긴다.
- 3번 두 번째 리뷰어(`fable-review`)는 PM의 Bash 호출이라 하네스의 `codex-scan` 훅(명령 문자열 시크릿 backstop)이 발화하지 않는다. playbook §5 ① 절차 보안 스캔이 유일한 보장이다. 리뷰어 호출이 실패하거나 사용량을 관측하지 못하면 `usage_complete=false`·`reviewer_usage_pending=true`로 남고 성공으로 위장하지 않는다.
- v1(2026-09-23)의 codex 관련 경계(bubblewrap 샌드박스 실패, read-only auth 갱신)는 v3-claude에 해당하지 않는다.

## 실험 개시 체크리스트

(v1 스모크 ①~④는 2026-09-23 Claude Code 2.1.280·v1 모델 기준이다. v3-claude는 새 모델 ID로 인증 스모크를 다시 한다.)

- [x] 유료 스모크 ①: 1번형 1턴 `RUNNER_OK` — `result.usage` 4개 카운터·`session_id` 일치·`total_cost_usd`·`modelUsage` 확인 (2026-09-23, `docs/results/setup/claude/summary.json`)
- [x] 유료 스모크 ②: 같은 run `--resume` → `RUNNER_OK_RESUMED`, transcript 파일 1개(fork 없음), request-ids 중복 0
- [x] 유료 스모크 ③: 3번형에서 SessionStart·PreToolUse 훅 실행 관측, gate-guard가 SPEC=0 Write 차단(`HOOK_BLOCKED`), 파일 미생성
- [x] 스모크 ④: 컨테이너 안 `codex exec --sandbox read-only`가 read-only auth로 성공, 래퍼가 세션·usage 보존
- [x] config volume에 `.credentials.json` 없음(`.claude.json`·policy-limits·remote-settings만) — 폐기 절차는 파일 목록 기록 후 `docker volume rm`
- [x] `context_variant`: CLI가 두 모델 모두 `contextWindow 1000000`을 보고 — `[1m]` 접미 없이 1M 적용, `default` 유지
- [ ] `result.usage`의 sidechain 포함 여부·`CLAUDE_CODE_SUBAGENT_MODEL` 전파: 파일럿 run의 산출물로 판정(`freeze_readiness.py --pilot-run`, worker 모델 일치 포함) — 그 전까지 `usage_cross_check`는 관측치
- [ ] 하네스 조건 워커 모델: 구현·테스트·리뷰 워커는 `claude-opus-5-5`(`CLAUDE_CODE_SUBAGENT_MODEL`), 탐색 워커 `explorer`는 하네스 정의대로 Sonnet. 2.1.326 번들 판독상 에이전트 정의의 모델이 이 환경변수보다 우선하고 `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`만 그것을 덮는다(실행기는 FORCE를 설정하지 않는다) — **파일럿의 요청 단위 모델 기록으로 확인할 항목**. 판정은 요청마다: transcript `agent-*.meta.json`의 `agentType`이 `explorer`인 요청만 Sonnet 허용, 나머지 워커 요청은 Opus 5.5(`freeze_readiness.py --pilot-run`). PM이 Agent 호출에 `model: opus` 같은 별칭을 넘기면 별칭이 가리키는 버전이 쓰이므로 이 경우도 요청 단위 기록으로 판정된다
- [ ] 서브에이전트 effort: 실행기는 요청마다 transcript의 `perTurnEffort`를 `observed_tools_and_effort.by_role.<role>.efforts`에 남긴다. 값이 없는 요청은 `unobserved`다. 파일럿에서 worker 요청에 `high`가 기록되지 않으면 **effort 전파 미검증**을 결과의 한계로 적는다(설정했다는 사실은 증거가 아니다)
- [ ] 본 실험 `--max-turns`/`--max-budget-usd` 값 고정(스모크는 3~4턴·1 USD)
- [x] **D5 웹 도구**: 세 조건 모두 허용하고 기록(2026-11-17 결정). self-test가 모든 arm에 `WebSearch`/`WebFetch`가 노출되는지 단정한다(`docs/results/setup/v3/`)
- [ ] arm 3 두 번째 리뷰어(`fable-review`) 사용량: 실행기가 요청 단위 `reviewer` 이벤트로 분리 집계한다(단위테스트). 실제 호출에서 관측되는지는 파일럿에서 확인
- [ ] 본 실험 개시 전 토큰 재발급(최초 토큰은 채팅 경유 노출 — `docs/auth-token.md`)
- [ ] C01..C20 판정기·DB adapter·장애 주입 완성(Codex 쪽 공유) 후 파일럿 1회
- [ ] 이미지 digest·모델·계약·평가기·seed 동결 및 baseline commit 기록
- [ ] v3-claude 인증 스모크(새 토큰, **arm 1·2·3 각각** 1턴) → `scripts/freeze_readiness.py --smoke-run <arm1> --smoke-run <arm2> --smoke-run <arm3>`로 동결(대리 사용자 모델은 합의값 `claude-opus-5-5`로 기록된다)(세 조건이 모두 없으면 동결 거부, 플래그는 증거 파일에서만 도출)
- [ ] 파일럿 개시 전: `preflight.py --stage pilot <ledger+readiness>` 전 항목 true (`pilot_passed`만 면제, 가짜 true 금지 — 프로토콜·상한 포함)
- [ ] 파일럿 init: `python3 payment-settlement/claude/runner/launch.py init --run-id claude-pilot-arm3 --arm 3 --core 1 --harness payment-settlement/claude/harness-source --readiness payment-settlement/claude/docs/results/readiness.json` — readiness가 있으면 `--image`를 생략해도 동결 digest(`image_digests.runner`)를 쓰고 다른 이미지는 거부한다. manifest의 `image`·`image_frozen=true`·`readiness`로 확인한다
- [ ] 파일럿 뒤: `freeze_readiness.py ... --pilot-run <id> --replace`로 `pilot_passed`를 파일럿 산출물에서 도출(파일럿 manifest가 init 때 기록한 `contract_sha256`·`image`가 지금 쓰는 동결값과 같을 때만 통과). `--replace`는 플래그만 다시 도출한다 — 동결 입력(`frozen-inputs.json`)이나 이미지 id가 바뀌었으면 거부된다(프로토콜 버전 변경·전 조건 재실행 대상) → 사이클 개시 게이트(`start_cycle.py`, 항상 `--stage cycle` 기준)
