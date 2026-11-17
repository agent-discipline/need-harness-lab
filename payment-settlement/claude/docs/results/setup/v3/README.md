# v3-claude 준비 증거 (무과금, 2026-11-17)

- `selftest-arm1-manifest.json`, `selftest-arm3-manifest.json`: `launch.py init`의 무네트워크·무토큰 self-test 결과(이미지 `payment-claude-runner:2.1.326`, 하네스 v4.3 `7b0ef7b`). 두 run은 증거 수집 뒤 `discard`로 volume을 지웠다(`discard` 기록 complete=true). `workspace`는 `<scratch>/…`로, `source`는 저장소 상대 경로로 바꿨고, 그 밖의 필드(arm 3의 `harness` 등)에는 기록 당시 호스트 절대경로가 그대로 남아 있다.
- 2026-11-17 재실행(3번 조건 허용 목록 적용 뒤): arm 1 init tools = `Bash,Edit,Glob,Grep,NotebookEdit,Read,ToolSearch,WebFetch,WebSearch,Write`, arm 3 = 같은 목록 + `Task`.
- arm 3: 관측 tools ⊆ 허용 목록, `Task` 존재, 1·2번 목록 + 서브에이전트 표면 밖 도구 없음, settings SHA 일치, 훅 8개 존재·문법, 시딩 해시(core.md·HISTORY.md·hooks·playbooks·templates·agents) 일치, 추가 파일 없음, gate-guard L1 차단 exit 2 / docs 통과 exit 0, Bash 양성 3훅 exit 0, 상태파일 tee exit 2, push → ask, `fable-review` 존재, WebSearch/WebFetch 노출.
- arm 1: allow-list 부분집합, fan-out 도구 부재, WebSearch/WebFetch 노출.
- 하네스 자체 테스트: `harness-source/hooks/tests/run.sh`를 같은 이미지(`--network none --cap-drop=ALL`)에서 실행 → `261 passed, 0 failed`.
