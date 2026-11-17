# Claude Code 격리 실행기

`init`은 starter를 복사하고 무과금 self-test를 통과해야 run을 만든다. **`turn`은 실제 유료(구독) 호출을 시작한다.** `normalize`는 보존된 이벤트를 공유 adapter로 정규화한다.

## 이미지

`node:22-bookworm-slim` + Temurin 21 JDK + `@anthropic-ai/claude-code@2.1.326` + 훅 의존(jq·flock·git·awk·realpath·python3). `USER node`(uid 1000), `HOME=/home/node`, `CLAUDE_CONFIG_DIR=/home/node/.claude`, 자동 업데이트 비활성. codex는 없다. `/usr/local/bin/fable-review`(`fable-review.sh`)는 3번의 두 번째 리뷰어다: stdin 프롬프트 → 새 `claude -p` 세션(`LAB_REVIEWER_MODEL`, effort high, `--tools Read,Grep,Glob`, `CLAUDE_CONFIG_DIR=$LAB_REVIEWER_CONFIG_DIR`) → stdout 리뷰. 호출마다 시작 전에 `reviewer-calls.log`에 호출·session id를 남기고, 기록 실패나 리뷰어 미설정(1·2번)이면 실행을 거부한다.

## 명령

```bash
python3 payment-settlement/claude/runner/launch.py init --run-id <id> --arm <1|2|3> --core <1..10> [--harness payment-settlement/claude/harness-source] [--network <compose_default>]
python3 payment-settlement/claude/runner/launch.py turn --run-id <id> --prompt <file> --env-file <0600 file outside repo> [--resume --kind feedback|answer|recovery] [--max-turns N] [--max-budget-usd X] [--timeout-seconds 7200] --cache-policy observed-cache [--dry-run]
python3 payment-settlement/claude/runner/launch.py normalize --run-id <id>
python3 -m unittest discover -s payment-settlement/claude/runner -v
```

- init: `.runs/<id>/`를 staging에서 조립해 원자적으로 rename한다(실패 시 잔여물 없음). workspace는 SKIP 집합(`.git`·build·experiment·에이전트 상태 등) 제외 복사 후 `git init` + 커밋 1개. manifest에 요청 모델·워커 모델·session id·volume 이름·시딩 해시·self-test 결과를 기록한다. 호스트 uid는 1000이어야 한다.
- self-test(무네트워크·무토큰 helper 컨테이너, 900초 상한): CLI 인자 수용 + init 이벤트의 tools 관측(모든 arm에 `WebSearch`·`WebFetch` 존재, 모든 arm이 자기 allow-list의 부분집합, 1·2번은 fan-out 도구 부재, 3번은 1·2번 목록 + `Task`이며 `Task` 존재), `/workspace` git toplevel 판독, 3번은 settings.json SHA 일치·참조 훅 전부 존재(8개 이상)·`bash -n`·시딩 파일(core.md·HISTORY.md·hooks·playbooks·templates·agents) sha256 재검증(검증기 실패도 실패)·추가 파일 부재, gate-guard에 합성 PreToolUse Write(L1 exit 2 / docs exit 0)와 Bash(양성 3훅 exit 0 / 상태파일 tee exit 2 / push → ask), `fable-review` 존재·`bash -n`. turn 직전(3번)에도 반복한다.
- turn: `docker create --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=512 --cpus=2 --memory=3g --env-file <파일> --env HOME --env CLAUDE_CONFIG_DIR` + workspace bind + config volume(+3번 리뷰어 volume `/home/node/.fable-review`와 `LAB_REVIEWER_MODEL`·`LAB_REVIEWER_CONFIG_DIR`) + `claude -p --verbose --output-format stream-json --model <m> --effort high --dangerously-skip-permissions --session-id|--resume <고정>` (+`--tools <allow-list>` — 3번은 1·2번 목록 + `Task`, 3번 `CLAUDE_CODE_SUBAGENT_MODEL`·`CLAUDE_CODE_SUBAGENT_EFFORT=high`). 재개 턴은 `--kind`(feedback·answer·recovery)를 선언해야 하고(없으면 호출 전 거부, `answer`는 3번만) 종류는 `invocation.json`의 `turn_kind`에 남는다. 피드백 상한 30은 성공한 `feedback` 전달(그 `recovery` 포함)만 센다(`docs/runbook.md` 상한 절). 호출 전 run 상한 게이트(`limit_gate`)를 지나고 timeout은 남은 시간으로 줄인다. prompt는 stdin. 종료 후 마스킹된 inspect·`projects/` transcript 사본·(3번) 리뷰어 호출 로그와 transcript(`reviewer-calls.log`·`reviewer-projects/`)를 복사하고 `events.jsonl`·`events.role-*.jsonl`·`result.json`을 만든 뒤 컨테이너만 제거한다. run 디렉터리 전체 토큰 스캔에서 히트가 나오면 실패.
- result.json 주요 필드: `result`(stream 최종 이벤트), `session_id_confirmed`, `request_ids`, `event_count`, `incomplete`, `transcript_stats`, `usage_complete`, `usage_cross_check`, `role_files`, `transcript_models`, `roles`, `config_volume`(파일 목록·credentials 여부), `cost`, `reviewer`(호출·비용), `observed_tools_and_effort`, `secret` 히트는 `SECRET-LEAK.txt`. run 디렉터리의 `ledger.json`이 전 역할 단일 장부, `censored.json`이 상한 도달 기록이다.

## 검증

단위테스트(`python3 -m unittest discover -s payment-settlement/claude/runner`, 실행 시점 개수는 출력의 `Ran N tests`가 정본): 마운트·환경변수·플래그 구성, arm 3 요구사항, 원자 init·덮어쓰기 거부·git init, 하네스 시딩 해시, env-file 검증(권한·단일 키·repo 밖), stream 파싱, requestId 사본 일치·역할·incomplete, inspect 마스킹·스캔, dry-run 무실행, 토큰 유출 시 실패. 실제 인증·resume·훅 발화·서브에이전트 모델 전파는 유료 스모크 증거(`docs/results/setup/claude/`)가 필요하다.
