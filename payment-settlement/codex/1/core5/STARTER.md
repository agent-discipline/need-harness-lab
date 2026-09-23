# 공통 초기 서버

Java21 / Kotlin2.2.0 / Spring Boot3.5.5 / Gradle8.14.4. `./gradlew test bootJar`로 빌드한다. Postgres 접속은 DB_URL/DB_USER/DB_PASSWORD, 은행은 BANK_BASE_URL 환경변수로 제공한다.

업무 코드는 api/application/domain/infrastructure 패키지의 레이어드 구조로 구현한다. 초기 코드는 DB 연결을 확인하는 GET /health뿐이다. 업무 테이블은 db/migration 아래 Flyway V1부터 추가한다. 공통 users/agencies는 인프라 seed가 생성한다. 테스트와 문서는 이 프로젝트 안에 작성한다.

공통 고정 한도: 앱1.5CPU/1536MiB, JVM heap768MiB, Hikari max10/min2, DB max_connections40. 은행 port8081 HTTP, PostgreSQL16. 공개 연동 규격은 FIXTURE.md를 참조한다. 요구사항·판정기는 평가자가 해당 실험 조건에 맞게 전달한다.
