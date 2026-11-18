# 판정 8회째 — 어댑터 변경점(턴 0008의 V5 migration 반영)

도구 구성은 `../r01/README.md`, 스키마와 허용 규칙 1~5는 `../r03/README.md`와 같다. 달라진 것:
- `settlements.business_date`가 생겨 3회째에 "허용하지 않고 둔 검사 ②"(정산 영업일 = 포함된 결제 성공 확정 시각의 KST 날짜)가 이제 실제 값으로 대조된다.
- `payments.business_date`가 생김: 성공 결제는 `completed_at`의 KST 날짜와 같아야 하고 그 밖은 NULL(`payment_business_date` — 추가 검사). 기존 결제의 불변 칸에도 넣음.
- `run_faults.py`의 C17 시각 fixture는 `completed_at`과 `business_date`를 함께 바꾼다(파일럿 03 어댑터와 같은 처리 — 이 구현은 정산 대상을 `business_date`로 고르므로 성공 시각만 옮기면 fixture가 뜻을 잃는다).
- 음성 대조 21건(`adapter-unittest.txt`).
새 허용 규칙은 없다.
