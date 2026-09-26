# 독립 판정 중 발견한 문제

## P-ACCEPT-01 — KST 정산 날짜의 SQL 타입 변환 오류

- 발생: KST 2026-09-24 성공 결제(101,199,10001원)를 같은 일자로 정산했는데 0건/0원. C15/C16 실패.
- 재현 증거: `acceptance-http.json` C15/C16. DB succeeded_at=2026-09-23T23:44:13Z는 KST 9/24다.
- 추가 증거: `acceptance-faults.json` C17. 자정 직전/직후 세 timestamp 2020-01-01T14:59:59Z,15:00:00Z,15:00:01Z를 넣었는데 1/1에 3건이 모두 포함되고 1/2는 0건.
- 원인: PaymentRepository.kt의 `?::date AT TIME ZONE 'Asia/Seoul'`은 PostgreSQL에서 `timestamp without time zone`이 된다. UTC session에서 `'2026-09-24'::date AT TIME ZONE 'Asia/Seoul'`은 9/24 09:00을 반환한다. 의도한 KST 자정의 UTC 시각은 9/23 15:00이다.
- 해결 상태: 외부 Codex 구현 세션에서 날짜 변환을 수정했다. 평가자는 구현 코드를 변경하지 않았다.
- 재검증: 수정 빌드와 fresh verify2 volumes에서 C15/C16 HTTP+DB+BANK PASS(`verification-02/http.json`). C17 결과는 `verification-02/faults.json`에 별도 기록한다. timestamp fixture를 주입한 기존 DB는 재사용하지 않는다.

## E-ACCEPT-01 — 판정기 초기 SQL 계정 오기

- 발생: 최초 audit가 `role payment does not exist`로 실행 오류.
- 원인: DB 이름 payment를 DB 사용자로 잘못 가정. fixture 실제 사용자는 lab.
- 해결: adapter를 `psql -U lab -d payment`로 수정.
- 검증: C19 전체 사용자100만/대행사1000, 잔액·예약·은행·원장·정산 전수대조 PASS (`acceptance-audit.json`). 최초 실행 오류를 구현 결함으로 세지 않는다.

## E-ACCEPT-02 — 동시성 helper의 모듈명 가림

- 발생: 구현 중 함수명 concurrent가 concurrent.futures 모듈 참조를 가릴 가능성 발견.
- 원인: 지역 함수명과 import 이름 충돌.
- 해결: concurrent_payments로 변경 후 실제100동시결제·50동시정산 실행.
- 검증: C06/C07 HTTP 관측 통과. C16은 실행기 오류가 아닌 P-ACCEPT-01 업무결함으로 실패.

## 성공한 장애 검증

C13/C14는 은행 journal 성공 + DB PENDING을 관측한 정확한 구간에서 app/DB를 각각 SIGKILL했다. 재시작 후 동일 paymentId로 수렴하고 은행·원장·차감은 각1회였다. `acceptance-faults.json`에 kill/healthy/final 시각과 전수대조를 보존했다. 전체 판정 합격을 뜻하지 않는다.
