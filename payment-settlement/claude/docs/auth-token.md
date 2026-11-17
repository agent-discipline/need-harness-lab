# 인증 토큰 — 무엇을 발급했고 어떻게 다루는가

## 발급한 것

`claude setup-token`이 만든 **구독 계정용 장기 OAuth 토큰**(접두 `sk-ant-oat01-`, 유효 1년)이다. Claude Code CLI가 구독 자격으로 API에 접속하도록 인증하는 계정 단위 자격증명이며, 환경변수 `CLAUDE_CODE_OAUTH_TOKEN`으로 주입한다. API 키(`sk-ant-api…`)가 아니므로 종량 청구가 아니라 구독 한도를 소비한다(`billed_cost=null`).

## 사용량 측정과의 관계

이 토큰은 **사용량을 조회하는 수단이 아니다.** 세션별·역할별 사용량은 실행기가 따로 관측한다.

- `claude -p --output-format stream-json`의 최종 `result` 이벤트: `usage`(input / cache_creation / cache_read / output), `modelUsage`, `total_cost_usd`(CLI 추정치), `num_turns`
- `CLAUDE_CONFIG_DIR/projects/**/*.jsonl` transcript: 요청(requestId) 단위 `message.usage`·`model`, 서브에이전트 여부(`isSidechain`)

run마다 새 config volume·새 session id를 쓰므로 한 토큰으로 30회를 돌려도 세션은 섞이지 않는다. 같은 계정이라는 사실과 같은 대화라는 사실은 다르다(격리 문서 참조). 구독 UI의 사용량 퍼센트는 요청별 청구 근거가 아니며 정량 비교에 쓰지 않는다.

## 보관 규칙

- 저장 위치: repo 밖 `~/.config/payment-lab/claude.env`, 내용은 `CLAUDE_CODE_OAUTH_TOKEN=<토큰>` 한 줄, 권한 600. 실행기는 이 경로가 repo 안이거나 권한이 느슨하거나 다른 키가 섞이면 거부한다.
- 실행기는 `docker --env-file`로만 전달하고 `invocation.json`에 경로만 남긴다. `docker inspect` 사본의 Env 값은 마스킹하고, 턴 종료 시 run 디렉터리 전체를 토큰 값으로 스캔해 히트가 있으면 실패로 끝낸다.
- 토큰 값을 채팅·이슈·커밋·로그에 붙여넣지 않는다. 2026-09-23 최초 발급본은 채팅으로 전달되어 세션 transcript에 남았으므로 **노출된 것으로 간주**한다. 스모크에는 사용하되, 본 실험 30회 개시 전에 터미널에서 새로 발급해 교체하고 이 문서에 교체 일자를 적는다.
- 만료(1년) 또는 교체 시 `claude.env`만 갱신하면 되고 저장소 파일은 바뀌지 않는다.

| 일자 | 사건 |
|---|---|
| 2026-09-23 | 최초 발급(setup-token). 채팅 경유 전달 → 노출 간주, 스모크 전용 |
| 2026-11-18 | 재발급·교체(사용자가 별도 터미널에서 `setup-token` → `claude.env` 덮어쓰기). 값은 채팅을 거치지 않음. 확인: 권한 600 · 1줄 · 키 1개 · 접두 일치(값 미출력). 이전 발급본은 사용하지 않는다 |
