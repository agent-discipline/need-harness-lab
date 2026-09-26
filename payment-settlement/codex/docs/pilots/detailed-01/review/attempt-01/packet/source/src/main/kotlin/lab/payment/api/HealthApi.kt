package lab.payment.api

import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController

/** Infrastructure smoke endpoint; no payment behavior is supplied. */
@RestController
class HealthApi(private val jdbc: JdbcTemplate) {
    @GetMapping("/health")
    fun health(): Map<String, Any> {
        val database = try { jdbc.queryForObject("SELECT 1", Int::class.java) == 1 }
                       catch (_: Exception) { false }
        return mapOf("status" to if (database) "UP" else "DOWN", "database" to database)
    }
}
