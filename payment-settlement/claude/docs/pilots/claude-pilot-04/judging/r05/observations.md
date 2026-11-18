# 시나리오별 관측 메모 — claude-pilot-04, 판정 5회째(턴 0005 = 피드백 4 [C07] 뒤)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 3회째와 같다(`../r03/README.md`, 음성 대조 20건 통과).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 34줄, `source-head.txt`). 실행 2026-11-18T13:13Z~13:17:57Z, **v1만**(C13·C14·C17·C20 이번 회차 미실행, 직전 결과 유지 — `../r01/`). `cx-` 2개 실행 중.

## v1 (`v1/acceptance-http.json` 요청 456건, `v1/acceptance-audit.json`) — `run_http.py` exit 1

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01~C06 | PASS (HTTP+DB+BANK) | [0,236] | 4회째와 같음 |
| C07 | PASS | [236,386] | 한 사용자 동시 100 × 20,000원: 201/202 50건, **409 50건**. 409는 FAILED `INSUFFICIENT_BALANCE` 행으로 저장됨(원장 0행 또는 홀드 해제, 은행 기록 없음) — **어댑터 허용 규칙 2(409·422 뒤 무효과 FAILED 행)가 적용된 PASS** |
| C08 | FAIL | [386,388] | 대행사 902 키 `tenant` → 201. 대행사 903 같은 키 → **409 `IDEMPOTENCY_CONFLICT`**(기대 201, 다른 paymentId) |
| C09 | FAIL | [388,391] | 다른 대행사의 조회 **200**(기대 404) |
| C10 | FAIL | [391,392] | `X-Bank-Fault: decline` → 422 아님 |
| C11·C12 | PASS | [392,398] | 장애 헤더 미전달로 즉시 201 — 장애 주입 상태의 관측 아님(3회째 메모와 같음) |
| C15 | FAIL | [398,403] | HTTP 단언 통과, audit `settlement_business_date_missing` |
| C16 | FAIL | [403,454] | 1건 201, 49건 422 `NOTHING_TO_SETTLE` |
| C18 | FAIL | [454,456] | 422 `NOTHING_TO_SETTLE`×2 |

- 마지막 전수 대조: FAIL(errors 4건 모두 `settlement_business_date_missing`), 집계 9개 0.
- 판정기 status PASS 9개(C01~C07·C11·C12).

## 가장 앞선 미충족 항목(피드백 대상)
C08. 보낸 원문 `feedback-005.txt`.

## 원자료 위치(이 폴더 기준)
C01~C12·C15·C16·C18: `v1/acceptance-http.json` cases.<ID>와 요청 색인. C19: `v1/acceptance-audit.json`. C13·C14·C17: 미실행 — `../r01/v2/acceptance-faults.json`. C20: 미실행 — `../r01/v3/load/summary.json`. 명령 `commands.log`.
