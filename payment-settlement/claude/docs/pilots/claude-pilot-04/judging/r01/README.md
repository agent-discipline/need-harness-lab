# 판정 절차 기록 — claude-pilot-04, 판정 1회째(완료 선언 1회째, 턴 0001 뒤)

진행자(Fable 5.2)가 작성·실행했다. **통과 여부는 여기서 정하지 않는다** — 판정기가 기계적으로 낸 `status` 칸과 원자료만 둔다. 판정표(`verdict.json`)는 메인이 쓴다.

폴더 배치: 이 파일럿은 판정 → 피드백을 되풀이하므로 판정 회차마다 `judging/rNN/`을 따로 둔다(런북은 `judging/` 하나만 적는다 — 회차 구분은 진행자가 덧붙인 것). 어댑터도 회차마다 그때의 스키마에 맞춘 사본을 그 폴더에 둔다.

## 판정 대상
- 작업 폴더 `.runs/claude-pilot-04/workspace`의 **작업 트리**(Compose build context가 읽는 그대로). 구현자는 커밋하지 않았다: HEAD는 `ec7b322 starter`, 미커밋 변경 30줄(`source-head.txt`에 목록과 내용 해시). 런북의 "커밋된 코드, 미커밋 변경 0"과 다르다 — 1번 프롬프트는 커밋을 요구하지 않는다(메인 확인 사항).

## 도구
- 공유 판정기(수정하지 않음): `payment-settlement/evaluation/acceptance.py`의 `Evaluator`(C01~C12·C15·C16·C18), `claude/scripts/load_check.py` + `evaluation/k6/workload.js`(C20).
- 이 구현 전용 어댑터(이 폴더): `adapter.py` — `evaluation/pilot_adapter.py`와 같은 의미(snapshot / verify_requests / audit·reconcile)를 이 구현의 스키마로 옮김. 구현자의 SQL·검증 코드는 쓰지 않고 모든 금융 행과 은행 SQLite journal을 직접 읽는다. 음성 대조 `test_adapter.py`(14건: 정상 2 + 깨뜨린 데이터 12) → `adapter-unittest.txt`.
- `run_http.py`: 공유 `Evaluator`에 위 어댑터를 물리는 구동 스크립트(파일럿 03의 것과 같음, 경로만 다름).
- `run_faults.py`: `evaluation/pilot_faults.py`의 시나리오 논리 그대로, 스키마에 닿는 SQL만 바꾼 사본(성공 확정 시각 칸 `completed_at`).
- `pending.sql`: `load_check.py`의 PENDING 수렴 쿼리.

## 이 구현의 스키마(판정 1회째)
- `payments`(id uuid, idempotency_key **전역 UNIQUE**, user_id, agency_id, amount_minor, currency, fee_bps, fee_minor, net_minor, status, failure_code, bank_transfer_id, …, completed_at), `ledger_entries`(payment_id, entry_type AUTHORIZE/CAPTURE/RELEASE, account_type USER/PAYMENT_CLEARING/AGENCY_SETTLEMENT/PLATFORM_FEE, account_id, 부호 있는 amount_minor).
- **`settlements`·`settlement_items` 테이블이 없다.** 정산은 조회 API(`GET /agencies/{id}/settlement`)뿐이고, 결제 성공 시점에 `agencies.settlement_balance_minor`에 net을 바로 더한다.
- HTTP 경로는 `/payments`·`/agencies/{id}/settlement`(계약의 `/api/payments`·`/api/settlements`가 아님). 은행 장애 헤더는 `X-Fault`(계약은 `X-Bank-Fault`), `X-Agency-Id`는 읽지 않는다. 은행 멱등성 키는 결제 UUID 그대로.

## 어댑터의 허용 규칙(메인이 판정할 때 볼 것)
1. **수락 때 차감(홀드) 모델**: 이 구현은 결제를 받을 때 사용자 잔액을 먼저 줄이고(AUTHORIZE) 실패하면 되돌린다(RELEASE). 그래서 잔액 보존을 `balance + Σ(SUCCEEDED·PENDING amount) = 1,000,000`과 `balance − Σ USER 원장 = 1,000,000`으로 본다. FAILED 결제는 **원장 0행 또는 AUTHORIZE+RELEASE 한 쌍(사용자 순효과 0)**일 때만 허용하고 그 밖은 `ledger_failed_effect`.
2. **거절 응답이 남긴 FAILED 행**: 409·422 응답이 남긴 행은 FAILED이고 위 1의 무효과 조건 + 은행 기록 없음일 때만 허용한다(파일럿 03 어댑터와 같은 규칙). 400/403/404 뒤의 행이나 그 밖의 상태는 `rejected_request_changed_state`.
3. **원장 형태**: 성공 결제 1건 = AUTHORIZE 2행 + CAPTURE 3행(수수료 0이면 2행)의 정확 일치.
4. **은행 키**: 저장된 은행 키 칸이 없어 결제 id로 journal을 찾는다(키에 대행사·operation이 들어가지 않음 — 허용이 아니라 대조 방법).
- **허용하지 않고 계약 그대로 둔 검사**: `agency_balance_vs_settlements` — 대행사 정산 잔액 = Σ 정산 순액(정산 테이블이 없으면 0). 이 구현은 결제 성공 때 잔액을 올리므로 성공 결제가 하나라도 있으면 이 집계가 0이 아니게 된다. 구현의 자체 모델(잔액 = Σ 성공 결제 net)과의 일치 여부는 검사가 아닌 `info.agency_balance_vs_succeeded_net_mismatches`로만 남긴다.
- 정산 테이블이 생기면 이 어댑터로는 대조할 수 없다(`settlement_rows_present_but_adapter_has_no_settlement_schema`) — 다음 회차에 어댑터를 다시 쓴다.

## 실행 순서(각각 새 volume의 전용 Compose project, 한 번에 하나)
| stack | project | 포트(app/db/bank) | 실행 | 산출물 |
|---|---|---|---|---|
| v1 | `payment-claude-pilot-04-v1` | 38011/39011/40011 | `run_http.py` → `adapter.py`(C19) | `v1/` |
| v2 | `payment-claude-pilot-04-v2` | 38012/39012/40012 | `run_faults.py` | `v2/` |
| v3 | `payment-claude-pilot-04-v3` | 38013/39013/40013 | `load_check.py` | `v3/` |

실행 명령·종료 코드·시각은 `commands.log`, 시나리오별 관측 메모는 `observations.md`.
