package lab.payment

import com.fasterxml.jackson.databind.ObjectMapper
import lab.payment.infrastructure.PaymentRepository
import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.jdbc.core.JdbcTemplate
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration
import java.util.UUID
import java.util.concurrent.ThreadLocalRandom
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@EnabledIfEnvironmentVariable(named = "RUN_INTEGRATION", matches = "true")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class PaymentIntegrationTest {
    @LocalServerPort var port = 0
    @Autowired lateinit var mapper: ObjectMapper
    @Autowired lateinit var db: JdbcTemplate
    @Autowired lateinit var repo: PaymentRepository
    private val http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build()

    private fun call(method: String, path: String, agency: Long, key: String? = null, body: String? = null,
                     fault: String? = null): HttpResponse<String> {
        val req = HttpRequest.newBuilder(URI.create("http://localhost:$port$path"))
            .timeout(Duration.ofSeconds(8)).header("X-Agency-Id", agency.toString())
            .apply { if (key != null) header("Idempotency-Key", key) }
            .apply { if (fault != null) header("X-Bank-Fault", fault) }
            .apply { if (body != null) header("Content-Type", "application/json") }
            .method(method, if (body == null) HttpRequest.BodyPublishers.noBody() else HttpRequest.BodyPublishers.ofString(body))
            .build()
        return http.send(req, HttpResponse.BodyHandlers.ofString())
    }

    private fun paymentBody(user: Long, agency: Long, amount: Long) =
        """{"userId":$user,"agencyId":$agency,"amount":$amount,"currency":"KRW"}"""
    private fun id(response: HttpResponse<String>) = mapper.readTree(response.body()).path("paymentId").asText()
    private fun freshUser(): Long {
        repeat(30) {
            val candidate = ThreadLocalRandom.current().nextLong(100_000, 800_000)
            val available = db.queryForObject(
                "SELECT balance_minor-reserved_minor FROM users WHERE id=?", Long::class.java, candidate)
            if (available == 1_000_000L) return candidate
        }
        error("Could not find an untouched fixture user")
    }
    private fun eventually(agency: Long, id: String): String {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30)
        while (System.nanoTime() < deadline) {
            val response = call("GET", "/api/payments/$id", agency)
            if (mapper.readTree(response.body()).path("status").asText() == "SUCCEEDED") return response.body()
            Thread.sleep(250)
        }
        fail<String>("Payment $id did not converge within 30 seconds")
        error("unreachable")
    }

    @Test
    fun `same key concurrency, idempotency, and tenant isolation`() {
        val key = "same-${UUID.randomUUID()}"
        val user = freshUser()
        val body = paymentBody(user, 999, 101)
        val pool = Executors.newFixedThreadPool(32)
        try {
            val futures = (1..100).map { pool.submit<HttpResponse<String>> { call("POST", "/api/payments", 999, key, body) } }
            val responses = futures.map { it.get(20, TimeUnit.SECONDS) }
            assertTrue(responses.all { it.statusCode() in setOf(200, 201, 202) }, responses.groupingBy { it.statusCode() }.eachCount().toString())
            val ids = responses.map { id(it) }.toSet()
            assertEquals(1, ids.size)
            eventually(999, ids.single())
            assertEquals(1L, db.queryForObject("SELECT count(*) FROM ledger WHERE payment_id=?", Long::class.java, UUID.fromString(ids.single())))
            assertEquals(404, call("GET", "/api/payments/${ids.single()}", 998).statusCode())
            assertEquals(409, call("POST", "/api/payments", 999, key, paymentBody(user, 999, 102)).statusCode())
            assertEquals(400, call("POST", "/api/payments", 999, "bad-${UUID.randomUUID()}",
                """{"userId":$user,"agencyId":999,"amount":1.5,"currency":"KRW"}""").statusCode())
        } finally { pool.shutdownNow() }
    }

    @Test
    fun `different keys cannot overdraw same user`() {
        val prefix = UUID.randomUUID().toString()
        val user = freshUser()
        val pool = Executors.newFixedThreadPool(32)
        try {
            val futures = (1..100).map { n -> pool.submit<HttpResponse<String>> {
                call("POST", "/api/payments", 999, "$prefix-$n", paymentBody(user, 999, 1_000_000))
            } }
            val responses = futures.map { it.get(20, TimeUnit.SECONDS) }
            assertEquals(1, responses.count { it.statusCode() in setOf(200, 201, 202) },
                responses.groupingBy { it.statusCode() }.eachCount().toString())
            val winner = responses.first { it.statusCode() in setOf(200, 201, 202) }
            eventually(999, id(winner))
            assertEquals(0L, db.queryForObject("SELECT balance_minor FROM users WHERE id=?", Long::class.java, user))
            assertEquals(0L, db.queryForObject("SELECT reserved_minor FROM users WHERE id=?", Long::class.java, user))
        } finally { pool.shutdownNow() }
    }

    @Test
    fun `bank faults preserve uncertainty and recover with one ledger entry`() {
        val declineKey = "decline-${UUID.randomUUID()}"
        val declinedUser = freshUser()
        val decline = call("POST", "/api/payments", 999, declineKey, paymentBody(declinedUser, 999, 100), "decline")
        assertEquals(422, decline.statusCode(), decline.body())
        assertEquals("BANK_DECLINED", mapper.readTree(decline.body()).path("code").asText())
        assertEquals(1_000_000L, db.queryForObject("SELECT balance_minor FROM users WHERE id=?", Long::class.java, declinedUser))
        assertEquals(0L, db.queryForObject("SELECT reserved_minor FROM users WHERE id=?", Long::class.java, declinedUser))

        for (fault in listOf("timeout-after", "timeout-before", "5xx")) {
            val user = freshUser()
            val key = "$fault-${UUID.randomUUID()}"
            val response = call("POST", "/api/payments", 999, key, paymentBody(user, 999, 100), fault)
            assertEquals(202, response.statusCode(), response.body())
            val paymentId = id(response)
            eventually(999, paymentId)
            assertEquals(1L, db.queryForObject("SELECT count(*) FROM ledger WHERE payment_id=?", Long::class.java, UUID.fromString(paymentId)))
            assertEquals(999_900L, db.queryForObject("SELECT balance_minor FROM users WHERE id=?", Long::class.java, user))
        }
    }

    @Test
    fun `settlement is a snapshot and never counts a payment twice`() {
        val date = db.queryForObject("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date", java.sql.Date::class.java)!!.toLocalDate()
        val payment = call("POST", "/api/payments", 999, "settle-pay-${UUID.randomUUID()}", paymentBody(freshUser(), 999, 101))
        assertEquals(201, payment.statusCode(), payment.body())
        val pool = Executors.newFixedThreadPool(20)
        try {
            val prefix = UUID.randomUUID().toString()
            val futures = (1..50).map { n -> pool.submit<HttpResponse<String>> {
                call("POST", "/api/settlements", 999, "$prefix-$n", """{"agencyId":999,"businessDate":"$date"}""")
            } }
            val results = futures.map { it.get(20, TimeUnit.SECONDS) }
            assertTrue(results.all { it.statusCode() == 201 }, results.groupingBy { it.statusCode() }.eachCount().toString())
            val paymentId = UUID.fromString(id(payment))
            assertEquals(1L, db.queryForObject("SELECT count(*) FROM settlement_items WHERE payment_id=?", Long::class.java, paymentId))
            assertEquals(0L, db.queryForObject("SELECT count(*) FROM payments WHERE id=? AND settlement_id IS NULL", Long::class.java, paymentId))
            val totalNet = db.queryForObject("SELECT COALESCE(sum(net_amount),0) FROM settlements WHERE agency_id=999", Long::class.java)!!
            val balance = db.queryForObject("SELECT settlement_balance_minor FROM agencies WHERE id=999", Long::class.java)!!
            assertEquals(totalNet, balance)
        } finally { pool.shutdownNow() }
    }

    @Test
    fun `late bank confirmation belongs to a later KST settlement snapshot`() {
        val date = db.queryForObject("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date", java.sql.Date::class.java)!!.toLocalDate()
        val converted = db.queryForObject(
            "SELECT ('2026-09-22 15:00:00+00'::timestamptz AT TIME ZONE 'Asia/Seoul')::date",
            java.sql.Date::class.java)!!.toLocalDate()
        assertEquals(java.time.LocalDate.of(2026, 9, 23), converted)

        val payment = call("POST", "/api/payments", 997, "late-${UUID.randomUUID()}",
            paymentBody(freshUser(), 997, 101), "timeout-after")
        assertEquals(202, payment.statusCode(), payment.body())
        val body = """{"agencyId":997,"businessDate":"$date"}"""
        val first = call("POST", "/api/settlements", 997, "early-${UUID.randomUUID()}", body)
        assertEquals(201, first.statusCode(), first.body())
        assertEquals(0L, mapper.readTree(first.body()).path("paymentCount").asLong())
        eventually(997, id(payment))
        val second = call("POST", "/api/settlements", 997, "later-${UUID.randomUUID()}", body)
        assertEquals(201, second.statusCode(), second.body())
        assertEquals(1L, mapper.readTree(second.body()).path("paymentCount").asLong())
        assertEquals(101L, mapper.readTree(second.body()).path("grossAmount").asLong())
        assertEquals(1L, mapper.readTree(second.body()).path("feeAmount").asLong())
    }
}
