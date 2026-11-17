# Claude 판 실험 결과

현재 30개 시행 모두 `NOT_STARTED`다. 준비 세션의 빌드·self-test·스모크는 실험 결과가 아니다.

- `schedule.json`: `../../codex/docs/results/schedule.json`에서 파생한 실행 순서(order·cycle·arm·core·policy 동일, run_id만 `claude-` 접두). `scripts/prepare.py`가 재생성하고 `verify_setup.py`가 동일성을 검사한다
- `run-manifest.example.json`: Claude 판 manifest 형식(provider `claude-code`, 역할별 모델, `hook_enforcement_verified` 등)
- `setup/verification.json`: starter 동일성·Codex 판과의 입력 바이트 동일성·프롬프트 SHA. fixture 검증은 Codex 증거를 SHA로 인용
- `setup/claude/`: 2026-09-23 유료 스모크 4턴의 원문(stream-json·events·result·manifest)과 `summary.json`(해시 포함). setup 비용이며 실험 결과가 아니다
- `../experiments/<arm>/core<N>/`: run.json·events.jsonl·transcript.md

```bash
python3 payment-settlement/claude/scripts/preflight.py payment-settlement/claude/docs/experiments/3/core1/run.json
```

exit 2로 미확인 항목을 나열하는 것이 현재 정상이다. 플래그는 literal `true`만 통과하며 문자열이나 다른 값으로 채워 넘기지 않는다.
