# Claude 판 구동과 시행 절차

명령은 저장소 루트 기준이다. 공유 fixture(`../../infra`, `../../evaluation`)와 계약(`../../docs`)은 Codex 판과 같다.

## 준비

```bash
python3 payment-settlement/claude/scripts/prepare.py
python3 payment-settlement/claude/scripts/verify_setup.py
docker build -t payment-claude-runner:2.1.326 payment-settlement/claude/runner
```

인증: 구독 계정에서 한 번 `claude setup-token`을 실행해 장기 토큰을 만들고, **repo 밖** 파일(예: `~/.config/payment-lab/claude.env`)에 `CLAUDE_CODE_OAUTH_TOKEN=<토큰>` 한 줄만 넣어 `chmod 600` 한다. 실행기는 이 파일을 `docker --env-file`로만 넘기고 경로만 기록한다(repo 안 경로·다른 키·느슨한 권한은 거부). 추가 자격증명 파일은 없다.

- 1·2번: 컨테이너는 이미지 사용자 `node`(uid 1000)로 `claude`를 바로 실행하고 토큰은 컨테이너 환경에서 받는다(변경 없음).
- 3번(명세 §11, 파일럿 01 뒤): Claude Code 2.1.326은 Bash 도구의 자식 프로세스 환경에서 `CLAUDE_CODE_OAUTH_TOKEN`을 뺀다 — 그래서 PM 셸에서 띄우던 리뷰어가 `Not logged in`으로 실패했다. 이제 3번 컨테이너는 root로 시작하되 능력은 `SETUID`·`SETGID`·`DAC_READ_SEARCH` 셋뿐이고(`--cap-drop=ALL`·`no-new-privileges` 유지), 시작 스크립트 `lab-arm3-start`가 ① 리뷰어 도우미(`fable_review.py serve`)를 별도 계정 `fablereview`(uid 2001)로 띄워 토큰을 **그 프로세스 환경에만** 두고(다른 uid는 `/proc/<pid>/environ`을 읽지 못한다), ② PM을 지금과 같은 `node`로 실행하면서 토큰을 환경변수가 아니라 상속한 파이프(`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3`, CLI가 한 번 읽음)로 준다. PM 쪽 `fable-review`는 소켓(`/run/fable-review/socket`)으로 작업 디렉터리와 프롬프트만 보내고, 모델·도구·effort·리뷰어 config dir은 도우미 시작 때 고정된다. 리뷰어 config dir(리뷰어 volume, 0700 `fablereview`)과 도우미의 프로세스 환경은 PM의 사용자가 읽을 수 없다. 도우미의 `DAC_READ_SEARCH`는 PM이 `mktemp -d`(0700)로 만든 미러를 읽기 위한 것이고 PM의 파일에 쓰지는 못한다. 무과금 실측(2026-11-18, 더미 토큰·`--network none`): `node`로 읽을 수 있는 파일·`/proc/*/environ`·`cmdline`·`fd` 어디에도 토큰이 없음, 도우미 종료 시도 거부, 미러 읽기 가능·쓰기 거부, 형식 밖 요청 거부(exit 94).

## 시행 (한 번에 하나, schedule.json 순서)

역할(요구사항 명세 §0, 2026-11-18 결정): **실험 진행자는 Fable 5.2(`claude-fable-5-2`) 에이전트**다 — run 실행, 판정기·어댑터 실행, 산출물과 관측 메모 저장, 도중 피드백 전달(`prompts/feedback.md` 형식), 기록·커밋까지 맡고 구현에 개입하지 않는다. 메인 세션은 실험을 통제한다(진행자 브리핑, 승인, 최종 판정표 `verdict.json` 작성, 결과 회수·보고). 진행자는 실험 대상이 아니라 운영 쪽이며, 그 사용량은 평가 비용 장부에 모델 `claude-fable-5-2`로 기록한다(아래 "평가 비용 기록").

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
# 3번만: 대리 사용자 답을 PM에게 전하는 턴. 프롬프트 = consults/NNNN/response.txt 바이트 그대로(머리말·진행자 문구를 붙이지 않는다)
python3 $L turn --run-id claude-arm3-core01 --resume --kind answer --prompt .runs/claude-arm3-core01/consults/0001/response.txt --env-file ~/.config/payment-lab/claude.env --cache-policy observed-cache
python3 $L normalize --run-id claude-arm1-core01     # 역할별 파일 → 공유 adapter → usage-summary.json
```

턴 제한 시간(2026-11-18 결정): **턴마다 따로 정한 제한은 없고 run 상한 8시간만 있다.** `launch.py turn`과 `start_cycle.py`의 `--timeout-seconds` 기본값은 28,800초이고, 실행기가 매 턴 남은 run 시간으로 줄인다(`invocation.json`의 `effective_timeout_seconds`). 값을 주지 않는다.

비용(파일럿 01 실측): `--resume` 턴의 `total_cost_usd`·`modelUsage`는 세션 누적값이다. 장부(`ledger.json`)의 `cost_usd`는 고정 세션의 마지막 누적값(턴마다 직전 턴 대비 증가분 — `cost_by_call`) + 리뷰어 호출 + 대리 사용자 호출이고, 관측하지 못한 값이 있으면 `null`이다(`cost_semantics`). 한 턴에 `result` 이벤트가 여럿이면(백그라운드 워커가 끝난 뒤 세션이 다시 이어짐) 네 카운터를 모두 더해 `usage_cross_check.reported`로 대조한다(`result_events`). `result.usage`는 서브에이전트 요청을 포함하지 않으므로 기대 일치는 `main_delta=0`이고, 상한 게이트는 요청 단위 장부만 쓴다.

이미지: 동결 전(인증 스모크)에는 `--image`(기본 `payment-claude-runner:2.1.326` 태그)를 쓴다. `readiness.json`이 생긴 뒤의 `init`(파일럿·사이클)은 그 동결 digest만 쓴다 — `--image`를 생략하면 digest가 들어가고, 다른 값은 거부된다(바꾸려면 새 프로토콜 버전). 동결 전 실행은 **기본 위치**(`docs/results/readiness.json`)에 동결 파일이 없을 때만 된다 — `--readiness`로 경로를 명시했는데 그 파일이 없으면 `init`은 거부한다. 3번의 대리 사용자 호출(`consult_contract.py`)은 그 run의 manifest에 고정된 이미지(`image`)로만 실행한다(`invocation.json`의 `image`).

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
| `recovery` | 바로 앞 턴이 `ok`로 끝나지 않았을 때 그 턴을 이어 감. **그 턴과 같은 프롬프트만 보낸다**(앞 턴 `invocation.json`의 `prompt_sha256`과 일치해야 하며, 다르거나 해시가 없으면 호출 전 거부). 앞 턴이 `ok`면 거부. **앞 턴이 `ok`가 아니면 재개 턴은 `recovery`만 허용**(`feedback`·`answer` 거부 — 실패한 턴도 모델에 닿았을 수 있으므로 그 턴의 종류로 센다) | 이어 가는 턴의 종류로 센다 |

`--kind` 없는 재개 턴은 유료 호출 전에 거부된다. `turn_kind`가 없는(또는 `invocation.json`이 없는) 턴 디렉터리가 있으면 상한 계산이 실패하고(건너뛰지 않음) 그 run의 다음 호출은 막힌다. `limit_gate` 결과(`invocation.json`의 `limits_before_call`)와 `ledger.json`에 `turn_kinds`(`delivered`: 성공 전달 수 `initial`·`feedback`·`answer` / `declared`: 시도별 선언 종류 수)와 `answer_turns`(limit 상태)가 남는다. 실패한 `feedback` 턴은 세지 않고, 그 `recovery`가 성공하면 1회로 센다(종전 "성공 턴만 센다" 규칙 유지).

상한에 닿으면 `censored.json`(상태·그때의 사용량·어느 종류의 호출에서 닿았는지 `call`: `turn` 또는 `user-proxy`)을 남기고 호출을 거부하며, 이후 모든 호출도 거부한다. 호출 도중 넘으면 호출 뒤 `censored.json`에 초과분(`overshoot`)과 그 호출의 종류를 기록한다. 산출물은 지우지 않는다. 상한은 **다음 유료 호출을 막는 규칙**이다(2026-11-17 결정): `censored.json`은 지우거나 고치지 않으며, 그 run에는 완료 여부와 관계없이 더 이상 호출이 나가지 않는다. 종료 상태는 아래 "종료와 커밋"의 `finish_run.py`가 판정표(`verdict.json`)에서 도출하고, `censored.json`은 통과하지 못한 run의 `CENSORED_*` 상태와 상한 표시(`cap_*`)에만 쓰인다(종료 상태 표도 그 절).

## 실패한 턴의 복구

`turn`이 유료 호출 이후 예외로 끝나면 `turns/NNNN/`에 원문과 `result.json{status:"failed"}`가 남는다(`cli_status`는 CLI exit code, `status`는 수집·토큰 스캔까지 끝난 뒤의 최종 판정). 다음 `turn`은 새 번호를 받는다. 실패한 턴을 이어 가는 재개 턴은 `--kind recovery`로 선언한다(위 상한 절의 표 — 실패한 `feedback`의 복구는 성공 시 피드백 1회). **첫 턴/재개 판정은 config volume에 그 session id의 transcript가 존재하는지로 한다**: 실패한 호출이라도 transcript를 남겼으면 다음 턴은 `--resume`이어야 하고, 남기지 않았으면 `--session-id` 첫 턴이다(실행기가 실측해 반대 지정을 거부한다). 실패 턴에서 완전히 수집된 요청은 `request-ids.txt`에 들어가고, 원장에 들어가기 전에 중단된 요청은 다음 턴에서 다시 집계된다. `normalize`는 모든 턴의 이벤트를 requestId로 병합해(첫 등장 usage, worker 역할은 단조) 중복을 막는다. 디렉터리를 손으로 지우지 않는다.

3번의 호출 전 훅 self-test가 실패하면(또는 self-test 자체가 돌지 못하면) 턴 디렉터리를 만들기 전에 멈추고 유료 호출은 나가지 않는다. 그 실패는 `.runs/<run>/selftest-failures-before-call/<시각>-<id>.json`(`paid_call: false`·시도한 `turn_kind`·self-test 보고)에 남는다. 이 기록은 턴이 아니므로 턴 번호·상한 셈·장부에 들어가지 않고, 다음 시도는 `recovery`가 아니라 원래 선언하려던 종류의 턴이다. 대신 그 run의 `isolation_verified`는 false가 된다(아래 증거 칸 표) — 원인을 `issues.md`에 적고 메인에 보고한다.

## 종료와 커밋

순서(고정, 요구사항 명세 §10.1):

```
마지막 턴 종료(또는 상한 차단)
  → ① 판정 실행: 진행자가 판정기·어댑터를 실행하고 산출물과 시나리오별 관측 메모를 기록 폴더에 저장(통과 여부는 정하지 않는다)
  → ② 판정표: 메인 세션이 산출물을 직접 읽고 기록 폴더에 verdict.json을 쓴다
  → ③ launch.py normalize → ④ capture_turn.py(남은 턴 — 기록 폴더의 파일이 여기서 마지막으로 바뀐다)
  → ⑤ 종료 기록: finish_run.py --run-id <id>(근거 해시는 커밋될 파일 그대로의 값)
  → ⑥ launch.py discard → ⑦ commit_run.py(본 실험만)
```

⑤의 시점에 있어야 하는 것: 기록 폴더의 `run.json`(본 실험은 `prepare.py`, 파일럿은 ④의 `capture_turn.py`가 만든다)·`verdict.json`과 그 근거 파일(①②), 실행 폴더 `.runs/<id>`(manifest·턴·consults — ⑥ `discard`는 volume만 지우고 이 폴더는 남긴다), manifest가 가리키는 `readiness.json`. `finish_run.py`는 실행 폴더에서 끝난(`result.json`의 `status`가 `ok`/`failed`) 턴이 하나라도 기록 폴더 `turns/NNNN/`에 없거나, 기록이 끝나지 않았거나(`capture_turn.py`가 턴 기록의 마지막 동작으로 쓰는 `turns/NNNN/captured.json`이 없음 — 이 파일은 그 턴 폴더에 남긴 파일 전부와 sha256을 적는다), 그 뒤 적힌 파일이 없어지거나 바뀌었거나, `result.json`이 다르면 그 턴 번호를 대며 거부한다 — ④를 마저 하고 다시 실행한다. 도중에 멈춘 턴(`captured.json` 없음)은 같은 `capture_turn.py --run-id <id> --turn N --analysis <분석 파일>`을 다시 실행하면 그 턴 폴더를 지우고 처음부터 다시 기록한다(폴더의 파일은 모두 실행 폴더와 `--analysis`에서 다시 만들어진다). 단 기록 폴더의 `events.jsonl`(덧붙이기만 하는 기록)에 그 턴이 이미 들어가 있으면 중복 기록을 막기 위해 거부한다 — 그때는 `events.jsonl`과 턴 폴더를 확인해 메인에 보고한다. `captured.json`이 있는 턴을 다시 기록하려 하면 거부된다. 끝나지 못한 턴(`result.json` 없음 등, `capture_turn.py`가 받지 않음)은 거부하지 않고 `run.json`의 `turns_not_capturable`에 남는다(그 run의 장부는 `usage_complete=false`). ⑤ 뒤에는 기록 폴더를 바꾸지 않는다(바꿨다면 `--supersede --reason`으로 종료 기록을 다시 한다).

기록 폴더는 manifest가 정한다: 본 실험은 `docs/experiments/<arm>/core<N>/`, 파일럿(`init --pilot`)은 `docs/pilots/<run id>/`. `capture_turn.py`·`finish_run.py`는 그 폴더의 `run.json`의 `run_id`가 실행 run id와 다르면 거부한다.

### 판정표 `verdict.json` (메인이 쓴다)

기록 폴더에 둔다. 형식(예시: `docs/results/verdict.example.json` — 예시 표시 `_example`가 있으면 거부된다):

```json
{
  "run_id": "claude-arm1-core01",
  "judged_by": "판정한 메인 세션 표시(필수, 비어 있으면 거부)",
  "judged_at": "2026-11-18T00:00:00+00:00",
  "criteria": {
    "C01": {"result": "PASS", "evidence": ["acceptance-http.json", "acceptance-audit.json"], "note": "판정 근거 요지"},
    "...": "C01~C20 정확히 20개",
    "C20": {"result": "NOT_RUN", "evidence": [], "note": "실행하지 못한 사유(필수)"}
  }
}
```

- `result`는 `PASS`·`FAIL`·`PARTIAL`·`NOT_RUN` 중 하나. 20개가 모두 `PASS`일 때만 통과다(`PARTIAL`·`NOT_RUN`은 통과가 아니다 — `evaluation/README.md` 결과 해석).
- **모든 기준은 근거가 있어야 한다**(명세 §10.2): `PASS`·`FAIL`·`PARTIAL`은 실제로 있는 근거 파일 1개 이상, `NOT_RUN`은 비어 있지 않은 사유(`note`). 하나라도 채우지 못한 판정표로는 종료 기록을 할 수 없다 — 결과가 통과든 아니든, `--invalid-infra`와 함께여도 거부되고 메시지가 해당 기준을 나열한다. 판정표를 고친 뒤 다시 실행한다.
- `evidence`는 기록 폴더 기준 상대 경로 목록(폴더 밖·절대 경로는 거부). 종료 기록은 파일마다 존재 여부와 sha256을 `run.json`의 `verdict.evidence`에 남긴다. 근거가 있는 기준에 적힌 다른 파일이 없으면 `evidence_missing`에 남고 `all_acceptance_passed`는 false다.
- 기계가 확인하는 것은 형식·근거 파일의 존재와 해시·`judged_by`까지다. 판정 내용이 맞는지는 확인하지 못한다.

### 종료 기록 `finish_run.py`

```bash
python3 payment-settlement/claude/scripts/finish_run.py --run-id <id>                         # 판정표에서 도출
python3 payment-settlement/claude/scripts/finish_run.py --run-id <id> --invalid-infra "<사유>" --evidence <기록 폴더 기준 파일> [--evidence ...]
python3 payment-settlement/claude/scripts/finish_run.py --run-id <id> --supersede --reason "<정정 사유>"
```

- PASSED·FAILED는 인자로 받지 않는다. `INVALID_INFRA`만 인자다: 구현과 무관한 실행 환경의 실패(Docker·호스트·판정 도구 자체의 고장)일 때만, 사유와 근거 파일(1개 이상, 기록 폴더 안)과 함께, 진행자가 제안하고 메인이 확인한 뒤에 쓴다. 판정 실행이 구현 때문에 실패한 것은 `FAIL`이다. 판정표가 20개 모두 `PASS`이면 `INVALID_INFRA`는 거부된다.
- 대상은 턴이 한 번 이상 나간 run이다. 기록 상태가 `NOT_STARTED`·`SETUP_FAILED`이거나 턴 기록이 없으면 거부한다(아래 "사이클 준비 실패 복구").
- run당 한 번이다. 이미 종료 상태(또는 `finished_at`)가 있으면 거부하고, 정정은 `--supersede --reason`으로만 한다. 이전 `run.json`은 `run.superseded-<시각>.json`으로 옆에 남고 새 기록의 `supersedes`가 그 파일·해시·사유를 가리킨다. 파일럿 재판정도 같은 방법이다.
- 상한에 호출 전 차단으로 끝난 run은 새 턴 없이 그 시점까지의 기록으로 판정·종료 기록·커밋한다.

종료 상태(실제 분기 — `finish_run.terminal`·`cap_annotation`). **통과 여부는 판정표만으로 정하고, 상한 기록은 표시로 붙는다**(명세 §10.2, 2026-11-18 — 이전의 "턴 초과일 때만 완료 + 초과", "호출 전 차단 + 전부 PASS는 거부"를 대체):

| 판정표 | `censored.json` | `run.json` `status` | 함께 남는 것 |
|---|---|---|---|
| 20개 모두 `PASS` | 없음 | `PASSED` | `cap_reached=false`, `cap`·`cap_*`는 null — 결과 표 "완료" |
| 20개 모두 `PASS` | 있음, `first.when="before call"`(턴·대리 사용자 호출이 호출 전에 막힘, `CENSORED_TURNS` 포함) | `PASSED` | `cap_reached=true`, `cap_status`·`cap_when="before call"`·`cap_call`(`turn`/`user-proxy`), `cap_overshoot=null` — "완료 + 상한 도달" |
| 20개 모두 `PASS` | 있음, `first.when="after call"`(턴·대리 사용자 호출 도중 초과) | `PASSED` | `cap_reached=true`, `cap_status`·`cap_when="after call"`·`cap_call`·`cap_overshoot`(초과량) — "완료 + 초과" |
| 하나라도 `PASS`가 아님 | 있음 | `censored.json`의 `CENSORED_*` | 위와 같은 `cap_*` 표시 |
| 하나라도 `PASS`가 아님 | 없음 | `FAILED` | `cap_reached=false` |
| (인자 `--invalid-infra`) | 무관 | `INVALID_INFRA` | `invalid_infra`(사유·근거 해시), `all_acceptance_passed=false`, 상한 기록이 있으면 같은 `cap_*` 표시 |
| `verdict.json` 없음(그리고 `--invalid-infra` 없음) | 무관 | **거부** | — |
| 근거가 빠진 기준이 있는 `verdict.json`(위 판정표 규칙) | 무관 | **거부**(`--invalid-infra`여도) | — |

`cap`은 `censored.json`의 `first`를 옮긴 블록(상태·시점·호출 종류·그때 사용량·한도·초과량)이고 `cap_status`·`cap_when`·`cap_call`·`cap_overshoot`는 그 요약이다. `call` 칸이 없는 옛 상한 기록은 `cap_call=null`로 옮긴다. `censored.json`은 고치지 않으므로 상한에 닿은 run은 통과 여부와 관계없이 더 이상 유료 호출을 받지 않는다. 상한 기록의 `status`가 `CENSORED_BUDGET`·`CENSORED_TIME`·`CENSORED_TURNS`가 아니면 통과하지 못한 run의 종료 기록은 거부된다(기록을 확인해 메인에 보고).

`commit_run.py`가 `PASSED`에 요구하는 네 칸은 종료 기록이 아래 원천에서만 쓴다(원천이 없거나 거짓이면 false — `run.json`에 미리 들어 있던 값은 덮어쓴다, 도출 내역은 `evidence_derivation`):

| 칸 | 원천 |
|---|---|
| `all_acceptance_passed` | 판정표 20개 모두 `PASS` + 적힌 근거 파일이 전부 존재(기준마다 존재하는 근거 1개 이상은 판정표 규칙이라 못 채우면 종료 기록 자체가 거부된다) |
| `usage_complete` | 실행 폴더(`.runs/<id>`)에서 종료 기록 시점에 계산한 run 장부의 `usage_complete` |
| `all_role_models_verified` | manifest의 역할별 관측 모델 + 요청 이벤트(대리 사용자 포함)를 합의 모델과 대조 — 동결과 같은 함수 `freeze_readiness.run_role_check`. 있어야 할 역할(1·2번 `implementer`, 3번 `pm`·`worker`·`reviewer`·`user-proxy` — `freeze_readiness.FINISH_ROLES`)이 관측되지 않으면 false. 3번은 사전 인터뷰가 의무이므로 대리 사용자 호출이 한 번도 관측되지 않은 run은 false다(동결의 파일럿 검사는 `PILOT_ROLES` = `pm`·`worker`·`reviewer` 그대로). explorer의 Sonnet 예외는 요청 단위 |
| `isolation_verified` | 그 run의 init self-test와 3번의 호출 전 self-test가 모두 통과(턴 기록의 `hook_selftest_before_call` + 턴 전에 실패해 `selftest-failures-before-call/`에 남은 기록이 하나도 없음) + 그 run의 도구 목록이 동결 파일(manifest가 가리키는 `readiness.json`의 `evidence.smoke_runs`)의 그 조건 도구 목록과 같음 — `freeze_readiness.run_isolation_check`. 세 조건 간 비교는 동결 때 한 번 |

통과했는데 네 칸 중 하나라도 false면 상태는 `PASSED`로 쓰이고 `commit_run.py`가 커밋을 막는다. 우회 경로는 없다: 진행자는 거기서 멈추고 원인을 `issues.md`에 적어 메인에 보고하며, 메인이 사용자에게 보고한다(다음 사이클은 시작하지 않는다). 장부가 미완전(`usage_complete=false`)이면 토큰 합은 하한값이다.

### 커밋 뒤 정정

`commit_run.py`로 커밋한 run을 고쳐야 하면(판정표 재판정, 빠진 근거, 종료 기록 오류): ① 기록 폴더에서 고칠 것(`verdict.json`·근거 파일·`issues.md`에 정정 사유)을 고친다 → ② `finish_run.py --run-id <id> --supersede --reason "<정정 사유>"` — 이전 `run.json`은 `run.superseded-<시각>.json`으로 남고 새 기록의 `supersedes`가 그것을 가리킨다(이전 커밋도 그대로 남는다) → ③ `commit_run.py --arm N --core M`을 다시 실행한다. 같은 run의 두 번째 `experiment: <run_id> <상태>` 커밋이 생기는 것이 정상이다(이전 시행 커밋 확인은 제목 접두로 하므로 그대로 통과한다). 커밋을 고치거나(`--amend`·rebase) 지우지 않는다. 정정한 run이 이미 끝난 사이클에 속하면 메인에 보고하고, 결과 표는 가장 최근 커밋의 `run.json`을 쓴다. 파일럿 정정은 같은 ①②에 일반 docs 커밋이다.

파일럿은 `commit_run.py` 대상이 아니다(본 실험 30건 전용). 파일럿 기록(`docs/pilots/<run id>/`)은 일반 docs 커밋으로 남긴다. 파일럿의 판정표·종료 기록은 보고 ②의 자료이고, 동결의 `pilot_passed`는 지금처럼 실행 폴더(`.runs`)에서 도출한다(`freeze_readiness.py --pilot-run`).

⑥ `discard`는 3번 리뷰어 volume의 `raw/<session>.json`이 전부 run 디렉터리(`turns/*/reviewer-raw/`)로 회수됐는지 먼저 확인하고, 아니면 아무 volume도 지우지 않고 실패한다. 제거 실패도 non-zero로 끝나며 `discard-<시각>.json`의 `complete`가 false로 남는다. 다음 사이클의 `start_cycle.py`는 앞 사이클 세 run의 terminal 상태·`experiment: <run_id>` 커밋·미커밋 변경 없음·`complete=true` discard 기록을 증거로 확인한다(`cycles.json`의 상태 문자열은 게이트가 아니다).

`scripts/commit_run.py --arm N --core M`(Codex 판의 바이트 복사본 — Codex 쪽이 바뀌면 `verify_setup.py` 패리티가 깨지므로 그때 다시 복사한다)이 상태·문서(`summary.md`·`transcript.md`·`events.jsonl`·`usage-summary.json`·**`issues.md`** — 그 시행에서 발생한 문제와 해결)·이전 시행 커밋 여부를 확인한 뒤 해당 core와 기록만 커밋한다. 원격 push는 하지 않는다. 실험 종료 후 `launch.py discard --run-id <id>`가 run별 Docker volume의 파일 목록(내용 아님)을 `discard-<시각>.json`에 기록한 뒤 volume을 제거한다. `credentials_file_in_volume`이 `null`이면 조회 실패이므로 "없음"으로 읽지 않는다.

## 평가 비용 기록 (진행자·판정 어댑터)

진행자(실험 진행 에이전트, Fable 5.2 `claude-fable-5-2`)와 판정 어댑터의 토큰은 **평가 비용**이고, run이 아니라 **실험 전체 단위 장부** `docs/results/evaluation-cost.jsonl`에 기록한다(2026-11-17 사용자 결정). run의 2,000만 상한·조건별 비용·run 장부(`ledger.json`)에는 들어가지 않고, 종료 기록도 평가 비용을 요구하지 않는다(진행자 사용량은 세션이 끝나야 나온다). 진행자 피드백의 토큰을 떼어 실험군에 넣을 방법은 없으므로 결과 표의 한계로 적는다(세 조건이 같은 진행자·같은 방식이라 조건 간 비교는 유지).

기록 주체는 **메인**이다(진행자 에이전트 완료 알림의 usage). 한 줄 = 진행자 세션 1개 또는 어댑터 작성 1건. 어댑터 줄(`judging-adapter`)은 건마다 한 줄이고 `--item`(건을 구분하는 표시 — 예: 대상 run id·어댑터 이름)이 **필수**다(없거나 비어 있으면 거부). 같은 세션이 어댑터를 여러 번 작성했으면 건마다 다른 `--item`으로 한 줄씩 쓴다. 진행자(`conductor`) 줄은 세션당 한 줄이고 `--item`을 받지 않는다. 덧붙이기만 하며(같은 역할·세션·`item`은 두 번 기록되지 않는다, 읽을 수 없는 줄이 있으면 덧붙이지 않고 멈춘다), 맡은 run id를 전부 적는다.

```bash
# 진행자 세션 1개(맡은 run을 모두 --run-id로). 진행자가 어댑터를 직접 썼으면 --includes-judging-adapter, 어댑터 줄은 따로 쓰지 않는다
python3 payment-settlement/claude/scripts/record_evaluation_cost.py --role conductor --session <세션 식별자> --model <보고된 모델 id> \
  --input-tokens N --cache-creation-input-tokens N --cache-read-input-tokens N --output-tokens N \
  --source "<숫자의 출처: 예 진행자 완료 알림 usage>" --run-id claude-arm1-core01 --run-id claude-arm2-core01 [--includes-judging-adapter]
# 진행자가 아닌 에이전트가 어댑터를 작성한 1건(--item 필수. 같은 세션의 다음 건은 다른 --item으로 한 줄 더)
python3 payment-settlement/claude/scripts/record_evaluation_cost.py --role judging-adapter --session <식별자> --item "<건 표시: 예 claude-arm1-core01 어댑터>" --model <id> \
  --total-tokens <합> --source "<출처>" --run-id <id>
```

줄의 칸: `role`·`session`·`item`(어댑터 건 표시 — 어댑터 줄은 항상 있음, 진행자 줄은 null)·`model`·`usage`(네 카운터, 합계만 있으면 null)·`total_tokens`·`tokens_source`·`run_ids`·`cost_usd`(선택, 0 이상의 유한수)·`includes_judging_adapter`·`recorded_at`. 누락 점검은 결과 정리(task 10)에서 한다: 본 실험 30건과 파일럿의 모든 run id가 어느 한 줄의 `run_ids`에 들어 있어야 하고, 빠진 run은 표에 "평가 비용 미기록"으로 드러난다.

## 사이클 준비 실패 복구

`start_cycle.py`는 세 조건의 init·Compose를 모두 끝낸 뒤에만 첫 턴을 함께 시작한다. 한 조건이라도 실패하면 그 run의 `run.json`이 `SETUP_FAILED`(+`setup_error`), `cycles.json`의 해당 사이클이 `SETUP_FAILED`(+`setup_failures`·`prepared_runs`)가 되고 유료 호출은 0건이다. 복구: 원인 확인 → 준비된 run마다 `docker compose -p payment-claude-cNN-aN -f payment-settlement/claude/compose.yaml down -v`와 `launch.py discard --run-id <id>` → 그 run 디렉터리(`.runs/<id>`, 유료 호출 없음)를 `.runs/aborted-<id>-<시각>`으로 옮겨 보존 → 각 `run.json`의 `status`를 `NOT_STARTED`로 되돌리고 실패 내용을 `issues.md`에 기록 → `cycles.json` 해당 행을 `NOT_STARTED`로 되돌린 뒤 `start_cycle.py` 재실행. 첫 턴이 실패한 run은 `initial_turn_exit`·`initial_turn_failed`가 `run.json`에 남고 상태는 `RUNNING`이다 — 위 "실패한 턴의 복구"대로 같은 세션으로 이어 간다(세션 transcript가 남았으면 `--resume --kind recovery`, 없으면 `--resume` 없는 첫 턴).

## 알려진 경계

- 네트워크: `--network`를 주지 않으면 기본 bridge라 다른 컨테이너에 IP로 닿을 수 있다. 격리 주장은 파일시스템·세션·상태 범위다(Codex 판과 동일한 정직 경계).
- headless(`-p`)에서 git-guard의 `permissionDecision: ask`는 승인 UI가 없다. push는 금지 영역이므로 실험 영향은 작지만 관측 결과를 run 문서에 남긴다.
- 3번 두 번째 리뷰어(`fable-review`)는 PM의 Bash 호출이라 하네스의 `codex-scan` 훅(명령 문자열 시크릿 backstop)이 발화하지 않는다. playbook §5 ① 절차 보안 스캔이 유일한 보장이다. 리뷰어 호출이 실패하거나 사용량을 관측하지 못하면 `usage_complete=false`·`reviewer_usage_pending=true`로 남고 성공으로 위장하지 않는다. 예외 하나: API 요청 없이 실패한 시도(원시 결과가 오류·네 카운터 0, transcript에 requestId 있는 줄 없이 `<synthetic>` 0 사용량 줄만)는 "실패한 시도, 사용량 0"으로 `reviewer.failed_attempts_without_requests`에 남고 턴을 미완전으로 만들지 않는다. 실패 시도 수는 턴의 `reviewer.calls_failed_this_turn`과 장부의 `reviewer_calls_failed`·`reviewer_failed_attempts_without_requests`에 보인다.
- 3번 `fable-review` 종료 코드: 0 성공, 96 빈/오류 리뷰, 97 시도 기록 불가(호출 거부), 94 요청 거부(작업 디렉터리가 `/tmp`·`/workspace` 밖, 빈 프롬프트, 형식 밖 요청), 95 도우미에 닿지 못함, 98 리뷰어 없음(1·2번), 2 인자 있음. 도우미가 준비되지 않으면 `lab-arm3-start`가 PM을 시작하지 않고 exit 91로 끝난다(턴 실패, 유료 호출 없음). 도우미의 로그는 턴의 `stderr.txt`에 `[fable-review-helper]`·`[lab-arm3-start]`로 남는다.
- 3번 격리의 전제: 호스트 `kernel.yama.ptrace_scope=1`(2026-11-18 확인)이라 PM의 Bash 자식이 부모 `claude`의 메모리(`/proc/<pid>/mem`)를 읽지 못한다. 이 값이 0인 호스트에서는 같은 uid의 메모리 읽기가 가능해지므로 실행 전 확인한다.
- v1(2026-09-23)의 codex 관련 경계(bubblewrap 샌드박스 실패, read-only auth 갱신)는 v3-claude에 해당하지 않는다.

## v3 인증 스모크와 동결

### 스모크 (진행자, 유료 — 승인된 뒤에만)

프롬프트는 9월 증거의 파일을 그대로 쓴다(`docs/results/setup/claude/`). 조건마다 run 하나·턴 하나, 상한은 모두 `--max-budget-usd 1 --timeout-seconds 600`이고 `--max-turns`만 다르다(1·2번 3, 3번 4 — 2026-11-17 스모크에서 쓴 값). 이미지는 동결 전이므로 기본 태그 `payment-claude-runner:2.1.326`(이 절 앞의 `docker build`).

```bash
L=payment-settlement/claude/runner/launch.py
P=payment-settlement/claude/docs/results/setup/claude
E=~/.config/payment-lab/claude.env
python3 $L init --run-id smoke-v3-arm1 --arm 1 --core 1
python3 $L init --run-id smoke-v3-arm2 --arm 2 --core 1
python3 $L init --run-id smoke-v3-arm3 --arm 3 --core 1 --harness payment-settlement/claude/harness-source
# 1·2번: "RUNNER_OK" 한 줄만 답하게 하는 프롬프트
python3 $L turn --run-id smoke-v3-arm1 --prompt $P/smoke-1-prompt.txt --env-file $E --cache-policy observed-cache --max-turns 3 --max-budget-usd 1 --timeout-seconds 600
python3 $L turn --run-id smoke-v3-arm2 --prompt $P/smoke-1-prompt.txt --env-file $E --cache-policy observed-cache --max-turns 3 --max-budget-usd 1 --timeout-seconds 600
# 3번: Write로 src/main/kotlin/lab/payment/Probe.kt를 만들게 하고 HOOK_BLOCKED/HOOK_PASSED로 답하게 하는 음성 프롬프트
python3 $L turn --run-id smoke-v3-arm3 --prompt $P/smoke-3-arm3-negative-prompt.txt --env-file $E --cache-policy observed-cache --max-turns 4 --max-budget-usd 1 --timeout-seconds 600
```

확인(각 run의 `turns/0001/result.json`): `status: ok`·`usage_complete: true`·`session_id_confirmed: true`·`incomplete: []`, 1·2번 `transcript_models`가 `claude-opus-5-5`, 3번 `claude-fable-5-2`. 3번은 `stdout.jsonl`의 최종 응답이 `HOOK_BLOCKED: [gate-guard] …`이고 `workspace/src/main/kotlin/lab/payment/Probe.kt`가 없어야 한다(`HOOK_PASSED`나 파일 생성은 실패 — 메인에 보고). 3번 훅 스모크는 리뷰어를 부르지 않으므로 `reviewer.log_absent`·`projects_absent`가 true, `reviewer_dir_rc`가 0이어야 한다(리뷰어 디렉터리를 읽지 못하면 `usage_complete=false`로 남는다 — 그대로 보고). 한 조건이라도 기준을 못 채우면 동결하지 않는다. 같은 run id로는 다시 `init`할 수 없으므로(덮어쓰기 거부) 다시 하는 조건은 새 id(예 `smoke-v3r2-arm3`)로 하고, 동결의 `--smoke-run`에도 그 id를 준다.

**3번 리뷰어 스모크(명세 §11.5 — 동결 전 필수).** 리뷰어 인증과 리뷰어 역할 사용량 수집을 동결 전에 실제 호출 1회로 확인한다. 3번 run을 하나 더 만들고 PM이 `fable-review`를 한 번 부르게 한다(프롬프트 `smoke-3-arm3-reviewer-prompt.txt`, 9월 파일과 같은 폴더의 새 파일). 리뷰어 호출이 몇 분 걸릴 수 있어 `--timeout-seconds 1800`, `--max-turns 4`:

```bash
python3 $L init --run-id smoke-v3r3-arm3-reviewer --arm 3 --core 1 --harness payment-settlement/claude/harness-source
python3 $L turn --run-id smoke-v3r3-arm3-reviewer --prompt $P/smoke-3-arm3-reviewer-prompt.txt --env-file $E --cache-policy observed-cache --max-turns 4 --max-budget-usd 1 --timeout-seconds 1800
```

확인(`turns/0001/result.json`): `status: ok`·`usage_complete: true`·`incomplete: []`, `roles`에 `reviewer`, `reviewer.calls_succeeded_this_turn` ≥ 1·`reviewer.event_count` ≥ 1·`reviewer_usage_pending: false`, `reviewer.cost_usd`가 숫자, 최종 응답 `REVIEWER_OK: …`. `stderr.txt`에 `[lab-arm3-start] reviewer helper ready`. 이 조건을 채운 3번 스모크 턴이 하나도 없으면 동결의 `usage_collector_verified`는 false다(`freeze_readiness.reviewer_smoke_turn`, readiness의 `evidence.smoke_runs.<id>.reviewer_smoke_turns`). `REVIEWER_FAILED`·`Not logged in`·exit 95/97이면 동결하지 않고 메인에 보고한다. 이미지가 바뀐 뒤에는 세 조건 스모크를 모두 새 이미지로 다시 한다(readiness의 `evidence.smoke_runs.<id>.image_ids`가 동결하는 runner id와 같은지 확인 — 동결 도구는 이 값을 기록만 하고 막지는 않는다).

### 증거 내보내기 (진행자, 무과금)

`--attest`의 근거 파일은 저장소 안에 있어야 하는데(`freeze_readiness.attested`) `.runs/`는 추적되지 않는다. 진행자는 `discard` **전에** 3번 스모크의 증거를 `docs/results/setup/v3/`로 내보낸다:

```bash
python3 payment-settlement/claude/scripts/export_setup_evidence.py --run-id smoke-v3-arm3 --env-file ~/.config/payment-lab/claude.env
```

내보내는 파일: `<run>-manifest.json`(init self-test 보고 — `gate_l1_write_exit=2`·`gate_docs_write_exit=0`·`git_guard_push=ask`·`harness_hash_match=1`·`reviewer_on_path=1`), 턴마다 `<run>-turn-NNNN-invocation.json`(호출 전 훅 self-test `hook_selftest_before_call`)·`-result.json`·`-stdout.jsonl`(게이트 훅이 Write를 막은 그 턴의 CLI 원본 스트림). 마스킹 보장: 실행기는 턴이 끝날 때마다 run 디렉터리 전체에서 토큰 값을 치환하고(`launch.scrub`), 그 뒤 `result.json`을 확정한다. 도구는 `ok`로 확정된 턴(`result.json`의 `status`가 `ok`)만, `SECRET-LEAK.txt`가 없는 run만 받고(턴이 하나라도 `ok`가 아니면 그 run은 거부), 쓰기 전에 모든 파일을 `--env-file`의 토큰 값과 하네스 시크릿 가드(`harness-source/hooks/codex-scan.sh`)와 같은 범위의 자격증명 패턴(`sk-`·`gh?_`·`github_pat_`·`AKIA`/`ASIA`·`xox?-`·`AIza`·JWT·`PRIVATE KEY` PEM·password/token/secret 할당) + `Authorization: Bearer`로 검사해 한 건이라도 걸리면 아무것도 쓰지 않고 파일과 패턴 이름만(값은 출력하지 않음) 알린다. 걸리면 허용 목록을 넣지 말고 메인에 보고한다. 이미 있는 파일은 덮지 않는다. 마스킹 대상은 토큰 값뿐이라 호스트 절대경로(manifest의 `workspace`·`harness`, invocation의 `env_file` 경로)는 남는다 — 비밀이 아니다.

판정기 자체 테스트 출력(무과금, 같은 폴더, 덮어쓰기 금지 `set -C`):

```bash
( set -C; O=payment-settlement/claude/docs/results/setup/v3/evaluation-unittest.txt; : > $O || exit 1
  python3 -m unittest discover -s payment-settlement/evaluation -p 'test_*.py' -v >> $O 2>&1; echo "exit=$?" >> $O )
```

`exit=0`과 `OK`가 아니면 메인에 보고하고 멈춘다. 내보낸 파일은 진행자가 일반 docs 커밋으로 남긴다(근거 해시가 커밋된 바이트와 같아야 한다).

### 증명 파일 (메인이 쓴다)

`harness_adapter_verified`·`acceptance_suite_verified`는 실행기 산출물로 도출되지 않고 `freeze_readiness.py --attest <file>`로만 true가 된다(`attested`: 이 두 플래그만 받고, true에는 저장소 안 근거 파일이 1개 이상 필요하며 근거는 sha256으로 readiness에 들어간다). **증명 파일은 감독하는 메인 세션이 위 증거를 직접 읽고 쓴다 — 진행자는 쓰지 않는다.** 위치: `payment-settlement/claude/docs/results/setup/v3/attestation.json`(근거 경로는 저장소 루트 기준).

```json
{
  "harness_adapter_verified": {"value": true, "evidence": [
    "payment-settlement/claude/docs/results/setup/v3/smoke-v3-arm3-manifest.json",
    "payment-settlement/claude/docs/results/setup/v3/smoke-v3-arm3-turn-0001-invocation.json",
    "payment-settlement/claude/docs/results/setup/v3/smoke-v3-arm3-turn-0001-result.json",
    "payment-settlement/claude/docs/results/setup/v3/smoke-v3-arm3-turn-0001-stdout.jsonl"]},
  "acceptance_suite_verified": {"value": true, "evidence": [
    "payment-settlement/claude/docs/results/setup/v3/evaluation-unittest.txt",
    "payment-settlement/codex/docs/pilots/detailed-01/acceptance-issues.md"]}
}
```

- `harness_adapter_verified`의 근거 = 3번 스모크: init self-test 보고와 게이트 훅이 쓰기를 막은 턴(위 내보낸 파일). 3번을 새 id로 다시 했으면 그 id의 파일을 쓴다.
- `acceptance_suite_verified`의 근거 = 판정기 자체 테스트 출력 + 9월 파일럿에서 판정기가 실제 결함을 잡은 기록(`codex/docs/pilots/detailed-01/acceptance-issues.md`, 읽기만).
- 메인이 근거를 읽고 확인하지 못한 플래그는 `"value": false`로 쓰거나 빼며, 근거 없는 true는 동결이 거부한다.

### 동결 명령

```bash
python3 payment-settlement/claude/scripts/freeze_readiness.py \
  --smoke-run smoke-v3-arm1 --smoke-run smoke-v3-arm2 --smoke-run smoke-v3-arm3 \
  --attest payment-settlement/claude/docs/results/setup/v3/attestation.json
```

(다시 한 조건은 그 새 id로. 3번 리뷰어 스모크 run도 `--smoke-run`으로 준다.) 동결 입력(`FROZEN`)에 미커밋 변경이 있으면 거부된다. 스모크 run의 `discard`는 내보내기 뒤에 한다 — 동결은 run 디렉터리의 파일만 읽으므로 volume이 없어도 된다.

### 본 실험 전 동결 대체 (사용자 결정 2026-11-18)

동결 입력(실행기·스크립트 등)이나 이미지가 바뀌었는데 **본 실험 run이 하나도 시작되지 않았으면** 프로토콜 이름을 유지한 채 동결을 대체할 수 있다. "시작되지 않음"은 플래그가 아니라 `start_cycle.py`가 쓰는 증거에서 도출한다(`freeze_readiness.experiment_started`): 30개 `docs/experiments/<arm>/core<N>/run.json`이 모두 `NOT_STARTED`, `.runs/claude-armA-coreNN` 폴더 없음, `cycles.json`의 사이클이 모두 `NOT_STARTED`, `experiment: claude-arm…` 커밋 없음(읽지 못한 기록은 "시작됨"으로 본다).

```bash
python3 payment-settlement/claude/scripts/freeze_readiness.py --supersede-before-runs --reason "<대체 사유>" \
  --smoke-run <arm1 id> --smoke-run <arm2 id> --smoke-run <arm3 훅 id> --smoke-run <arm3 리뷰어 id> \
  --attest payment-settlement/claude/docs/results/setup/v3/attestation.json
```

- 이전 `readiness.json`·`frozen-inputs.json`은 지우지 않고 옆에 `readiness.superseded-<시각>.json`·`frozen-inputs.superseded-<시각>.json`으로 보관된다. 새 readiness의 `supersedes`에 사유·시각·이전 `contract_sha256`·이전 이미지 id·보관 파일과 그 sha256·바뀐 입력 목록이 남는다.
- `pilot_passed`는 false로 돌아간다(`--pilot-run`과 함께 쓰면 거부). 파일럿은 새 동결로 다시 한다.
- `--reason`이 없거나 `--replace`와 함께면 거부. 본 실험 run이 하나라도 시작됐으면 거부되고 종전 규칙(동결 뒤 변경 = 프로토콜 버전 변경·전 조건 재실행)이 적용된다.
- 파일럿 뒤 입력이 그대로일 때의 `--replace --pilot-run`(플래그만 다시 도출)은 그대로다.
- 증명 파일의 근거(`attestation.json`의 스모크 내보내기)는 새 스모크 run의 파일로 다시 쓴다(메인).

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
- [ ] v3-claude 인증 스모크(새 토큰, **arm 1·2·3 각각** 1턴 — 명령은 위 "v3 인증 스모크와 동결") → 증거 내보내기 → 메인이 증명 파일 작성 → `scripts/freeze_readiness.py --smoke-run <arm1> --smoke-run <arm2> --smoke-run <arm3> --attest payment-settlement/claude/docs/results/setup/v3/attestation.json`로 동결(대리 사용자 모델은 합의값 `claude-opus-5-5`로 기록된다)(세 조건이 모두 없으면 동결 거부, 플래그는 스모크 산출물과 해시된 증명에서만 도출)
- [ ] 파일럿 개시 전 게이트(파일럿 init 직전, 통과해야만 init): `python3 payment-settlement/claude/scripts/preflight.py --stage pilot --readiness payment-settlement/claude/docs/results/readiness.json --core 1` → `"ready": true`, exit 0. 이 명령은 readiness를 세 조건 각각의 run 기록(`docs/experiments/<arm>/core1/run.json` — 조건·모델·provider)과 합쳐(사이클 게이트 `start_cycle.gate`와 같은 함수 `preflight.merge`, 조건의 관측 모델 `actual_models[<arm>]`이 `actual_model`이 된다) 조건마다 검사한다. `pilot_passed`만 면제되고 나머지 플래그는 전부 리터럴 true여야 한다(가짜 true 금지 — 프로토콜·상한 포함). exit 2면 `blockers`를 메인에 보고하고 파일럿을 시작하지 않는다. (`--arm 3`으로 한 조건만 볼 수 있으나 개시 게이트는 세 조건 전부로 한다.) 이 게이트는 `init --pilot`에 내장돼 있지 않다 — 진행자가 반드시 먼저 실행하고 출력을 파일럿 기록에 남긴다
- [ ] 파일럿 init + Compose: 본 실험(`start_cycle.py`)과 같은 순서·같은 방식이다 — run마다 자기 Compose project를 만들고 그 네트워크로 init한다(`--network`가 없으면 실행기가 `DB_URL`·`BANK_BASE_URL`을 넘기지 않아 PM이 DB·은행에 닿지 못한다). 파일럿 01에서 동작한 명령(NN·포트만 바꾼다, 포트는 `ss -ltn`으로 비어 있는지 확인):
  ```bash
  N=02; PROJECT=payment-claude-pilot-$N
  python3 payment-settlement/claude/runner/launch.py init --pilot --run-id claude-pilot-$N --arm 3 --core 1 \
    --harness payment-settlement/claude/harness-source --readiness payment-settlement/claude/docs/results/readiness.json \
    --network ${PROJECT}_default
  ARM=.runs/claude-pilot-$N CORE=workspace APP_PORT=38001 DB_PORT=39001 BANK_PORT=40001 \
    docker compose -p $PROJECT -f payment-settlement/claude/compose.yaml up -d --wait db bank
  python3 payment-settlement/claude/scripts/lab.py prompt --arm 3 > payment-settlement/claude/docs/pilots/claude-pilot-$N/initial-prompt.txt
  ```
  (명령은 저장소 루트에서, `ARM`은 `start_cycle.py`처럼 `payment-settlement/claude` 기준 상대 경로 `.runs/<run id>`.) 파일럿 run id는 `claude-pilot-NN`(그 밖의 id에 `--pilot`, 또는 `--pilot` 없이 이 접두는 거부), 기록은 `docs/pilots/<run id>/`. readiness가 있으면 `--image`를 생략해도 동결 digest(`image_digests.runner`)를 쓰고 다른 이미지는 거부한다. manifest의 `image`·`image_frozen=true`·`readiness`·`network`로 확인한다
- [ ] 파일럿 턴: 첫 턴 `turn --run-id claude-pilot-NN --prompt <initial-prompt.txt> --env-file … --cache-policy observed-cache`(`--timeout-seconds`는 주지 않는다 — 기본 28,800초를 남은 시간으로 줄임). PM이 질문·승인 요청으로 끝나면 그 응답 전문을 `questions-NNN.txt`로 저장해 `consult_contract.py`로 보내고, `consults/NNNN/response.txt`를 **바이트 그대로** `--resume --kind answer --prompt`로 보낸다. 매 턴 뒤 `normalize` + `capture_turn.py --turn N --analysis <메모>`
- [ ] 실행 환경 때문에 멈춘 파일럿의 종료(예: `claude-pilot-01`, 명세 §11.8): 판정 없이 `finish_run.py --run-id claude-pilot-NN --invalid-infra "<사유>" --evidence <기록 폴더 기준 근거 파일> [--evidence …]`(근거 예: `reviewer-failure/reviewer-calls.log`·`reviewer-failure/<session>.json`·`conductor-journal.md`). `--invalid-infra`에는 `verdict.json`이 없어도 된다(있으면 판정표 규칙대로 검사되고, 20개 모두 `PASS`면 거부). 근거 파일은 기록 폴더 안에 있어야 하고, 남은 턴은 먼저 `capture_turn.py`로 기록한다. 그 뒤 `docker compose -p payment-claude-pilot-NN -f payment-settlement/claude/compose.yaml down -v`, `launch.py discard --run-id claude-pilot-NN`, 기록 폴더는 남기고 일반 docs 커밋. (파일럿 01은 리뷰어 도우미 이전 manifest라 `reviewer_uid`가 없고, `discard`는 리뷰어 volume을 종전처럼 이미지 사용자로 나열한다)
- [ ] 파일럿 뒤: `freeze_readiness.py ... --pilot-run <id> --replace`로 `pilot_passed`를 파일럿 산출물에서 도출(파일럿 manifest가 init 때 기록한 `contract_sha256`·`image`가 지금 쓰는 동결값과 같을 때만 통과). `--replace`는 플래그만 다시 도출한다 — 동결 입력(`frozen-inputs.json`)이나 이미지 id가 바뀌었으면 거부된다(프로토콜 버전 변경·전 조건 재실행 대상) → 사이클 개시 게이트(`start_cycle.py`, 항상 `--stage cycle` 기준)
