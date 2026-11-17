# Claude 조건 — 결제·정산 API 실험 (동일 프로토콜, Claude Code 실행)

`../codex/`와 **같은 프로토콜(`payment-settlement-v1`)·같은 계약(`../docs/`)·같은 판정기(`../evaluation/`)·같은 fixture(`../infra/`)**로 세 조건을 Claude Code로 실행하기 위한 폴더다. 조건당 10회, 총 30회 독립 시행이며 현재 30개 시행 모두 `NOT_STARTED`다. 판정기(C01..C20 전체)는 Codex 쪽에서 제작 중이며 완성본을 그대로 공유한다.

| 조건 | 최초 입력 | 모델(요청) | 서브에이전트 | 하네스 |
|---|---|---|---|---|
| 1 — minimal | `prompts/common.md` + `1-minimal.md` (Codex와 바이트 동일) | Opus · high | 금지(도구 allow-list `--tools`, self-test가 부분집합 단정) | 없음(빈 config volume) |
| 2 — detailed | 동일 + `../docs/requirements.md`·`resources.md`·`slo.md` (Codex와 바이트 동일) | Opus · high | 금지 | 없음 |
| 3 — harness-auto-native | 2와 동일 + `prompts/3-harness.md`(Claude 전용) | PM Fable · high, 워커 Opus · high | `CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-5` | **원본 훅 그대로 설치·강제**(Codex 판의 절차 이식과 다름) |

인증은 구독 OAuth 장기 토큰(`claude setup-token`)만 사용하고 API 키는 쓰지 않는다. 따라서 `billed_cost=null`이며 `total_cost_usd`는 CLI 추정치다. Codex 판과 토크나이저가 다르므로 토큰 절대값이 아니라 비용·완료율·품질을 병기한다.

## 배치 (codex/ 미러)

- `{1,2,3}/core1..10`: `template/`과 바이트 동일한 starter (`scripts/verify_setup.py`가 30개 검사)
- `template/`, `compose.yaml`, `prompts/{common,1-minimal,feedback,slo}.md`, `harness-source/`, `scripts/{lab,commit_run}.py`: `../codex/`와 바이트 동일 복사본
- `prompts/3-harness.md`: Claude 전용(네이티브 훅·Fable PM·Opus 워커·컨테이너 환경 사실)
- `runner/`: Claude Code 2.1.280 격리 실행기 ([runner/README.md](runner/README.md))
- `docs/runbook.md`, `docs/isolation-and-measurement.md`: Claude 판 절차와 측정 차이
- `docs/auth-token.md`: 발급한 인증 토큰이 무엇이고 어떻게 보관·교체하는지
- `docs/issues-log.md`: 설계·준비·실행 중 발생한 문제와 해결 기록(발생 시점에 추가)
- `docs/experiments/<arm>/core<N>/`: 시행별 run.json·events.jsonl·transcript.md (공유 `../recording/record.py`)
- `docs/results/`: schedule·manifest 예시·setup 증거

## 빠른 시작 (저장소 루트 기준)

```bash
python3 payment-settlement/claude/scripts/prepare.py        # 30 starter·기록 원장·schedule (멱등)
python3 payment-settlement/claude/scripts/verify_setup.py   # codex/ 와의 바이트 동일성 + 프롬프트 SHA
docker build -t payment-claude-runner:2.1.280 payment-settlement/claude/runner
python3 -m unittest discover -s payment-settlement/claude/runner
python3 payment-settlement/claude/scripts/preflight.py payment-settlement/claude/docs/experiments/1/core1/run.json   # exit 2 = 미확인 항목 나열(정상)
```

Docker fixture(DB seed·fake-bank·k6 smoke)는 `compose.yaml`이 Codex 판과 바이트 동일하므로 [Codex setup 증거](../codex/docs/results/setup/verification.json)를 인용하며 여기서 다시 돌리지 않는다(`docs/results/setup/verification.json`의 `docker_fixture.fixture_verified_here=false`).

## Codex 판과의 차이 (전부 기록 대상)

- 3번은 원본 Claude 훅이 컨테이너 안에서 실제로 강제된다. 실행기가 init과 매 턴 직전에 무과금 self-test(훅 존재·gate-guard 차단 exit 2·docs 통과 exit 0)를 단정한다.
- 모든 arm의 workspace에 `git init` + 초기 커밋 1개가 들어간다(훅의 docs 판정·git-guard가 git toplevel을 요구). Codex 판은 repo 없이 돈다.
- 사용량은 `codex exec --json`의 누적 counter 차분이 아니라 Claude Code transcript의 **요청(requestId) 단위 usage**를 그대로 쓴다. 서브에이전트(sidechain)는 `worker` 역할로 분리 집계한다.
- 3번 리뷰의 codex 호출은 원본 playbook대로 허용하되, 컨테이너의 `codex`는 `--ephemeral`만 제거하는 래퍼다(리뷰어 사용량 관측 목적, `runner/codex-wrapper.sh`).
- 컨테이너 settings는 실배포 `~/.claude/settings.json`의 hooks 섹션 추출본이다(`runner/harness-settings.provenance.json`). 실배포의 `[1m]` 접미는 가져오지 않았고, 스모크에서 두 모델 모두 contextWindow 1M으로 보고되어 추가 결정이 필요 없다.
- Claude arm은 `WebSearch`/`WebFetch`가 활성이고 Codex arm에는 없다. 끌지(패리티) 열고 기록할지는 개시 전 결정 항목(D5)이며, 어느 쪽이든 `result.web_requests`와 manifest `observed_tools`에 실사용을 남긴다.

2026-09-23 스모크(구독 토큰 인증·resume·3번 훅 강제·컨테이너 안 codex)는 통과했다(`docs/results/setup/claude/summary.json`). 실제 모델 실행은 자동 시작하지 않는다. 개시 조건은 [runbook의 체크리스트](docs/runbook.md#실험-개시-체크리스트)를 따른다.
