# 판정 절차 기록 — claude-pilot-04, 판정 3회째(턴 0003 = 피드백 2 뒤)

진행자 작성. 통과 여부는 정하지 않는다. 도구 구성은 `../r01/README.md`와 같고, **어댑터는 이 회차의 스키마에 맞춰 다시 썼다**(`adapter.py`, 음성 대조 20건 = 정상 3 + 깨뜨린 데이터 17, `adapter-unittest.txt`).

## 바뀐 스키마(V3 migration)
- `settlements`(id, idempotency_key, agency_id, payment_count, gross_minor, fee_minor, net_minor, created_at) 신설. **`business_date` 칸이 없다.** `settlement_items` 테이블은 없고 `payments.settlement_id`·`settled_at`이 정산 항목 구실을 한다.
- 원장: CAPTURE의 대행사 leg가 `AGENCY_PAYABLE`(+net)로 바뀌고, 정산 때 결제별 `SETTLE` 2행(`AGENCY_PAYABLE` −net, `AGENCY_SETTLEMENT` +net)이 붙는다. 결제 성공 때 `agencies.settlement_balance_minor`는 바뀌지 않는다.

## 어댑터의 허용 규칙(메인이 판정할 때 볼 것)
1~2, 4는 1회째와 같다(홀드 모델·FAILED 무효과, 409·422 뒤 FAILED 행, 은행 키 = 결제 id).
3. 원장 형태: 성공 결제 = AUTHORIZE 2 + CAPTURE 3(0원 leg 생략), 정산된 결제는 + SETTLE 2의 정확 일치.
5. **정산 항목 = `payments.settlement_id`**(별도 items 테이블 없음 — 결제당 최대 1개가 스키마로 보장됨). 정산 합계(gross·fee·net·count) = 그 정산을 가리키는 결제들의 합으로 대조.
- **허용하지 않고 계약 그대로 둔 검사**: ① `agency_balance_vs_settlements`(대행사 잔액 = Σ 정산 순액) ② **정산의 영업일**: 결제가 들어 있는 정산은 `business_date`가 있고 각 결제의 성공 확정 시각(KST) 날짜와 같아야 한다 — 이 스키마에는 칸이 없으므로 결제가 든 정산마다 `settlement_business_date_missing`이 난다 ③ 정산 snapshot 시각(`created_at`) 이전에 성공한 결제만 포함.
