# 시나리오별 관측 메모 — claude-pilot-04, 판정 6회째(턴 0006 = 피드백 5 [C08] 뒤)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 3회째와 같다(`../r03/README.md`, 음성 대조 20건 통과 — V4는 제약만 바꿔 칸은 같음).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 35줄, `source-head.txt`). 실행 2026-11-18T13:27Z~13:32:16Z, **v1만**(C13·C14·C17·C20 이번 회차 미실행, 직전 결과 유지 — `../r01/`). `cx-` 2개 실행 중.

## v1 (`v1/acceptance-http.json` 요청 456건, `v1/acceptance-audit.json`) — `run_http.py` exit 1

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01~C07 | PASS (HTTP+DB+BANK) | [0,386] | 5회째와 같음(C07은 허용 규칙 2 적용) |
| C08 | PASS | [386,388] | 대행사 902·903 같은 키 각각 201, paymentId 다름 |
| C09 | PASS | [388,391] | 헤더≠body 403, 다른 대행사의 조회 404 |
| C10 | FAIL | [391,392] | `X-Bank-Fault: decline` → **201 SUCCEEDED**(기대 422) |
| C11·C12 | PASS | [392,398] | 장애 헤더 미전달로 즉시 201 — 장애 주입 상태의 관측 아님(3회째 메모와 같음) |
| C15 | FAIL | [398,403] | HTTP 단언 통과, audit `settlement_business_date_missing` |
| C16 | FAIL | [403,454] | 1건 201, 49건 422 `NOTHING_TO_SETTLE` |
| C18 | FAIL | [454,456] | 422 `NOTHING_TO_SETTLE`×2 |

- 마지막 전수 대조: FAIL(errors 4건 모두 `settlement_business_date_missing`), 집계 9개 0.
- 판정기 status PASS 11개(C01~C09·C11·C12).

## 가장 앞선 미충족 항목(피드백 대상)
C10. 보낸 원문 `feedback-006.txt`.

## 원자료 위치(이 폴더 기준)
C01~C12·C15·C16·C18: `v1/acceptance-http.json` cases.<ID>와 요청 색인. C19: `v1/acceptance-audit.json`. C13·C14·C17: 미실행 — `../r01/v2/acceptance-faults.json`. C20: 미실행 — `../r01/v3/load/summary.json`. 명령 `commands.log`.
