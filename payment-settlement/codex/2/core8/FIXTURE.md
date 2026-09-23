# 공통 실험 인프라

이 디렉터리는 세 실험군에 동일하게 제공하는 fixture다. 은행 모의 서버 구현 토큰은 실험 구현 토큰에서 제외한다. 은행은 외부 금융망과 연결되지 않으며 journal 한 행을 한 번의 송금 효과로 간주한다.

## 은행 HTTP 계약

Python 3.12 표준 라이브러리, 포트 `8081`, 영속 SQLite journal `/data/bank.sqlite3`를 사용한다. `/data`에 실험 실행별 전용 volume을 연결한다. SQLite WAL과 쓰기 트랜잭션 및 기본키로 동시 요청의 동일 키 효과를 하나로 제한한다. 서버를 재시작해도 같은 volume이면 기존 결과가 유지된다.

- `GET /health`: 200 `{"status":"UP"}`.
- `POST /transfers`: `Idempotency-Key` 필수(최대 200자), JSON `{"paymentId":"p1","userId":1,"agencyId":1,"amount":100,"currency":"KRW"}`. ID와 금액은 양의 정수, `paymentId`는 빈 문자열이 아닌 문자열이다. 금액은 KRW 정수 단위다.
- 성공: 200 `{"bankTransferId":"<uuid>","status":"SUCCEEDED"}`. 같은 키와 같은 본문은 동일 응답, 같은 키와 다른 본문은 409 `IDEMPOTENCY_CONFLICT`.
- `GET /transfers/{key}`: 기록이 있으면 성공 결과 200, 없으면 404 `TRANSFER_NOT_FOUND`. 키를 URL 인코딩한다.
- 잘못된 요청은 400 `INVALID_REQUEST`.

`X-Fault`는 fixture 전용 헤더이며 다음 값만 허용한다. 기존 성공 키의 재요청은 fault보다 기존 결과/본문 충돌 판정을 우선한다.

| 값 | 효과 | 응답 |
|---|---|---|
| 없음 | journal 기록 | 200 |
| `decline` | 기록 없음 | 422 `BANK_DECLINED` |
| `5xx` | 기록 없음 | 503 `BANK_UNAVAILABLE` |
| `timeout-before` | 기록 없음, 3초 대기 | 503 `BANK_UNAVAILABLE` |
| `timeout-after` | 먼저 commit, 3초 대기 | 200 |

timeout 시나리오는 호출자의 read timeout을 3초 미만으로 고정해야 한다. 실패 응답은 journal에 저장하지 않으므로 이후 fault 없는 요청은 성공할 수 있다. 미응답 성공의 판정은 조회 API를 이용한다. 은행 요청 본문은 추가 필드를 허용하지 않는다.

## PostgreSQL fixture

PostgreSQL 16 최초 빈 volume 시작 시 `postgres/001-seed.sql`을 init 디렉터리에 마운트한다. `users`는 정확히 1,000,000행(id 1..1,000,000), 초기 `balance_minor=1000000`이다. `agencies`는 1,000행(id 1..1000), `fee_bps=100`, `settlement_balance_minor=0`이다. users의 잔액이 사용자 계좌이며 별도 accounts 테이블은 없다. payments, ledger 및 추가 업무 테이블은 참가 에이전트가 구현한다. 동일 fixture 복원에는 새 전용 DB volume을 사용한다.

## 검증

저장소 루트에서 `python3 -m unittest discover -s payment-settlement/infra/fake-bank -v`를 실행한다. 동시 요청 100건의 단일 효과, 재시작 복원, 키 충돌, 장애별 기록 여부, timeout-after의 commit 시점, HTTP 성공/검증 계약을 확인한다. 실제 Docker/DB 기동 검증과 자원 제한은 상위 compose 및 실험 계획을 따른다.
