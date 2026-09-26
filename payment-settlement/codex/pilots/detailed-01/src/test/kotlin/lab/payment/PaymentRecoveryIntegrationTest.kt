package lab.payment

import com.fasterxml.jackson.databind.ObjectMapper
import lab.payment.application.BankPort
import lab.payment.application.BankResult
import lab.payment.domain.Payment
import lab.payment.infrastructure.PaymentRepository
import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable
import org.mockito.Mockito
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.test.context.bean.override.mockito.MockitoBean
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean
import org.springframework.test.annotation.DirtiesContext
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration
import java.util.UUID
import java.util.concurrent.ThreadLocalRandom
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

@EnabledIfEnvironmentVariable(named = "RUN_INTEGRATION", matches = "true")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class PaymentRecoveryIntegrationTest {
    @LocalServerPort var port = 0
    @Autowired lateinit var db: JdbcTemplate
    @Autowired lateinit var mapper: ObjectMapper
    @MockitoBean lateinit var bank: BankPort
    @MockitoSpyBean lateinit var repo: PaymentRepository
    private val http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build()
    @Suppress("UNCHECKED_CAST")
    private fun <T> mockArg(): T { Mockito.any<T>(); return null as T }

    private fun freshUser(): Long {
        repeat(30) {
            val id = ThreadLocalRandom.current().nextLong(100_000, 800_000)
            if (db.queryForObject("SELECT balance_minor-reserved_minor FROM users WHERE id=?", Long::class.java, id) == 1_000_000L)
                return id
        }
        error("No untouched user")
    }
    private fun freshAgency(): Long = db.queryForObject(
        """SELECT a.id FROM agencies a WHERE NOT EXISTS
           (SELECT 1 FROM payments p WHERE p.agency_id=a.id) ORDER BY a.id DESC LIMIT 1""".trimIndent(),
        Long::class.java)!!

    private fun post(agency: Long, user: Long): HttpResponse<String> {
        val request = HttpRequest.newBuilder(URI.create("http://localhost:$port/api/payments"))
            .timeout(Duration.ofSeconds(5)).header("Content-Type", "application/json")
            .header("X-Agency-Id", agency.toString()).header("Idempotency-Key", "fake-${UUID.randomUUID()}")
            .POST(HttpRequest.BodyPublishers.ofString(
                """{"userId":$user,"agencyId":$agency,"amount":101,"currency":"KRW"}"""))
            .build()
        return http.send(request, HttpResponse.BodyHandlers.ofString())
    }

    private fun awaitSuccess(agency: Long, id: UUID) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30)
        while (System.nanoTime() < deadline) {
            val status = repo.paymentById(agency, id)?.status
            if (status == "SUCCEEDED") return
            Thread.sleep(200)
        }
        fail<Unit>("$id did not recover after fault release")
    }

    @Test
    fun `committed reservation returns 202 and durable id when finalization fails then recovers`() {
        val transferId = "fake-${UUID.randomUUID()}"
        Mockito.`when`(bank.transfer(mockArg(), Mockito.isNull()))
            .thenReturn(BankResult.Succeeded(transferId))
        Mockito.`when`(bank.lookup(mockArg()))
            .thenReturn(BankResult.Succeeded(transferId))
        Mockito.doThrow(IllegalStateException("injected finalization failure")).doCallRealMethod()
            .`when`(repo).succeed(mockArg(), mockArg())

        val user = freshUser()
        val agency = freshAgency()
        val response = post(agency, user)
        assertEquals(202, response.statusCode(), response.body())
        val body = mapper.readTree(response.body())
        assertEquals("PENDING", body.path("status").asText())
        val id = UUID.fromString(body.path("paymentId").asText())
        assertEquals("PENDING", repo.paymentById(agency, id)?.status)
        assertEquals(101L, db.queryForObject("SELECT reserved_minor FROM users WHERE id=?", Long::class.java, user))
        assertEquals(0L, db.queryForObject("SELECT count(*) FROM ledger WHERE payment_id=?", Long::class.java, id))
        awaitSuccess(agency, id)
        assertEquals(999_899L, db.queryForObject("SELECT balance_minor FROM users WHERE id=?", Long::class.java, user))
        assertEquals(0L, db.queryForObject("SELECT reserved_minor FROM users WHERE id=?", Long::class.java, user))
        assertEquals(1L, db.queryForObject("SELECT count(*) FROM ledger WHERE payment_id=?", Long::class.java, id))
        Mockito.verify(bank, Mockito.times(1)).transfer(mockArg(), Mockito.isNull())
    }

    @Test
    fun `persistent unknown bank result keeps reservation and converges after release`() {
        val available = AtomicBoolean(false)
        val transferId = "fake-${UUID.randomUUID()}"
        Mockito.`when`(bank.lookup(mockArg())).thenAnswer {
            if (available.get()) BankResult.Absent else BankResult.Unknown
        }
        Mockito.`when`(bank.transfer(mockArg(), Mockito.isNull())).thenAnswer {
            if (available.get()) BankResult.Succeeded(transferId) else BankResult.Unknown
        }

        val user = freshUser()
        val agency = freshAgency()
        val response = post(agency, user)
        assertEquals(202, response.statusCode(), response.body())
        val id = UUID.fromString(mapper.readTree(response.body()).path("paymentId").asText())
        Thread.sleep(5500)
        assertEquals("PENDING", repo.paymentById(agency, id)?.status)
        assertEquals(1_000_000L, db.queryForObject("SELECT balance_minor FROM users WHERE id=?", Long::class.java, user))
        assertEquals(101L, db.queryForObject("SELECT reserved_minor FROM users WHERE id=?", Long::class.java, user))
        assertEquals(0L, db.queryForObject("SELECT count(*) FROM ledger WHERE payment_id=?", Long::class.java, id))
        available.set(true)
        awaitSuccess(agency, id)
        assertEquals(1L, db.queryForObject("SELECT count(*) FROM ledger WHERE payment_id=?", Long::class.java, id))
        assertEquals(999_899L, db.queryForObject("SELECT balance_minor FROM users WHERE id=?", Long::class.java, user))
    }
}
