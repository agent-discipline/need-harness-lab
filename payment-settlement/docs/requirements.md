# 동결 후보 요구사항 — 결제·정산 v1

이 파일은 2·3 최초 입력의 공통 업무 계약이며, 1에는 평가자가 순서대로 필요한 항목만 공개한다. 초기 서버에는 업무 구현을 넣지 않는다. 출시 인증이나 실제 금융망 적합성 평가가 아닌 모의 결제 처리 실험이다.

## 범위와 불변식

Kotlin/JVM 21, Spring MVC, PostgreSQL. Controller → application service → domain / repository / bank client의 레이어드 구성. HTTP와 SQL·은행 호출·상태 전이를 한 클래스에 넣지 않는다. 구체 구현 하나뿐인 요소에 불필요한 인터페이스를 늘리지 않는다. 은행 연동은 교체 가능한 port/interface로 분리한다.

KRW 정수 최소 단위(Long), 금액 1..1,000,000. 부동소수점 금액 금지. 사용자 1,000,000명, 대행사 1,000개, 사용자 초기 잔액 각 1,000,000원. 대행사 수수료 100bps(1%), 건별 `floor(amount * feeBps / 10000)`. 사용자 id와 대행사 id는 fixture의 bigint와 일치한다.

각 성공 결제의 사용자 차감 = 대행사 정산 대상액 + 수수료. 잔액 음수 금지. 같은 결제의 은행 송금·잔액 차감·원장 반영·정산 반영은 각각 한 번만 발생해야 한다. 실패/거절은 차감하지 않고, 결과 불명은 성공/실패로 거짓 확정하지 않는다. DB와 외부 HTTP를 하나의 원자적 트랜잭션이라고 가정하지 않는다.

## API 계약

모든 업무 요청에 `X-Agency-Id: <id>` 필요. 실험용 tenant 식별이며 실제 인증 대체 수단으로 주장하지 않는다. body의 agencyId와 다르면 403, 다른 대행사의 객체는 404로 감춘다. health는 공개.

| 요청 | 계약 |
|---|---|
| POST /api/payments | Idempotency-Key(1..128 ASCII), body `{userId, agencyId, amount, currency:"KRW"}` |
| GET /api/payments/{paymentId} | `{paymentId,userId,agencyId,amount,currency,status,bankTransferId}` |
| POST /api/settlements | Idempotency-Key, body `{agencyId, businessDate:"YYYY-MM-DD"}` |
| GET /api/settlements/{settlementId} | `{settlementId,agencyId,businessDate,status,grossAmount,feeAmount,netAmount,paymentCount}` |
| GET /health | HTTP 200 + DB 접속 결과 |

결제 상태는 PENDING/SUCCEEDED/FAILED. 장애 없는 정상 최초 요청은 동기 성공 201, 재전송 확정 응답 200, 중복 동시 요청 처리 중 또는 장애에 따른 결과 불명은 202. 정상 입력 전체를 비동기202로 반환하는 구현은 이번 고정 SLO 계약에서 허용하지 않는다. 동일 대행사·동일 멱등성 키·동일 payload는 동일 paymentId와 최종 결과를 반환한다. 키의 범위는 대행사+operation, 다른 payload는 409. 불명 상태의 재시도에도 새 은행 키를 만들지 않는다. 잘못된 입력400, 없는 사용자/대행사404, 잔액 부족409, 은행 확정 거절422. 에러 body는 `{code,message}`이며 내부 SQL/stacktrace를 노출하지 않는다. 수락한 비동기 요청은 영속 조회 가능해야 한다.

Idempotency-Key는 실험 기간 전체 동안 유지한다. HTTP 재시도와 프로세스 재시작 뒤에도 유효하다. 동시에 같은 사용자로 여러 다른 키를 보내도 누적 성공 금액이 초기 잔액을 넘지 않는다.

## 은행 모의 서버

`BANK_BASE_URL`의 POST /transfers에 `{paymentId,userId,agencyId,amount,currency}`와 Idempotency-Key를 보낸다. GET /transfers/{key}로 기록을 조회할 수 있다. 은행의 키는 전역 범위이므로 대행사/operation을 포함해 충돌을 막는다. 응답과 장애 의미는 `infra/README.md`를 따른다.

실험 프로필에서 요청의 `X-Bank-Fault`를 은행 `X-Fault`로 전달한다. 값 decline / timeout-before / timeout-after / 5xx. 이 헤더는 해당 결제의 첫 은행 시도에만 전달하고 자동 복구 요청에는 전달하지 않는다. timeout-before/5xx의 일회 장애는 첫 요청 종료 시 해제된 것으로 정의한다. 지속 장애는 평가자가 은행 컨테이너를 중단/재시작하여 별도로 주입하고 재시작 health 성공을 해제 시각으로 기록한다. 은행이 수락한 뒤 응답만 유실될 수 있다. HTTP timeout 초기값1초, 영속 복구·재조회로 30초 이내 최종 상태 수렴. 잘못된 무한 재시도·스레드 sleep 재시도 폭주 금지. 복구는 재시작에도 동작해야 한다. 은행 timeout-before/5xx가 지속되면 PENDING을 유지하거나 명세상 안전한 FAILED로 종결하되 중복 차감 금지. 장애 해제 후30초 이내 수렴한다.

## 정산

businessDate는 Asia/Seoul 기준 **성공 확정 시각**의 날짜. 해당 대행사·일자의 성공 결제만 집계한다. 한 번의 정산 요청은 확정 시각 이전에 성공했고 아직 정산되지 않은 결제를 snapshot으로 고정한다. 이후 늦게 확정된 결제는 새 키의 추가 정산으로 처리한다. 동시에 같은 대행사/일자에 요청해도 결제 하나가 두 번 포함되지 않는다. 다른 키로 빈 집합 정산은 금액0/건수0 성공으로 허용한다.

수수료는 결제 시 저장한 요율로 건별 계산 후 합산한다. 총매출 = 수수료 + 순정산액. 정산 성공 시 agencies.settlement_balance_minor를 순정산액만큼 정확히 한 번 증가시킨다. 이번 범위에서 정산은 내부 대행사 잔액 반영이며 별도의 2차 은행 지급·환불·부분취소·외환·카드 인증은 제외한다. 대행사 정산 잔액과 미정산 채무·수수료 계정은 회계상 중복 가산하지 않는다.

## 구현·검증 계약

레이어 경계, 명시적 transaction 경계, 금액·멱등성·상태 전이의 모듈화, 오류 매핑, migration과 인덱스, 쿼리 복잡도, 자원 제한을 검토한다. 의존 라이브러리 추가는 허용하되 동일 자원 한도 유지. 풀은 Hikari maximum10/minimum2, DB max_connections40 고정. 인메모리 저장만으로 결제/멱등성을 구현하지 않는다.

공통 fixture와 판정기는 수정하지 않는다. 실제 업무 tables/migrations는 구현자가 추가한다. 평가자가 record-level 검증할 수 있도록 payments·ledger·settlements·settlement_items의 의미 및 대응 SQL을 설명한다(특정 내부 스키마 구현 강제 없음). 동시 요청·타임아웃·장애·재시작의 회귀 테스트를 작성한다. 성능 숫자는 acceptance.md의 고정 workload로 판단한다.
