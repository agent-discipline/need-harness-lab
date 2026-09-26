package lab.payment.domain

import java.time.LocalDate
import java.util.UUID

data class Payment(
    val id: UUID, val userId: Long, val agencyId: Long, val key: String,
    val bankKey: String, val amount: Long, val feeBps: Int, val fee: Long,
    val status: String, val failureCode: String?, val bankTransferId: String?
)

data class Settlement(
    val id: UUID, val agencyId: Long, val key: String, val businessDate: LocalDate,
    val status: String, val gross: Long, val fee: Long, val net: Long, val count: Long
)

data class SettlementTotals(val gross: Long, val fee: Long, val count: Long) {
    val net: Long get() = gross - fee
}

class BusinessError(val code: String, message: String) : RuntimeException(message)

fun feeFor(amount: Long, feeBps: Int): Long = Math.multiplyExact(amount, feeBps.toLong()) / 10000L

fun validateKey(key: String?): String {
    if (key == null || key.length !in 1..128 || key.any { it.code !in 32..126 })
        throw BusinessError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 1..128 printable ASCII characters")
    return key
}
