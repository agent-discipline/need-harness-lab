# 시나리오별 관측 메모 — claude-pilot-04, 판정 7회째(턴 0007 = 피드백 6 [C10] 뒤)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 3회째와 같다(`../r03/README.md`, 음성 대조 20건 통과).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 35줄, `source-head.txt`). 실행 2026-11-18T13:39Z~13:46:41Z. **v1 + v2**: v1에서 C01~C12가 모두 PASS로 나와, 순서상 다음 P0 항목인 C13·C14의 현재 결과가 있어야 "가장 앞선 미충족"을 정할 수 있으므로 v2를 이번 회차에 함께 돌렸다(메인 지시의 "v1의 P0 HTTP 항목이 모두 통과한 뒤부터"보다 한 회차 이른 실행 — C15·C16은 아직 FAIL. 진행자 판단, 일지에 적음). v3(C20)는 이번 회차 미실행, 직전 결과 유지(`../r01/`). v1 시작 때 `cx-` 2개, v1 끝~v2 동안 `cx-` 0개.

## v1 (`v1/acceptance-http.json` 요청 471건, `v1/acceptance-audit.json`) — `run_http.py` exit 1

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01~C09 | PASS (HTTP+DB+BANK) | [0,391] | 6회째와 같음(C07은 허용 규칙 2 적용) |
| C10 | PASS | [391,392] | `X-Bank-Fault: decline` → 422, FAILED 행(홀드 해제), 은행 기록 없음 — 허용 규칙 1·2 적용 |
| C11 | PASS | [392,411] | **장애가 실제로 주입된 상태**: timeout-before → 202(약 1초 뒤), 5xx → 202, 조회를 되풀이해 둘 다 30초 안에 SUCCEEDED |
| C12 | PASS | [411,413] | timeout-after → 201 SUCCEEDED(구현이 은행 조회로 동기 확정), 은행 기록 1건 |
| C15 | FAIL | [413,418] | 결제 3건 201, 정산 201(gross 10301 / fee 102 / net 10199 / count 3), 재전송 200 같은 settlementId — HTTP 단언 통과. audit FAIL: `settlement_business_date_missing`(정산 기록·응답에 영업일이 없음, 요청의 `businessDate`가 반영되지 않음 — 허용 규칙 넣지 않은 검사) |
| C16 | FAIL | [418,469] | 1건 201(count 1), 49건 422 `NOTHING_TO_SETTLE` |
| C18 | FAIL | [469,471] | 422 `NOTHING_TO_SETTLE`×2 |

- 마지막 전수 대조: FAIL(errors 4건 모두 `settlement_business_date_missing`), 집계 9개 0.

## v2 (`v2/acceptance-faults.json`, `v2/acceptance-audit.json`) — `run_faults.py` exit 1, 13:45:36Z~13:46:24Z, `cx-` 0개

| ID | 판정기 status | 관측 |
|---|---|---|
| C13 | PASS | 은행 기록 있음 + DB `PENDING` 확인 뒤 app SIGKILL. 처음 요청은 연결 끊김. 재기동 healthy까지 16.8초, 그 뒤 5.9초에 SUCCEEDED. 같은 키 재전송 200·같은 paymentId, 직후 audit PASS |
| C14 | PASS | 같은 경계에서 db SIGKILL. 처음 요청 응답 202. db healthy까지 6.8초, 그 뒤 8.1초에 SUCCEEDED. 재전송 200, audit PASS |
| C17 | FAIL | 2020-01-01 정산이 날짜를 가리지 않고 대행사의 미정산 결제를 모두 가져가고, 다음 날짜 정산은 422 `NOTHING_TO_SETTLE`. `late_success`도 FAIL(빈 정산 422) |

- `final_audit` FAIL(정산 영업일 없음).
- 판정기 status PASS 14개(C01~C14).

## 가장 앞선 미충족 항목(피드백 대상)
C15(P0). 보낸 원문 `feedback-007.txt`.

## 원자료 위치(이 폴더 기준)
C01~C12·C15·C16·C18: `v1/acceptance-http.json` cases.<ID>와 요청 색인. C13·C14·C17: `v2/acceptance-faults.json`. C19: `v1/acceptance-audit.json`·`v2/acceptance-audit.json`. C20: 미실행 — `../r01/v3/load/summary.json`. 명령 `commands.log`.
