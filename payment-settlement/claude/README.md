# Claude 조건 — 결제·정산 API 실험 (동일 프로토콜, Claude Code 실행)

**프로토콜 `payment-settlement-v3-claude`**(2026-11-17, Claude 전용 — Codex는 쓰지 않는다)로 세 조건을 Claude Code에서 실행하기 위한 폴더다. 계약(`../docs/`)·판정기(`../evaluation/`)·fixture(`../infra/`)는 공유한다. 조건당 10회, 총 30회 독립 시행이며 현재 30개 시행 모두 `NOT_STARTED`다. run당 상한은 input+output 20,000,000 토큰·8시간·피드백 30회다(Codex v2와 같은 상한).

| 조건 | 최초 입력 | 모델(요청) | 서브에이전트 | 하네스 |
|---|---|---|---|---|
| 1 — minimal | `prompts/common.md` + `1-minimal.md` (Codex와 바이트 동일) | `claude-opus-5-5` · high | 금지(도구 allow-list `--tools`, self-test가 부분집합 단정) | 없음(빈 config volume) |
| 2 — detailed | 동일 + `../docs/requirements.md`·`resources.md`·`slo.md` (Codex와 바이트 동일) | `claude-opus-5-5` · high | 금지 | 없음 |
| 3 — harness-auto-native | 2와 동일 + `prompts/3-harness.md`(Claude 전용) | PM `claude-fable-5-2` · high, 구현·테스트·리뷰 워커 `claude-opus-5-5` · high, 탐색 워커(explorer)는 하네스 정의(Sonnet), 두 번째 리뷰어 `claude-fable-5-2`, 대리 사용자 `claude-opus-5-5` | `CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-5-5`·`CLAUDE_CODE_SUBAGENT_EFFORT=high` | **하네스 v4.3(7b0ef7b) 원본 훅 그대로 설치·강제**. 도구는 1·2번 허용 목록 + `Task`(서브에이전트) |

웹 도구(`WebSearch`·`WebFetch`)는 세 조건 모두 허용하고 사용을 기록한다.

인증은 구독 OAuth 장기 토큰(`claude setup-token`)만 사용하고 API 키는 쓰지 않는다. 따라서 `billed_cost=null`이며 `total_cost_usd`는 CLI 추정치다. Codex 판과 토크나이저가 다르므로 토큰 절대값이 아니라 비용·완료율·품질을 병기한다.

## 배치 (codex/ 미러)

- `{1,2,3}/core1..10`: `template/`과 바이트 동일한 starter (`scripts/verify_setup.py`가 30개 검사)
- `template/`, `compose.yaml`, `prompts/{common,1-minimal,feedback,slo}.md`, `scripts/{lab,commit_run}.py`: `../codex/`와 바이트 동일 복사본
- `harness-source/`: 하네스 v4.3 commit 7b0ef7b의 `git archive` 사본(`SOURCE.json`). Codex 쪽 사본(1754f16)과 의도적으로 다르다
- `prompts/3-harness.md`: Claude 전용(네이티브 훅·Fable PM·구현·테스트·리뷰 워커 Opus·`fable-review`·컨테이너 환경 사실)
- `runner/`: Claude Code 2.1.326 격리 실행기 ([runner/README.md](runner/README.md))
- `scripts/{start_cycle,freeze_readiness,consult_contract,capture_turn,load_check}.py`: Codex 판 스크립트의 Claude 실행기 이식본(`test_scripts.py`)
- `docs/runbook.md`, `docs/isolation-and-measurement.md`: Claude 판 절차와 측정 차이
- `docs/auth-token.md`: 발급한 인증 토큰이 무엇이고 어떻게 보관·교체하는지
- `docs/issues-log.md`: 설계·준비·실행 중 발생한 문제와 해결 기록(발생 시점에 추가)
- `docs/experiments/<arm>/core<N>/`: 시행별 run.json·events.jsonl·transcript.md (공유 `../recording/record.py`)
- `docs/results/`: schedule·manifest 예시·setup 증거

## 빠른 시작 (저장소 루트 기준)

```bash
python3 payment-settlement/claude/scripts/prepare.py        # 30 starter·기록 원장·schedule (멱등)
python3 payment-settlement/claude/scripts/verify_setup.py   # codex/ 와의 바이트 동일성 + 프롬프트 SHA + 하네스 commit 일치
docker build -t payment-claude-runner:2.1.326 payment-settlement/claude/runner
python3 -m unittest discover -s payment-settlement/claude/runner
python3 -m unittest discover -s payment-settlement/claude/scripts
python3 payment-settlement/claude/scripts/preflight.py payment-settlement/claude/docs/experiments/1/core1/run.json   # exit 2 = 미확인 항목 나열(정상)
```

Docker fixture(DB seed·fake-bank·k6 smoke)는 `compose.yaml`이 Codex 판과 바이트 동일하므로 [Codex setup 증거](../codex/docs/results/setup/verification.json)를 인용하며 여기서 다시 돌리지 않는다(`docs/results/setup/verification.json`의 `docker_fixture.fixture_verified_here=false`).

## 원본 하네스·Codex 판과의 차이 (전부 기록 대상)

- 3번은 원본 Claude 훅이 컨테이너 안에서 실제로 강제된다. 실행기가 init과 매 턴 직전에 무과금 self-test(훅 존재·gate-guard 차단 exit 2·docs 통과 exit 0)를 단정한다.
- 모든 arm의 workspace에 `git init` + 초기 커밋 1개가 들어간다(훅의 docs 판정·git-guard가 git toplevel을 요구). Codex 판은 repo 없이 돈다.
- 사용량은 `codex exec --json`의 누적 counter 차분이 아니라 Claude Code transcript의 **요청(requestId) 단위 usage**를 그대로 쓴다. 서브에이전트(sidechain)는 `worker` 역할로 분리 집계한다.
- **원본 하네스와 다른 점**: 3번 듀얼 리뷰의 두 번째 리뷰어는 codex가 아니라 `fable-review`(새 `claude -p` 세션, `claude-fable-5-2`, 읽기 전용 도구, 별도 config dir — `runner/fable-review.sh`)다. 실제 하네스의 "다른 모델 계열의 독립 신호"가 같은 공급자 모델로 바뀐 것이므로 결과에 명시한다. codex는 이미지에 없다.
- 컨테이너 settings는 실배포 `~/.claude/settings.json`의 hooks 섹션 추출본이다(`runner/harness-settings.provenance.json`, v4.3에서 재확인 — 변경 없음). 실배포의 모델 키는 가져오지 않는다.
- 웹 도구는 세 조건 모두 허용한다(D5 결정 2026-11-17). `result.web_requests`와 manifest `observed_tools`에 남긴다.

2026-09-23 스모크(v1: 구독 토큰 인증·resume·3번 훅 강제·컨테이너 안 codex)는 통과했다(`docs/results/setup/claude/summary.json`). v3-claude의 무과금 self-test 증거는 `docs/results/setup/v3/`에 있고, 새 모델 ID로의 인증 스모크는 토큰 재발급 뒤에 한다. 실제 모델 실행은 자동 시작하지 않는다. 개시 조건은 [runbook의 체크리스트](docs/runbook.md#실험-개시-체크리스트)를 따른다.
