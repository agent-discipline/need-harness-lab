# Payment and settlement v1

## Boundaries and state

`PaymentApi` handles HTTP and tenant checks. `PaymentService` owns payment state changes and recovery. `SettlementService` owns settlement snapshots. `PaymentRepository` contains SQL. `BankPort` isolates the replaceable bank integration; `HttpBankClient` implements the fixture protocol. `BusinessError` is mapped to `{code,message}` without leaking SQL or stack traces.

The first payment transaction inserts a durable `PENDING` payment and reserves the user's available balance with a conditional `UPDATE`. Reservation does not debit the balance. Different keys for the same user compete on the `users` row, so their total reserved plus already debited amount cannot exceed the balance. The bank call occurs after commit. A successful bank response leads to one transaction that changes the payment to `SUCCEEDED`, debits the user, releases the reservation, and inserts one `ledger` row. A bank decline changes it to `FAILED` and releases the reservation without a debit. Unknown responses retain `PENDING`.

Each payment has one persistent bank key, `payment:<agencyId>:<payment UUID>`. Scheduled recovery leases due pending rows into a bounded queue (32 jobs, eight worker threads), checks the bank journal by that key, and sends a fault-free POST only when the journal says the key is absent. It runs after process restart. The unique bank key, unique ledger payment ID, and conditional payment status transition protect against duplicate effects. The initial HTTP request makes the bank call synchronously; ordinary success returns 201. Unknown outcomes return a durable 202. A replay of a final result returns 200 or 422.

Settlement runs in one database transaction after locking its agency. It records a cutoff, claims successful, unassigned payments for the requested KST business date, writes immutable `settlement_items`, sums each payment's stored fee, and credits `agencies.settlement_balance_minor` once. The `payments.settlement_id` and unique `settlement_items.payment_id` prevent inclusion twice. A new key may create an empty zero settlement. The account represents credited settlement value; unassigned payments remain unsettled obligations. There is no second bank payout in this version.

## Record-level reconciliation

`payments` is the operation and idempotency record. `ledger` records one actual user debit per successful payment. `settlements` records each immutable snapshot and credited total. `settlement_items` records the exact included payments and amounts. On a fresh fixture, the following queries should return no rows unless noted:

```sql
-- Every succeeded payment has exactly one matching user debit; others have none.
SELECT p.id FROM payments p LEFT JOIN ledger l ON l.payment_id=p.id
GROUP BY p.id,p.status HAVING count(l.id) <> CASE WHEN p.status='SUCCEEDED' THEN 1 ELSE 0 END;

-- Ledger amount and payment amount agree.
SELECT p.id FROM payments p JOIN ledger l ON l.payment_id=p.id
WHERE p.amount_minor<>l.amount_minor OR p.user_id<>l.user_id;

-- Current user balance plus committed debits equals the seeded million KRW.
SELECT u.id FROM users u LEFT JOIN ledger l ON l.user_id=u.id
GROUP BY u.id,u.balance_minor HAVING u.balance_minor+COALESCE(sum(l.amount_minor),0)<>1000000;

-- Pending reservations equal outstanding pending payments.
SELECT u.id FROM users u LEFT JOIN payments p ON p.user_id=u.id AND p.status='PENDING'
GROUP BY u.id,u.reserved_minor HAVING u.reserved_minor<>COALESCE(sum(p.amount_minor),0);

-- Every item belongs to a successful payment, once, with the recorded fee.
SELECT i.payment_id FROM settlement_items i JOIN payments p ON p.id=i.payment_id
WHERE p.status<>'SUCCEEDED' OR p.settlement_id<>i.settlement_id
   OR p.amount_minor<>i.amount_minor OR p.fee_minor<>i.fee_minor;

-- Settlement totals match its items.
SELECT s.id FROM settlements s LEFT JOIN settlement_items i ON i.settlement_id=s.id
GROUP BY s.id,s.gross_amount,s.fee_amount,s.net_amount,s.payment_count
HAVING s.gross_amount<>COALESCE(sum(i.amount_minor),0)
    OR s.fee_amount<>COALESCE(sum(i.fee_minor),0)
    OR s.net_amount<>COALESCE(sum(i.amount_minor-i.fee_minor),0)
    OR s.payment_count<>count(i.payment_id);

-- Credited agency balance equals all settled net amounts.
SELECT a.id FROM agencies a LEFT JOIN settlements s ON s.agency_id=a.id
GROUP BY a.id,a.settlement_balance_minor
HAVING a.settlement_balance_minor<>COALESCE(sum(s.net_amount),0);
```

For bank reconciliation, enumerate `payments.bank_key`, call `GET /transfers/{encoded bank_key}` for each key, and compare each `SUCCEEDED` payment's `bank_transfer_id` to the bank's ID. A `PENDING` payment may have a bank journal row while recovery is in progress; after convergence these should match. A `FAILED` payment must not have a bank journal row. The fixture bank owns its SQLite journal, so no local SQL can substitute for that HTTP check.

## Verification

`./gradlew test bootJar` runs domain tests and builds the server. `RUN_INTEGRATION=true ./gradlew test bootJar` additionally runs tests against the configured disposable PostgreSQL and fake bank. Those tests issue actual payments and permanently change that test volume; use a fresh volume for measured workloads. They cover 100 simultaneous same-key requests, 100 different keys on one user, bank decline and transient faults, tenant isolation, integer validation, and 50 concurrent settlements. `bash scripts/restart-recovery.sh` leaves a `timeout-after` payment pending, kills the JVM, starts a new JVM, and checks recovery through the bank journal within 30 seconds.

The fixed Hikari settings remain maximum 10, minimum 2, 1-second connection timeout, 500-ms validation timeout. No changes were made to fixture files or external evaluation resources. The fixed acceptance load profile and sustained performance thresholds require the external load generator and a fresh isolated volume; this implementation does not claim those numbers without that run.
