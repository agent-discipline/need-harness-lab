# 실험 실행 기록

최근 기록 상태: VALIDATING

## 1. 프롬프트 전문

UTC: 2026-09-23T23:43:57.384824+00:00 · 역할: implementer · 세션: 01a0ce5d-80ec-7ce0-a126-e2533a4072a7 · 상태: VALIDATING

```text
This is an excluded infrastructure/domain pilot, not one of the 30 measured runs. Implement the full supplied payment requirements in this workspace; preserve the shared fixture and resource limits. You can use Gradle and reach db:5432 and bank:8081 through the configured environment. Do not use subagents or goals. Compile and run meaningful unit/integration tests, and report remaining failures honestly.

제공된 Kotlin/Spring 서버에서 작업하세요. STARTER.md와 FIXTURE.md는 공통 실행 환경입니다.
업무 코드는 레이어드 구조로 구현하고 은행 연동은 교체 가능한 인터페이스로 분리하세요.
공통 fixture·외부 평가기·CPU/RAM·DB pool 한도는 변경할 수 없습니다.
필요한 테스트와 리팩토링까지 수행하고, 완료 시 실행한 검증과 남은 문제를 보고하세요.


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


# 자원과 비용 가정

## 실험 프로필 budget-v1

운영 역할 전체를 **CPU 4개, 메모리 5GiB 이하**의 논리 예산 안에 배치한다. 서비스마다 4CPU/5GiB를 주는 설정은 이 실험의 기본이 아니다. Docker 한도는 AWS 인스턴스 성능과 동일하다는 뜻이 아니다.

| 역할 | CPU 상한 | RAM 상한 | 설정 |
|---|---:|---:|---|
| Kotlin API 1개 | 1.5 | 1536MiB | JDK21, Xms256m/Xmx768m, ActiveProcessorCount=2, Tomcat max threads64 |
| PostgreSQL16 | 1.5 | 2048MiB | max_connections40, shared_buffers512MB, work_mem4MB, fsync/on, synchronous_commit/on |
| fake-bank | 0.5 | 256MiB | Python HTTP, SQLite WAL 영속 journal |
| 합계 / OS 등 여유 | 3.5 / 0.5 | 3840 / 1280MiB | 앱 증설·Redis·추가 DB 금지 |

Hikari maximumPoolSize10, minimumIdle2, connectionTimeout1000ms, validationTimeout500ms. 단일 앱 기준 DB 연결10 + 관측/관리 여유30. 풀 크기·DB 내구성·CPU/RAM은 구현자가 늘릴 수 없다. 동일 한도 내 쿼리/인덱스/스레드 최적화는 허용하며 변경 이력을 남긴다.

부하 발생기(k6)는 별도 호스트 권장(2CPU/1GiB). 로컬 실행은 대상 서비스 예산 밖의 추가 자원이 필요하며 호스트 경합을 기록한다. Docker build/Gradle도 측정 부하와 동시에 실행하지 않는다. 서로 다른 arm의 서버를 동시에 띄워 비교하지 않는다.

DB seed는 사용자1,000,000명(각 잔액1,000,000원), 대행사1,000개(각100bps). ID는 순차 bigint, 난수 seed는 workload에 고정. seed SQL은 setup에서만 실행하고 count/min/max/sum으로 증명한다. 측정 전 ANALYZE, 워밍업 종료 후 고정 부하 시작. 각 시나리오의 잔액·은행 journal은 새 프로젝트 volume에서 초기화한다.

I/O 사전 계측: DB SELECT 1, 사용자 PK 조회, 은행 health, 은행 POST transfer, 은행 GET transfer를 단발로 기록한 뒤 각각1,000회 반복한 p50/p95도 기록한다. 단발시간으로 처리량을 예측하지 않는다. 복합 결제 latency와 DB pool 대기·CPU throttle·디스크 fsync 지연을 따로 수집한다.

## AWS 월10만원 가정

2026-09-23 확인한 [Lightsail 공식 bundle 표](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)에 Linux/public IPv4 2vCPU·4GB가 월24USD로 표시된다. 두 대면 월48USD(각80GB SSD)이며, **가정 환율1,400원/USD × 부가세1.1 = 73,920원**이다. 환율은 실측 현재값이 아닌 예산용 가정이다. 데이터 초과·백업·스냅샷·별도 k6 머신 비용은 추가다. 한 대 API/fake, 한 대 DB로 할당하는 소규모 실험 예시이며 구매/배포는 하지 않는다.

이는 4개의 전용 코어 보장 상품이 아니다. [Lightsail CPU burst 문서](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html)의 baseline/credit 제약 때문에 짧은 로컬 Docker 벤치마크를 한 달 지속 AWS 처리량으로 일반화할 수 없다. 실제 AWS 측정 시 region·아키텍처·credit·네트워크 RTT·EBS 성능·월 견적을 다시 고정한다. CPU4/RAM5GiB **서비스별** 배치를 원하면 다른 예산 프로필로 세 조건 모두 다시 실행한다.

## 버전 동결

초기 빌드는 검증 가능한 Spring Boot3.5.5 / Kotlin2.2.0 / Gradle8.14.4 / Java21로 고정한다. 최신 버전이라는 의미가 아니다. [Spring Boot3.5 공식 요건](https://docs.spring.io/spring-boot/3.5/system-requirements.html)을 참조하고 실제 빌드로 확인한다. 실험 시작 시 Docker tag뿐 아니라 pull 결과 digest, Java build, Gradle wrapper SHA를 manifest에 기록한다. 준비 도중 tag 이동 가능성이 있으므로 baseline 태그만으로 이미지까지 고정되었다고 주장하지 않는다.


# 외부 합격 기준

정상 결제50RPS·5분 p95<500ms/p99<1500ms, 예상치 못한5xx<0.1%, dropped_iterations=0.
동일키 동시100개·동일사용자 다른키100개·정산 동시50개에서 중복처리와 잔액음수가 없어야 합니다.
은행 응답유실·app 재시작·DB 장애 후 거래는 장애 해제30초 이내 수렴해야 합니다.
워밍업30초/10RPS, spike100RPS/30초→50RPS/60초, soak30RPS/15분에서 누수·교착·OOM 없이 회복해야 합니다.
DB/은행 전수 대조, KRW 건별 수수료·KST 경계·대행사 권한·영속 멱등성을 검증합니다.
레이어 분리·은행 교체성·상태/거래경계·메서드 응집성·오류테스트·추상화 적정성은 각0..2점, 총10/12 이상 및0점 항목 없음이 기준입니다.
```

### 1. 에이전트 응답 전문

```text
먼저 STARTER.md, FIXTURE.md와 현재 서버·DB 구조를 확인하겠습니다. 그다음 결제와 정산을 영속 상태 전이로 구현하고, 은행 장애·동시성·재시작 경로를 테스트하겠습니다.

현재 서버에는 health만 있고 업무 테이블은 없습니다. 결제 요청 시 사용자 잔액의 사용 가능 금액을 DB에서 예약하고, 은행 결과가 확정되면 같은 DB 트랜잭션에서 차감·원장·결제 상태를 반영하겠습니다. 응답이 유실되면 저장된 은행 키로 조회한 뒤 복구하도록 만들겠습니다.

결제·정산 스키마와 서비스 코드를 추가했습니다. 이제 컴파일 오류와 실제 DB 마이그레이션을 확인하고, 이어서 은행 fault와 동시 요청을 통합 검증하겠습니다.

실제 DB 마이그레이션과 은행 연동으로 정상 결제 201, 동일 키 재전송 200, 정산 집계 201을 확인했습니다. 다음으로 거절 응답 형식과 입력 검증을 다듬고, 동시성·응답 유실 복구 테스트를 추가하겠습니다.

통합 테스트 4개가 통과했습니다. 동일 키 100건, 동일 사용자 다른 키 100건, 은행 거절·응답 유실·5xx 복구, 정산 동시 50건을 실제 DB와 은행에 연결해 검증했습니다. 마지막으로 프로세스 재시작 뒤 PENDING 복구를 확인하고 데이터 대조용 SQL을 문서화하겠습니다.

재실행 가능한 통합 테스트도 다시 통과했습니다. 수동으로는 응답 유실 결제를 202로 남긴 직후 JVM을 종료하고 재시작해 최종 성공까지 확인했습니다. 이 재시작 경로를 별도 회귀 스크립트로 남기고 최종 검증을 마무리하겠습니다.

재시작 회귀 스크립트가 통과했습니다. 정산에는 추가로 KST 날짜 경계와 늦게 성공 확정된 결제가 다음 정산 snapshot에 들어가는 테스트를 넣었습니다. 전체 통합 테스트를 다시 실행하겠습니다.

복구 처리량을 검토하다가 대기 결제를 한 번에 10건씩 순차 처리하면 장애 후 backlog에서 30초 수렴 목표를 놓칠 수 있음을 확인했습니다. DB 풀 한도는 유지하면서 복구 작업만 제한된 워커 큐로 처리하도록 조정하고 다시 검증하겠습니다.

결제·정산 v1을 구현했습니다. 은행 연동을 [BankPort](/workspace/src/main/kotlin/lab/payment/application/BankPort.kt)로 분리했고, [결제 서비스](/workspace/src/main/kotlin/lab/payment/application/PaymentService.kt)에 잔액 예약·확정 차감·영속 복구를, [마이그레이션](/workspace/src/main/resources/db/migration/V1__payment_business.sql)에 멱등성 및 정산 제약을 추가했습니다. 테이블 의미와 전수 대조 SQL은 [IMPLEMENTATION.md](/workspace/IMPLEMENTATION.md)에 정리했습니다. 공통 fixture와 DB 풀 한도는 변경하지 않았습니다.

**검증:** `RUN_INTEGRATION=true ./gradlew --no-daemon test bootJar`에서 단위 테스트 2개와 실제 DB·은행 통합 테스트 5개가 모두 통과했습니다. 동일 키 100건, 동일 사용자 다른 키 100건, 정산 동시 50건, 은행 장애와 늦은 확정을 포함합니다. [재시작 회귀 스크립트](/workspace/scripts/restart-recovery.sh)도 통과했습니다.

실행한 검증에서 남은 실패는 없습니다. 다만 **DB 컨테이너 중단 후 복구**와 고정 k6 성능·SLO는 실행하지 않아 합격을 주장할 수 없습니다. Docker 이미지 pull digest도 이 환경에서 확인할 수 없으며 [매니페스트](/workspace/EXPERIMENT_MANIFEST.md)에 기록했습니다. 통합 테스트가 현재 DB 데이터를 변경했으므로 측정은 새 volume에서 시작해야 합니다.
```

## 1. 결과물 분석

```text
# 첫 구현 결과 분석 — 제외된 파일럿

요청: 상세 계약 기반 Kotlin 결제/정산 구현. 응답 전문은 response-001.txt에 보존했다.

- 외부 CLI exit 0, configured_model gpt-6-sol/high. backend model ID는 미관측.
- 입력 2,172,364 / 출력 30,590 / 합계 2,202,954 tokens. 캐시 읽기 2,110,848은 입력에 포함된다.
- 모델 자체 보고: 단위/통합 검증 성공. 자체 보고와 외부 판정은 구분한다.
- 외부 Docker 새 DB/bank에서 기존 payments.js 실행 exit 0, 28/28 checks, 129 requests. 기능 시나리오만 검증했으며 전체 C01~C20 및 부하 SLO 완료가 아니다.
- 계획의 본 시행 상한 200만보다 첫 턴 사용량이 크다. 파일럿은 본 시행 통계에서 제외하고, 본 시행은 기존 상한과 초과분 기록 규칙을 유지한다.
- 후속 작업: 전체 판정기 검증 및 독립 Fable 리뷰. 미완료를 성공으로 처리하지 않는다.
```

### 증거 파일

```text
payment-settlement/codex/.runs/pilot-detailed-01/turns/0001/result.json
```

SHA-256: 410bb4e282be3b5a3eb46e3a087b624703f120690d77aa2f27d008a43ed46200 · 인코딩: utf-8

```text
{
  "thread_id": "01a0ce5d-80ec-7ce0-a126-e2533a4072a7",
  "turn_completed_count": 1,
  "reported_models": [],
  "actual_model": null,
  "actual_model_note": "turn_context confirms client configuration, not the backend model identity.",
  "last_cumulative_usage": {
    "input_tokens": 2172364,
    "cached_input_tokens": 2110848,
    "cache_write_input_tokens": 0,
    "output_tokens": 30590,
    "reasoning_output_tokens": 10629
  },
  "model_contexts": [
    {
      "model": "gpt-6-sol",
      "effort": "high"
    }
  ],
  "configured_model": "gpt-6-sol",
  "exit_code": 0,
  "finished_at": "2026-09-23T13:24:01.751234+00:00"
}
```

### 증거 파일

```text
payment-settlement/codex/docs/pilots/detailed-01/usage-summary.json
```

SHA-256: f9c2d995888533d44c1543500f2479cc7137bdde5ef652f6e71101175e1e6003 · 인코딩: utf-8

```text
{
  "run_id": "pilot-detailed-01",
  "event_count": 1,
  "input_tokens": 2172364,
  "output_tokens": 30590,
  "total_tokens": 2202954,
  "cached_input_tokens": 2110848,
  "cache_write_input_tokens": 0,
  "reasoning_tokens": 10629,
  "actual_cost_usd": null,
  "cost_source": null,
  "model_labels": [
    "gpt-6-sol"
  ],
  "actual_models": [],
  "actual_model_missing_events": 1
}
```

## 2. 프롬프트 전문

UTC: 2026-09-25T12:45:01.081552+00:00 · 역할: implementer · 세션: 01a0ce5d-80ec-7ce0-a126-e2533a4072a7 · 상태: VALIDATING

```text
제외된 상세프롬프트 파일럿의 외부 검증 피드백입니다. 같은 계약을 유지하고 아래 결함을 수정하고 회귀 검증하세요. 실제 테스트 실행/미실행을 구분하고 실패원인과 해결을 기록하세요. 다른 에이전트/goal은 사용하지 마세요.

1. KST 정산 오류: POST payments user900001 agency904 amount101/199/10001 각각201 SUCCEEDED, DB succeeded_at=2026-09-23T23:44:13Z. POST settlements agency904 businessDate2026-09-24 응답201이지만 gross/fee/net/count 모두0. 외부 경계 fixture succeeded_at=2020-01-01T14:59:59Z/15:00:00Z/15:00:01Z 세건 agency920에서 1월1일 정산3건306, 1월2일0건으로 관측됐습니다. Asia/Seoul 성공확정 날짜와 정확히 일치하도록 수정하고 실제 PostgreSQL에서 경계 회귀검증하세요.
2. 독립 Fable 정적 리뷰8/12로 기준10/12 미달. application이 api DTO를 import하고 HTTP status Pair를 반환하는 역의존, payment same-payload 비교와 HTTP코드분기 중복, settlement의 명세상 업무결정이 repository 한메서드에 집중된 점을 지적했습니다. 레이어 경계/메서드 응집성과 중복을 개선하세요. 과도한 추상화는 피하세요.
3. 추가 검증 공백: fake BankPort 서비스테스트, DB확정 실패 후 복구, 403 tenant불일치,404 미존재 user/agency, 지속 은행 장애. 기존 restart-recovery.sh는 리뷰입력에서 누락됐으므로 없는 것으로 간주하지 않겠습니다.
4. 확정tx 예외 시 수락된 결제의 결과불명 응답 계약(202/id) 검토. 일반 잘못된 path/method가503이 되는 오류매핑도 검토하세요. FAILED422 오류body 계약은 기존 code/message를 유지하세요; reviewer 제안을 무조건 새요구사항으로 취급하지 마세요.

현재 사용자 데이터/업무데이터는 파일럿용이며 기존 실행으로 변경되어 있습니다. 테스트는 충돌하지 않는 namespace와 fixture를 사용하세요. 외부 판정기/공통fixture를 수정하지 마세요. 완료 후 변경파일, 실행명령과 결과, 남은 검증을 보고하세요.
```

### 2. 에이전트 응답 전문

```text
피드백의 KST 경계부터 실제 PostgreSQL 표현식으로 재현하겠습니다. 이어서 DTO·HTTP 상태가 application에 들어온 경계를 정리하고, 수락 후 DB 확정 실패와 지속 은행 장애를 회귀 테스트로 검증하겠습니다. 테스트 데이터는 기존 파일럿 행과 겹치지 않게 만들겠습니다.

원인을 실제 DB에서 재현했습니다. PostgreSQL이 `date AT TIME ZONE 'Asia/Seoul'`을 기대한 KST 자정의 UTC 시각이 아니라 `09:00`으로 평가했고, 경계 시각 `2020-01-01T15:00:00Z`도 범위 밖으로 판정했습니다. KST 날짜 경계를 Java에서 명시적인 UTC 시각으로 계산해 `timestamptz`에 바인딩하겠습니다.

KST 회귀 테스트가 실제 PostgreSQL에서 통과했습니다. 경계 1초 전은 1월 1일, 정확한 경계와 1초 후는 1월 2일로 정산되고, 제보된 23:44Z 결제 3건도 다음 KST 날짜에 총 10,301원으로 집계됩니다. 다음은 가짜 은행 port로 DB 확정 실패와 지속 장애의 결과불명 동작을 확인하겠습니다.

가짜 BankPort를 주입한 서비스 통합 테스트가 통과했습니다. DB 확정 단계 예외를 주입했을 때 요청이 영속 paymentId와 202 PENDING을 반환하고, 이후 재조회로 한 번만 차감되는지 확인했습니다. 지속적인 은행 결과불명도 예약 상태를 유지하다 장애 해제 후 성공으로 수렴했습니다. 403·404·잘못된 경로·메서드 응답도 실제 HTTP로 통과했습니다.

전체 테스트는 한 차례 통과했습니다. 다만 테스트 컨텍스트가 동시에 살아 있으면 서로의 PENDING 행을 복구할 수 있어, 각 통합 테스트 클래스 종료 시 컨텍스트를 닫도록 격리했습니다. 기존 업무 행은 건드리지 않도록 새 테스트는 미사용 대행사와 사용자·고유 키를 선택하고, 경계 fixture는 트랜잭션에서 롤백합니다. 이 최종 상태로 다시 실행하겠습니다.

최종 빌드는 컴파일을 마치고 통합 테스트를 실행 중입니다. 실제 은행을 쓰는 테스트와 가짜 port 장애 테스트를 분리해 실행한 결과를 확인한 뒤, 재시작 스크립트도 최종 빌드로 다시 돌리겠습니다.

외부 피드백의 결함을 수정하고 회귀 검증했습니다.

- **KST 정산:** PostgreSQL에서 기존 `date AT TIME ZONE` 경계가 잘못 계산되는 것을 재현했습니다. [SettlementService.kt](/workspace/src/main/kotlin/lab/payment/application/SettlementService.kt)가 서울 날짜의 양쪽 자정을 UTC 시각으로 계산하고, [PaymentRepository.kt](/workspace/src/main/kotlin/lab/payment/infrastructure/PaymentRepository.kt)가 이를 `timestamptz` 범위로 조회하도록 바꿨습니다. 실제 PostgreSQL 경계 테스트에서 15:00:00Z 전후 날짜 구분과 제보된 23:44Z 결제 3건의 **총액 10,301원·수수료 102원·3건** 집계를 확인했습니다.
- **레이어와 응답:** [Commands.kt](/workspace/src/main/kotlin/lab/payment/application/Commands.kt)를 추가해 application의 API DTO 의존과 HTTP 상태 반환을 제거했습니다. [PaymentService.kt](/workspace/src/main/kotlin/lab/payment/application/PaymentService.kt)의 payload 비교를 한곳으로 모았고, 정산 단계도 서비스와 저장소 메서드로 나눴습니다. [PaymentApi.kt](/workspace/src/main/kotlin/lab/payment/api/PaymentApi.kt)는 확정 DB 처리 실패 후 영속 ID를 포함한 202를 반환하며, 잘못된 경로·메서드는 404·405로 매핑합니다. 은행 거절 422의 `{code,message}`는 유지했습니다.

**실행 결과:** 최종 트리에서 `RUN_INTEGRATION=true ./gradlew --no-daemon test bootJar`가 **11개 통과, 실패·건너뜀 0개**였고, `bash scripts/restart-recovery.sh`도 통과했습니다. 추가 테스트는 [PostgreSQL 경계](/workspace/src/test/kotlin/lab/payment/SettlementBoundaryIntegrationTest.kt), [가짜 BankPort·확정 실패·지속 장애](/workspace/src/test/kotlin/lab/payment/PaymentRecoveryIntegrationTest.kt), [403·404·경로 오류](/workspace/src/test/kotlin/lab/payment/PaymentIntegrationTest.kt)에 있습니다. 경계용 합성 행은 롤백하고, 영속 테스트는 미사용 대행사·사용자와 고유 키를 사용했습니다.

초기 가짜 port 테스트는 Kotlin/Mockito matcher의 `NullPointerException`으로 **테스트 코드에서** 실패했으며 matcher를 수정한 뒤 통과했습니다. 원인·수정·실행 기록은 [REVIEW_FIXES.md](/workspace/REVIEW_FIXES.md)에 남겼습니다. 실제 DB·은행 컨테이너 중단/재시작, 고정 k6 부하, 전수 은행 대조와 독립 Fable 재평가는 실행하지 않았습니다.
```

## 2. 결과물 분석

```text
# 파일럿 수정 결과

동일 thread resume exit0. 두 번째 턴 사용량은 누적값 차분으로 input3,967,027/output28,308, 합계3,995,335. 두 턴 누적6,198,289 tokens이며 CLI값과 응답별 session audit이 일치한다.

실제 코드 변경: KST 날짜 경계를 application에서 Instant로 계산하여 PostgreSQL timestamptz 파라미터로 전달, application→api DTO 역의존 제거, 금액/멱등 로직 모듈화, fake bank 및 경계/오류 테스트 추가. 모델은 단위/통합11개와 restart script 통과를 보고했다. 외부verify2에서 C01~C12,C15,C16,C18의 HTTP+DB+은행 대조 통과를 확인했다. 장애/KST 후속 증거는 verification-02 폴더 참조. 부하와 최종 본 실험 완료를 의미하지 않는다.

문제: Kotlin/Mockito matcher 사용 중 테스트 NPE가 발생했고 matcher 수정 뒤 모델 테스트가 통과했다. 자체 기록 REVIEW_FIXES.md를 보존한다. Fable은 최초코드에1회 실행했으며 수정본을 재리뷰한 것으로 표시하지 않는다.
```

### 증거 파일

```text
payment-settlement/codex/.runs/pilot-detailed-01/turns/0002/result.json
```

SHA-256: 456805fe996cde9e4b17f171c1dbb8da0429041010853e78ed107f2e35117d1f · 인코딩: utf-8

```text
{
  "thread_id": "01a0ce5d-80ec-7ce0-a126-e2533a4072a7",
  "turn_completed_count": 1,
  "reported_models": [],
  "actual_model": null,
  "actual_model_note": "turn_context confirms client configuration, not the backend model identity.",
  "last_cumulative_usage": {
    "input_tokens": 6139391,
    "cached_input_tokens": 5960064,
    "cache_write_input_tokens": 0,
    "output_tokens": 58898,
    "reasoning_output_tokens": 21892
  },
  "model_contexts": [
    {
      "model": "gpt-6-sol",
      "effort": "high"
    },
    {
      "model": "gpt-6-sol",
      "effort": "high"
    }
  ],
  "session_usage_audit": {
    "semantics": "unique_response_id",
    "response_count": 90,
    "session_count": 1,
    "usage": {
      "input_tokens": 6139391,
      "cached_input_tokens": 5960064,
      "cache_write_input_tokens": 0,
      "output_tokens": 58898,
      "reasoning_output_tokens": 21892,
      "total_tokens": 6198289
    },
    "per_thread": {
      "01a0ce5d-80ec-7ce0-a126-e2533a4072a7": {
        "input_tokens": 6139391,
        "cached_input_tokens": 5960064,
        "cache_write_input_tokens": 0,
        "output_tokens": 58898,
        "reasoning_output_tokens": 21892,
        "total_tokens": 6198289
      }
    },
    "configured_models_verified": true,
    "backend_model_verified": false,
    "worker_session_observed": false,
    "note": "Session presence alone does not prove all spawned workers were collected."
  },
  "configured_model": "gpt-6-sol",
  "exit_code": 0,
  "finished_at": "2026-09-25T11:39:24.657948+00:00"
}
```

### 증거 파일

```text
payment-settlement/codex/docs/pilots/detailed-01/usage-summary.json
```

SHA-256: bcdb24c1b2fa7b7545223ae0623e6f80ee22bbb151d6550bb8380bee5d961a8e · 인코딩: utf-8

```text
{
  "run_id": "pilot-detailed-01",
  "event_count": 2,
  "input_tokens": 6139391,
  "output_tokens": 58898,
  "total_tokens": 6198289,
  "cached_input_tokens": 5960064,
  "cache_write_input_tokens": 0,
  "reasoning_tokens": 21892,
  "actual_cost_usd": null,
  "cost_source": null,
  "model_labels": [
    "gpt-6-sol"
  ],
  "actual_models": [],
  "actual_model_missing_events": 2
}
```

