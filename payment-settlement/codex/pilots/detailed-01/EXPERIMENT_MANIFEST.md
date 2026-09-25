# Local pilot build manifest (2026-09-23 UTC)

- Spring Boot plugin: 3.5.5; Kotlin application plugin: 2.2.0; Gradle wrapper distribution: 8.14.4.
- Java runtime used for build and integration tests: Eclipse Temurin 21.0.11+10-LTS.
- Gradle wrapper JAR SHA-256: `7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172`.
- Dockerfile base tags: `eclipse-temurin:21-jdk` and `eclipse-temurin:21-jre`.
- Pulled Docker image digests: unavailable in this workspace because the Docker CLI and image metadata are not exposed here. The tags alone do not pin the image bytes. Record the pull-result digests of the API, PostgreSQL, and bank images in the external measurement manifest before running the fixed workload.
- Integration environment: PostgreSQL 16.14 at `db:5432`; fixture bank health at `bank:8081`.
- Runtime resource settings in this project: Hikari maximum 10/minimum 2, connection timeout 1000 ms, validation timeout 500 ms; Tomcat maximum 64; Docker entrypoint Xms256m/Xmx768m, ActiveProcessorCount=2. CPU/RAM container quotas and PostgreSQL settings remain controlled by the shared fixture.
