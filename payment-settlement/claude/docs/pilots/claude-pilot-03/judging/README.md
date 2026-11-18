# 판정 절차 기록 — claude-pilot-03

진행자(Fable 5.2)가 작성·실행했다. **통과 여부는 여기서 정하지 않는다** — 판정기가 기계적으로 낸 `status` 칸과 원자료만 둔다. 판정표(`verdict.json`)는 메인이 쓴다.

## 판정 대상
- 작업 폴더 `.runs/claude-pilot-03/workspace`, 브랜치 `feat/payment-settlement-v1`, HEAD `e227e10`(미커밋 변경 0). run은 턴 0004 뒤 `CENSORED_BUDGET`으로 닫혔고 PM은 완료를 선언하지 않았다(마지막 응답 = 개발 DB 초기화 승인 요청). 판정은 그 시점의 커밋된 코드로 한다.

## 도구
- 공유 판정기(수정하지 않음): `payment-settlement/evaluation/acceptance.py`의 `Evaluator`(C01~C12·C15·C16·C18), `claude/scripts/load_check.py` + `evaluation/k6/workload.js`(C20).
- 이 구현 전용 어댑터(진행자 작성, 이 폴더): `adapter.py` — `evaluation/pilot_adapter.py`와 같은 의미(snapshot / verify_requests / audit·reconcile)를 이 구현의 스키마(`payments`·`ledger_entries`·`settlements`·`settlement_items`)로 옮김. 구현자가 낸 SQL(`docs/record-level-verification.sql`)은 쓰지 않고, 모든 금융 행과 은행 SQLite journal을 직접 읽어 대조한다. 음성 대조: `test_adapter.py`(`adapter-unittest.txt`).
- `run_http.py`: `acceptance.py`의 `__main__`은 Codex 파일럿 어댑터만 알기 때문에 같은 `Evaluator`에 위 어댑터를 물리는 구동 스크립트.
- `run_faults.py`: `evaluation/pilot_faults.py`(C13·C14·C17)는 Codex 파일럿 project만 받도록 막혀 있어 그대로 쓸 수 없다. 시나리오 논리는 그대로 두고 스키마에 닿는 SQL만 바꾼 사본.
- `pending.sql`: `load_check.py`의 PENDING 수렴 쿼리.

## 어댑터가 Codex 파일럿 어댑터와 다른 점(메인이 판정할 때 볼 것)
1. **거절 응답이 남긴 FAILED 행**: 이 구현은 잔액 부족(409)도 FAILED 결제 행으로 저장한다(명세 합의 사항). Codex 어댑터는 "수락되지 않은 요청이 만든 행"을 422에 한해서만 허용했다. 이 어댑터는 409·422 응답이 남긴 행을 **원장 0행·은행 기록 없음·FAILED**일 때만 허용하고, 400/403/404 뒤의 행이나 그 밖의 상태는 `rejected_request_changed_state`로 잡는다.
2. **원장 형태**: 성공 결제 1건 = 3행(USER_BALANCE DEBIT amount / AGENCY_PAYABLE CREDIT net / FEE_REVENUE CREDIT fee), 정산 1건 = 2행. Codex 어댑터의 "성공당 원장 1행" 검사를 이 형태의 정확 일치 검사로 바꿈.
3. **잔액 보존**: `users.reserved_minor`가 없다(PENDING은 잔액을 줄이지 않음). `balance + Σ SUCCEEDED amount = 1,000,000`과 `balance + Σ USER_BALANCE DEBIT = 1,000,000`을 각각 전수 집계하고, PENDING 보류 합 ≤ 잔액을 따로 본다.
4. **정산 cutoff**: `cutoff_at` 칸이 없어 `settlements.created_at`을 snapshot 시각으로 본다.
5. **C17 시각 fixture**: `payments`에 `business_date = (succeeded_at AT TIME ZONE 'Asia/Seoul')::date` CHECK가 있어 `succeeded_at`만 바꾸면 UPDATE가 거부된다. `succeeded_at`과 `business_date`를 함께 바꾼다(원장·잔액은 그대로).
6. 수수료율: 저장된 `fee_bps`가 `agencies.fee_bps`와 같은지도 본다(추가 검사).

## 실행 순서(각각 새 volume의 전용 Compose project, 한 번에 하나)
| stack | project | 포트(app/db/bank) | 실행 | 산출물 |
|---|---|---|---|---|
| v1 | `payment-claude-pilot-03-v1` | 38011/39011/40011 | `run_http.py` → `adapter.py`(C19) | `v1/` |
| v2 | `payment-claude-pilot-03-v2` | 38012/39012/40012 | `run_faults.py` | `v2/` |
| v3 | `payment-claude-pilot-03-v3` | 38013/39013/40013 | `load_check.py` | `v3/` |

실행 명령·종료 코드·시각은 `commands.log`, 시나리오별 관측 메모는 `observations.md`.
