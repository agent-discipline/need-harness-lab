package lab.payment

import lab.payment.domain.BusinessError
import lab.payment.domain.feeFor
import lab.payment.domain.validateKey
import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test

class DomainTest {
    @Test
    fun `fee is calculated per payment with integer floor`() {
        assertEquals(0L, feeFor(99, 100))
        assertEquals(1L, feeFor(101, 100))
        assertEquals(10_000L, feeFor(1_000_000, 100))
        assertEquals(0L, feeFor(99, 100) + feeFor(99, 100))
    }

    @Test
    fun `idempotency key is bounded printable ASCII`() {
        assertEquals("x".repeat(128), validateKey("x".repeat(128)))
        assertEquals("has space", validateKey("has space"))
        for (key in listOf(null, "", "x".repeat(129), "한글", "a\n")) {
            assertThrows(BusinessError::class.java) { validateKey(key) }
        }
    }
}
