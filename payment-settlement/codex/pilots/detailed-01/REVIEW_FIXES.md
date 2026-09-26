# Excluded pilot feedback: causes, fixes, and verification

## KST settlement defect

The old claim query used `?::date AT TIME ZONE 'Asia/Seoul'`. PostgreSQL implicitly converted the date before applying `AT TIME ZONE`; an actual diagnostic query for `2020-01-02` returned `2020-01-02 09:00:00` in the UTC session, and rejected `2020-01-01T15:00:00Z` from the lower bound. This selected the wrong KST day. `SettlementService` now computes Seoul midnight and the next midnight, converts both to UTC `OffsetDateTime`, and the repository binds both explicitly as `timestamptz`. The range is inclusive at the start and exclusive at the end.

`SettlementBoundaryIntegrationTest` uses the real PostgreSQL database, inserts uniquely keyed synthetic successful payments in a transaction that rolls back, then invokes the actual settlement service. Its assertions cover `2020-01-01T14:59:59Z` (Jan 1 KST), `15:00:00Z` and `15:00:01Z` (Jan 2 KST), plus three payments at `2026-09-23T23:44:13Z` (Sep 24 KST): gross 10,301, fee 102, net 10,199, count 3.

## Layering and failure responses

The controller maps `PaymentRequest` to an application `PaymentCommand` and maps application creation results to HTTP codes. Services no longer import API DTOs or return HTTP status pairs. The one `requireSamePayload` check handles both ordinary replay and insert races. Settlement service now chooses the snapshot window and transaction steps; repository methods separately insert, claim, aggregate, update totals, and credit the agency. `BusinessError` carries an application code and message; the API maps codes to HTTP statuses. The 422 bank decline still returns `{code,message}`.

If the initial bank call has returned but the payment finalization transaction or follow-up read fails, `PaymentService` returns durable `PENDING` with 202 and the original payment ID. The recovery lease remains in the database and the same bank key is reused. Unknown paths and unsupported methods now return 404 and 405 respectively instead of the generic 503 mapping.

## Executed verification

- `RUN_INTEGRATION=true ./gradlew --no-daemon test --tests lab.payment.SettlementBoundaryIntegrationTest`: passed against PostgreSQL 16.
- `RUN_INTEGRATION=true ./gradlew --no-daemon test --tests lab.payment.PaymentRecoveryIntegrationTest`: passed after fixing the test's Kotlin/Mockito matcher setup. The first attempt failed before exercising application code with `NullPointerException: any(...) must not be null`; the matcher helper fixed that test harness error.
- `RUN_INTEGRATION=true ./gradlew --no-daemon test --tests 'lab.payment.PaymentIntegrationTest.tenant input existence and route errors keep their contract'`: passed over HTTP.
- `RUN_INTEGRATION=true ./gradlew --no-daemon test bootJar`: final tree passed, 11 tests, zero skipped/failures/errors (two domain, six HTTP/infrastructure, two simulated fault, one PostgreSQL KST boundary); `bootJar` built successfully.
- `bash scripts/restart-recovery.sh`: final tree passed; payment `c39337f1-9eb3-49de-9f72-a79ec6d058aa` converged after JVM kill and restart.

The fake bank tests replace `BankPort` in the Spring context. One injects an exception at the database finalization method and verifies 202/ID, one ledger entry, and later convergence. Another holds bank lookup/transfer at `Unknown` for more than five seconds, verifies no debit and a retained reservation, then releases the fault and verifies convergence. These are simulated faults, not a real DB or bank container stop.

## Not run here

The actual PostgreSQL container stop/restart and persistent bank container stop/restart were not performed because they would affect the shared pilot services. The fixed k6 workload, full bank journal reconciliation, and independent Fable re-score were not run. Existing pilot data remain in place; measured workload checks still require their prescribed fresh volumes.
