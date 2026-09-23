package lab.payment.api

import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController

/** Infrastructure smoke endpoint; no payment behavior is supplied. */
@RestController
class HealthApi(private val jdbc: JdbcTemplate) {
    @GetMapping("/health")
    fun health(): Map<String, Any> = mapOf(
        "status" to "UP",
        "database" to (jdbc.queryForObject("SELECT 1", Int::class.java) == 1)
    )
}
