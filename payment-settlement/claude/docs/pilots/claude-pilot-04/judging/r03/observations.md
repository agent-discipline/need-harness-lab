# 시나리오별 관측 메모 — claude-pilot-04, 판정 3회째(턴 0003 = 피드백 2 [C01] 정산 잔액 뒤)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터는 이 회차 스키마에 맞춰 다시 씀(`README.md`, 음성 대조 20건 통과).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 32줄, 해시는 `source-head.txt`). 실행 2026-11-18T12:36:52Z~12:40:48Z, **v1만**(C13·C14·C17·C20은 이번 회차 미실행, 직전 결과 유지 — `../r01/`). `cx-` 2개 실행 중인 채 실행.

## v1 (`v1/acceptance-http.json` 요청 400건, `v1/acceptance-audit.json`) — `run_http.py` exit 1

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01 | PASS (HTTP+DB+BANK) | [0,1] | 201 SUCCEEDED, audit PASS(대행사 잔액 변화 없음) |
| C02 | FAIL | [1,4] | amount −1·0은 400. amount 1000001 → **422** FAILED `INSUFFICIENT_BALANCE`, FAILED 행 저장(기대 400·변경 0). 뒤 입력(1.5, USD, 필드 누락 4종)은 보내지 않음 |
| C03 | PASS | [4,6] | 404 `USER_NOT_FOUND`/`AGENCY_NOT_FOUND`(경로는 있음), 변경 0 |
| C04 | PASS | [6,27] | 201 뒤 200×20, 같은 paymentId |
| C05 | PASS | [27,30] | 409×2 |
| C06 | PASS | [30,230] | 동일 키 동시 100, paymentId 1개 |
| C07 | FAIL | [230,330] | 잔액 부족 응답이 **422**(기대 409 — 허용 상태 코드 201·202·409 밖) |
| C08 | FAIL | [330,332] | 대행사 902 201, 같은 키 대행사 903 **409**(기대 201) |
| C09 | FAIL | [332,335] | 헤더≠body 403. 다른 대행사의 조회 **200**(기대 404) |
| C10 | FAIL | [335,336] | `X-Bank-Fault: decline` → **201 SUCCEEDED**(기대 422) |
| C11 | PASS | [336,340] | `X-Bank-Fault: timeout-before`·`5xx` 요청이 둘 다 **즉시 201 SUCCEEDED(0.02~0.03초)** — 장애 헤더가 은행에 전달되지 않은 것으로 보임(C10과 같은 양상). 판정기는 201/202 + 최종 SUCCEEDED만 보므로 기계적으로 PASS. **장애가 실제로 주입된 상태의 관측이 아님** |
| C12 | PASS | [340,342] | `timeout-after`도 즉시 201(0.02초) — 위와 같음 |
| C15 | FAIL | [342,347] | 결제 3건 201, 정산 201(gross 10301 / fee 102 / net 10199 / count 3), 재전송 200 같은 settlementId — HTTP 단언 통과. 시나리오 뒤 audit FAIL: `settlement_business_date_missing`(정산 행에 영업일 칸이 없음 — 요청의 `businessDate`가 저장·반영되지 않음. **어댑터 허용 규칙을 넣지 않은 검사**, README) |
| C16 | FAIL | [347,398] | 같은 날짜 정산 동시 50: 1건 201(count 1), 49건 **422 `NOTHING_TO_SETTLE`**(기대 200/201 + 0건) |
| C18 | FAIL | [398,400] | 빈 정산 2건 모두 **422 `NOTHING_TO_SETTLE`**(기대 200/201·0건 0원) |

- 마지막 전수 대조(`v1/acceptance-audit.json`): FAIL, errors 4건 모두 `settlement_business_date_missing`(정산 2개). 집계 9개 모두 0(대행사 잔액 = Σ 정산 순액 일치). counts: payments 115(SUCCEEDED 64 / FAILED 51), ledger 328, settlements 2, 정산된 결제 4, 은행 journal 64.
- 판정기 status PASS 7개(C01·C03·C04·C05·C06·C11·C12). C11·C12는 위 관측대로 장애 미주입 상태의 PASS.

## 가장 앞선 미충족 항목(피드백 대상)
C02 — amount 1000001이 422 + FAILED 행. 보낸 원문 `feedback-003.txt`.

## C01~C20 원자료 위치(이 폴더 기준)
C01~C12·C15·C16·C18: `v1/acceptance-http.json` cases.<ID>와 위 요청 색인. C19: `v1/acceptance-audit.json`(FAIL). C13·C14·C17: 미실행 — `../r01/v2/acceptance-faults.json`. C20: 미실행 — `../r01/v3/load/summary.json`. 명령 `commands.log`.
