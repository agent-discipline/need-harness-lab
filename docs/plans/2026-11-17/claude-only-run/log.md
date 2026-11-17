# log — claude-only-run

> 메인이 단일 writer. 워커 산출은 packet 원시각을 명시해 메인이 append한다.

## 타임라인

| 시각 | 사건 | 결과/결정 |
|------|------|----------|
| 2026-11-17 | 조사 워커(Opus×1, 읽기 전용) 보고 회수 | Claude 쪽 30 run NOT_STARTED · v1 기준 · 스크립트 5종 없음 · Codex 1사이클 중단 · 실제 토큰 값 0건. 상세는 `rules/docs/plans/2026-11-17/org-publish/log.md` |
| 2026-11-17 | 저장소 공개·이름·위치 변경 | 조직 `agent-discipline/need-harness-lab` public, 로컬 `/home/jun/project/agent-discipline/need-harness-lab` (기록: rules 저장소 `docs/plans/2026-11-17/`) |
| 17:2x | 인터뷰 마감 | 하네스 v4.3 · 웹 도구 세 조건 허용·기록 · 두 번째 리뷰어 Fable 5.2 · 보고 단위 = 준비·파일럿·1사이클 뒤 승인. 나머지 4항은 메인 기본값(명세 §0) |
| 17:31 | 명세 합의 | "합의 — 준비 시작" · SPEC=1 · MODE=auto · stakes 높음 |
| 17:3x | 준비 워커(Opus) 기동 — task 01·02·03 | 기준 SHA `94cbc30`, 하네스 기준 `7b0ef7b`. 유료 실행·push 금지 |

## 리뷰 ledger (中↑ — 듀얼 리뷰가 돈 작업 필수)

- review packet: (준비 diff 확정 후 기입)

| id | first_seen_loop | source | 근거(file:line) | disposition | status | fixed_in_loop |
|----|-----------------|--------|-----------------|-------------|--------|---------------|
| | | | | | | |

## 생략한 검증 (긴급 경로 빚 — DEBT=1인 동안 정본)

- 없음(긴급 경로 아님).

## 사용자 대기 항목

- [ ] Claude 구독 토큰 재발급(`claude setup-token`) 후 저장소 밖 env 파일에 보관 — 절차는 `payment-settlement/claude/docs/auth-token.md`·`runbook.md`. 인증 스모크(task 05)의 선행 조건.

## 완료 요약 (마감 시)

