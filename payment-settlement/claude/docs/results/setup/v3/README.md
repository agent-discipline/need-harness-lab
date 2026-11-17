# v3-claude 준비 증거 (무과금, 2026-11-17)

- `selftest-arm1-manifest.json`, `selftest-arm3-manifest.json`: `launch.py init`의 무네트워크·무토큰 self-test 결과(이미지 `payment-claude-runner:2.1.326`, 하네스 v4.3 `7b0ef7b`). 두 run은 증거 수집 뒤 `discard`로 volume을 지웠다. 경로는 익명화했다.
- arm 3: settings SHA 일치, 훅 8개 존재·문법, 시딩 해시(core.md·HISTORY.md·hooks·playbooks·templates·agents) 일치, 추가 파일 없음, gate-guard L1 차단 exit 2 / docs 통과 exit 0, Bash 양성 3훅 exit 0, 상태파일 tee exit 2, push → ask, `fable-review` 존재, WebSearch/WebFetch 노출.
- arm 1: allow-list 부분집합, fan-out 도구 부재, WebSearch/WebFetch 노출.
- 하네스 자체 테스트: `harness-source/hooks/tests/run.sh`를 같은 이미지(`--network none --cap-drop=ALL`)에서 실행 → `261 passed, 0 failed`.
