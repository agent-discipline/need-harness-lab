package lab.payment.infrastructure

import lab.payment.domain.Payment
import lab.payment.domain.Settlement
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.time.LocalDate
import java.util.UUID

@Repository
class PaymentRepository(private val db: JdbcTemplate) {
    private fun payment(rs: ResultSet) = Payment(
        rs.getObject("id", UUID::class.java), rs.getLong("user_id"), rs.getLong("agency_id"),
        rs.getString("idempotency_key"), rs.getString("bank_key"), rs.getLong("amount_minor"),
        rs.getInt("fee_bps"), rs.getLong("fee_minor"), rs.getString("status"),
        rs.getString("failure_code"), rs.getString("bank_transfer_id")
    )
    private fun settlement(rs: ResultSet) = Settlement(
        rs.getObject("id", UUID::class.java), rs.getLong("agency_id"), rs.getString("idempotency_key"),
        rs.getObject("business_date", LocalDate::class.java), rs.getString("status"),
        rs.getLong("gross_amount"), rs.getLong("fee_amount"), rs.getLong("net_amount"), rs.getLong("payment_count")
    )

    fun agencyFee(id: Long): Int? = db.query("SELECT fee_bps FROM agencies WHERE id=?", { rs, _ -> rs.getInt(1) }, id).firstOrNull()
    fun userExists(id: Long): Boolean = db.query("SELECT 1 FROM users WHERE id=?", { _, _ -> true }, id).isNotEmpty()
    fun paymentByKey(agencyId: Long, key: String): Payment? =
        db.query("SELECT * FROM payments WHERE agency_id=? AND idempotency_key=?", { rs, _ -> payment(rs) }, agencyId, key).firstOrNull()
    fun paymentById(agencyId: Long, id: UUID): Payment? =
        db.query("SELECT * FROM payments WHERE agency_id=? AND id=?", { rs, _ -> payment(rs) }, agencyId, id).firstOrNull()
    fun paymentById(id: UUID): Payment? =
        db.query("SELECT * FROM payments WHERE id=?", { rs, _ -> payment(rs) }, id).firstOrNull()

    fun insertPayment(p: Payment): Boolean = db.update(
        """INSERT INTO payments(id,agency_id,user_id,idempotency_key,bank_key,amount_minor,currency,fee_bps,fee_minor,status,processing_until)
           VALUES (?,?,?,?,?,?,'KRW',?,?,'PENDING',clock_timestamp()+interval '4 seconds')
           ON CONFLICT (agency_id,idempotency_key) DO NOTHING""".trimIndent(),
        p.id, p.agencyId, p.userId, p.key, p.bankKey, p.amount, p.feeBps, p.fee
    ) == 1

    fun reserve(userId: Long, amount: Long): Boolean = db.update(
        "UPDATE users SET reserved_minor=reserved_minor+? WHERE id=? AND balance_minor-reserved_minor>=?",
        amount, userId, amount
    ) == 1

    fun succeed(p: Payment, transferId: String): Boolean {
        val updated = db.update(
            """UPDATE payments SET status='SUCCEEDED', bank_transfer_id=?, succeeded_at=clock_timestamp()
               WHERE id=? AND status='PENDING'""".trimIndent(), transferId, p.id
        )
        if (updated == 0) return false
        db.update("UPDATE users SET balance_minor=balance_minor-?, reserved_minor=reserved_minor-? WHERE id=?",
            p.amount, p.amount, p.userId)
        db.update("INSERT INTO ledger(payment_id,user_id,amount_minor) VALUES (?,?,?)", p.id, p.userId, p.amount)
        return true
    }

    fun fail(p: Payment): Boolean {
        val updated = db.update("UPDATE payments SET status='FAILED',failure_code='BANK_DECLINED' WHERE id=? AND status='PENDING'", p.id)
        if (updated == 0) return false
        db.update("UPDATE users SET reserved_minor=reserved_minor-? WHERE id=?", p.amount, p.userId)
        return true
    }

    fun retryLater(id: UUID) {
        db.update("UPDATE payments SET processing_until=clock_timestamp()+interval '2 seconds' WHERE id=? AND status='PENDING'", id)
    }

    fun claimDue(limit: Int): List<UUID> = db.query(
        """UPDATE payments SET processing_until=clock_timestamp()+interval '10 seconds'
           WHERE id IN (SELECT id FROM payments WHERE status='PENDING' AND processing_until<=clock_timestamp()
                        ORDER BY processing_until LIMIT ? FOR UPDATE SKIP LOCKED)
           RETURNING id""".trimIndent(), { rs, _ -> rs.getObject(1, UUID::class.java)
        }, limit)

    fun settlementByKey(agencyId: Long, key: String): Settlement? =
        db.query("SELECT * FROM settlements WHERE agency_id=? AND idempotency_key=?", { rs, _ -> settlement(rs) }, agencyId, key).firstOrNull()
    fun settlementById(agencyId: Long, id: UUID): Settlement? =
        db.query("SELECT * FROM settlements WHERE agency_id=? AND id=?", { rs, _ -> settlement(rs) }, agencyId, id).firstOrNull()
    fun lockAgency(id: Long): Boolean = db.query("SELECT id FROM agencies WHERE id=? FOR UPDATE", { _, _ -> true }, id).isNotEmpty()

    fun createSettlement(id: UUID, agencyId: Long, key: String, date: LocalDate) {
        db.update("""INSERT INTO settlements(id,agency_id,idempotency_key,business_date,status,cutoff_at)
                   VALUES (?,?,?,?,'SUCCEEDED',clock_timestamp())""".trimIndent(), id, agencyId, key, date)
        db.update("""WITH claimed AS (
            UPDATE payments SET settlement_id=? WHERE id IN (
                SELECT id FROM payments WHERE agency_id=? AND status='SUCCEEDED' AND settlement_id IS NULL
                  AND succeeded_at >= (?::date AT TIME ZONE 'Asia/Seoul')
                  AND succeeded_at < ((?::date + 1) AT TIME ZONE 'Asia/Seoul')
                  AND succeeded_at <= (SELECT cutoff_at FROM settlements WHERE id=?)
                FOR UPDATE SKIP LOCKED
            ) RETURNING id,amount_minor,fee_minor)
            INSERT INTO settlement_items(settlement_id,payment_id,amount_minor,fee_minor)
            SELECT ?,id,amount_minor,fee_minor FROM claimed""".trimIndent(), id, agencyId, date, date, id, id)
        db.update("""UPDATE settlements SET gross_amount=x.gross, fee_amount=x.fee,
            net_amount=x.gross-x.fee, payment_count=x.cnt
            FROM (SELECT COALESCE(SUM(amount_minor),0) gross,COALESCE(SUM(fee_minor),0) fee,COUNT(*) cnt
                  FROM settlement_items WHERE settlement_id=?) x WHERE id=?""".trimIndent(), id, id)
        db.update("UPDATE agencies SET settlement_balance_minor=settlement_balance_minor+(SELECT net_amount FROM settlements WHERE id=?) WHERE id=?", id, agencyId)
    }
}
