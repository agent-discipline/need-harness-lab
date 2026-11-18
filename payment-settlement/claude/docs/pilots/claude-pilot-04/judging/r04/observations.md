# 시나리오별 관측 메모 — claude-pilot-04, 판정 4회째(턴 0004 = 피드백 3 [C02] 뒤)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 3회째와 같다(`../r03/README.md`, 음성 대조 20건 통과).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 32줄, `source-head.txt`). 실행 2026-11-18T12:51Z~12:55:26Z, **v1만**(C13·C14·C17·C20 이번 회차 미실행, 직전 결과 유지 — `../r01/`). `cx-` 2개 실행 중.

## v1 (`v1/acceptance-http.json` 요청 406건, `v1/acceptance-audit.json`) — `run_http.py` exit 1

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01 | PASS (HTTP+DB+BANK) | [0,1] | 201 SUCCEEDED |
| C02 | PASS | [1,10] | 잘못된 입력 9종 모두 400, 변경 0 |
| C03 | PASS | [10,12] | 404×2 |
| C04 | PASS | [12,33] | 201 뒤 200×20 |
| C05 | PASS | [33,36] | 409×2 |
| C06 | PASS | [36,236] | 동일 키 동시 100, paymentId 1개 |
| C07 | FAIL | [236,336] | 201 SUCCEEDED 49 / 202 PENDING 1 / **422 FAILED `INSUFFICIENT_BALANCE` 50**(허용 상태 코드 201·202·409 밖 — 잔액 부족이 422). 콜백의 첫 단언에서 멈춤 |
| C08 | FAIL | [336,338] | 대행사 902 201, 같은 키 대행사 903 **409** |
| C09 | FAIL | [338,341] | 다른 대행사의 조회 **200**(기대 404) |
| C10 | FAIL | [341,342] | `X-Bank-Fault: decline` → 422 아님 |
| C11 | PASS | [342,346] | 3회째와 같음: 장애 헤더 미전달로 즉시 201 — 장애 주입 상태의 관측 아님 |
| C12 | PASS | [346,348] | 위와 같음 |
| C15 | FAIL | [348,353] | HTTP 단언 통과, audit `settlement_business_date_missing`(3회째와 같음) |
| C16 | FAIL | [353,404] | 1건 201, 49건 422 `NOTHING_TO_SETTLE` |
| C18 | FAIL | [404,406] | 422 `NOTHING_TO_SETTLE`×2 |

- 마지막 전수 대조: FAIL(errors 4건 모두 `settlement_business_date_missing`), 집계 9개 0. payments 114(SUCCEEDED 64 / FAILED 50), ledger 328, settlements 2, 은행 64.
- 판정기 status PASS 8개(C01~C06·C11·C12).

## 가장 앞선 미충족 항목(피드백 대상)
C07 — 잔액 부족 응답 422. 보낸 원문 `feedback-004.txt`.

## 원자료 위치(이 폴더 기준)
C01~C12·C15·C16·C18: `v1/acceptance-http.json` cases.<ID>와 요청 색인. C19: `v1/acceptance-audit.json`. C13·C14·C17: 미실행 — `../r01/v2/acceptance-faults.json`. C20: 미실행 — `../r01/v3/load/summary.json`. 명령 `commands.log`.
