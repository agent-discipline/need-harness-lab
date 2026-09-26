package lab.payment

import lab.payment.application.SettlementService
import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.transaction.annotation.Transactional
import org.springframework.test.annotation.DirtiesContext
import java.time.LocalDate
import java.time.OffsetDateTime
import java.util.UUID

@EnabledIfEnvironmentVariable(named = "RUN_INTEGRATION", matches = "true")
@SpringBootTest
@Transactional // All synthetic payment and settlement rows roll back; fixture rows remain unchanged.
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class SettlementBoundaryIntegrationTest {
    @Autowired lateinit var db: JdbcTemplate
    @Autowired lateinit var settlements: SettlementService

    @Test
    fun `PostgreSQL settlement assigns exact UTC boundary and late evening to KST dates`() {
        val agency = db.queryForObject(
            """SELECT a.id FROM agencies a WHERE NOT EXISTS
               (SELECT 1 FROM payments p WHERE p.agency_id=a.id) ORDER BY a.id DESC LIMIT 1""".trimIndent(), Long::class.java)!!
        val user = db.queryForObject(
            "SELECT id FROM users WHERE balance_minor=1000000 AND reserved_minor=0 ORDER BY id DESC LIMIT 1",
            Long::class.java)!!
        val run = UUID.randomUUID().toString()
        fun succeeded(amount: Long, time: String): UUID {
            val id = UUID.randomUUID()
            val fee = amount / 100
            db.update("""INSERT INTO payments(id,agency_id,user_id,idempotency_key,bank_key,
                amount_minor,currency,fee_bps,fee_minor,status,bank_transfer_id,succeeded_at,processing_until)
                VALUES (?,?,?,?,?,?,'KRW',100,?,'SUCCEEDED',?,?,clock_timestamp())""".trimIndent(),
                id, agency, user, "$run-$id", "test:$run:$id", amount, fee,
                "test-$id", OffsetDateTime.parse(time))
            db.update("UPDATE users SET balance_minor=balance_minor-? WHERE id=?", amount, user)
            db.update("INSERT INTO ledger(payment_id,user_id,amount_minor) VALUES (?,?,?)", id, user, amount)
            return id
        }

        val before = succeeded(101, "2020-01-01T14:59:59Z")
        val exact = succeeded(102, "2020-01-01T15:00:00Z")
        val after = succeeded(103, "2020-01-01T15:00:01Z")
        val late = listOf(101L, 199L, 10001L).map { succeeded(it, "2026-09-23T23:44:13Z") }

        val jan1 = settlements.create(agency, "$run-jan1", LocalDate.of(2020, 1, 1)).settlement
        val jan2 = settlements.create(agency, "$run-jan2", LocalDate.of(2020, 1, 2)).settlement
        val sep23 = settlements.create(agency, "$run-sep23", LocalDate.of(2026, 9, 23)).settlement
        val sep24 = settlements.create(agency, "$run-sep24", LocalDate.of(2026, 9, 24)).settlement

        assertEquals(listOf(101L, 1L, 100L, 1L), listOf(jan1.gross, jan1.fee, jan1.net, jan1.count))
        assertEquals(listOf(205L, 2L, 203L, 2L), listOf(jan2.gross, jan2.fee, jan2.net, jan2.count))
        assertEquals(0L, sep23.count)
        assertEquals(listOf(10301L, 102L, 10199L, 3L), listOf(sep24.gross, sep24.fee, sep24.net, sep24.count))
        assertEquals(jan1.id, settlementOf(before))
        assertEquals(jan2.id, settlementOf(exact))
        assertEquals(jan2.id, settlementOf(after))
        late.forEach { assertEquals(sep24.id, settlementOf(it)) }
    }

    private fun settlementOf(payment: UUID): UUID? = db.queryForObject(
        "SELECT settlement_id FROM payments WHERE id=?", UUID::class.java, payment)
}
