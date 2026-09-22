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
