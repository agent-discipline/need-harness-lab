# 시나리오별 관측 메모 — claude-pilot-04, 판정 8회째(턴 0008 = 피드백 7 [C15] 뒤)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터 변경점은 `README.md`(V5의 `business_date` 반영, 새 허용 규칙 없음, 음성 대조 21건 통과).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 37줄, `source-head.txt`). 실행 2026-11-18T14:03Z~14:09:37Z, **v1 + v2**(v3는 이번 회차 미실행, 직전 결과 유지 — `../r01/`). `cx-` 2개 실행 중.

## v1 (`v1/acceptance-http.json` 요청 472건, `v1/acceptance-audit.json`) — `run_http.py` exit 1

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01~C12 | PASS (HTTP+DB+BANK) | [0,414] | 7회째와 같음(C07·C10은 허용 규칙 1·2 적용, C11·C12는 장애 주입 상태) |
| C15 | PASS | [414,419] | 정산 201(businessDate 2026-11-18, gross 10301 / fee 102 / net 10199 / count 3), 재전송 200 같은 settlementId. audit PASS(정산 영업일 = 포함 결제의 KST 성공 날짜, 대행사 잔액 = Σ 정산 순액). 정산 항목은 `payments.settlement_id`(허용 규칙 5) |
| C16 | FAIL | [419,470] | 같은 날짜 정산 동시 50(서로 다른 키): 1건 201(paymentCount 1), **49건 422 `NOTHING_TO_SETTLE`**(기대: 모두 200/201, paymentCount 합 1) |
| C18 | FAIL | [470,472] | 2000-01-01 빈 정산 2건 모두 **422 `NOTHING_TO_SETTLE`**(기대 200/201·0건 0원, 재전송 200 같은 settlementId) |

- 마지막 전수 대조(`v1/acceptance-audit.json`): **PASS**, errors 0, 집계 9개 0. payments 115(SUCCEEDED 64 / FAILED 51), ledger 332, settlements 2, 정산된 결제 4, 은행 64.

## v2 (`v2/acceptance-faults.json`) — `run_faults.py` exit 1

| ID | 판정기 status | 관측 |
|---|---|---|
| C13 | PASS | app SIGKILL(은행 기록 있음 + DB PENDING 확인 뒤) → 재기동 뒤 SUCCEEDED, 재전송 200, audit PASS |
| C14 | PASS | db SIGKILL → 복구 뒤 SUCCEEDED, 재전송 200, audit PASS |
| C17 | FAIL | **경계 fixture 부분은 단언 통과**: 2020-01-01 정산 1건 101원, 2020-01-02 정산 2건 205원, 뒤늦은 1건 추가 뒤 같은 키 재전송은 그대로·새 키 정산 1건 104원. **`late_success` FAIL**: 은행 중단 중 결제 202 뒤 오늘 날짜 정산(대상 0건)이 422 `NOTHING_TO_SETTLE`(기대 0건 성공) — 그 뒤 단계는 진행되지 않음 |

- `final_audit` FAIL: `pending` 1건(late_success가 중간에 멈춰 수렴을 기다리지 않은 결제). 그 밖의 오류 없음.
- 판정기 status PASS 15개(C01~C15).

## 가장 앞선 미충족 항목(피드백 대상)
C16(P0). 보낸 원문 `feedback-008.txt`.

## 원자료 위치(이 폴더 기준)
C01~C12·C15·C16·C18: `v1/acceptance-http.json` cases.<ID>와 요청 색인. C13·C14·C17: `v2/acceptance-faults.json`. C19: `v1/acceptance-audit.json`(PASS)·`v2/acceptance-audit.json`. C20: 미실행 — `../r01/v3/load/summary.json`. 명령 `commands.log`.
