package lab.payment.application

import lab.payment.api.PaymentRequest
import lab.payment.domain.BusinessError
import lab.payment.domain.Payment
import lab.payment.domain.feeFor
import lab.payment.infrastructure.PaymentRepository
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Service
import org.springframework.transaction.support.TransactionTemplate
import jakarta.annotation.PreDestroy
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.Semaphore

@Service
class PaymentService(private val repo: PaymentRepository, private val tx: TransactionTemplate, private val bank: BankPort) {
    private val recoverySlots = Semaphore(32)
    private val recoveryWorkers = Executors.newFixedThreadPool(8) { task ->
        Thread(task, "payment-recovery").apply { isDaemon = true }
    }

    fun create(agencyId: Long, key: String, request: PaymentRequest, fault: String?): Pair<Payment, Int> {
        val prior = repo.paymentByKey(agencyId, key)
        if (prior != null) {
            if (prior.userId != request.userId || prior.amount != request.amount || request.currency != "KRW")
                throw BusinessError("IDEMPOTENCY_CONFLICT", 409, "Key already used with another payment")
            return prior to when (prior.status) { "PENDING" -> 202; "FAILED" -> 422; else -> 200 }
        }
        val userId = request.userId ?: throw BusinessError("INVALID_USER", 400, "userId is required")
        val amount = request.amount ?: throw BusinessError("INVALID_AMOUNT", 400, "amount is required")
        if (userId <= 0 || amount !in 1..1_000_000 || request.currency != "KRW")
            throw BusinessError("INVALID_REQUEST", 400, "Invalid payment fields")
        if (fault != null && fault !in setOf("decline", "timeout-before", "timeout-after", "5xx"))
            throw BusinessError("INVALID_BANK_FAULT", 400, "Invalid bank fault")
        val result = tx.execute {
            val existing = repo.paymentByKey(agencyId, key)
            if (existing != null) return@execute existing to false
            val feeBps = repo.agencyFee(agencyId) ?: throw BusinessError("AGENCY_NOT_FOUND", 404, "Agency not found")
            if (!repo.userExists(userId)) throw BusinessError("USER_NOT_FOUND", 404, "User not found")
            val id = UUID.randomUUID()
            val p = Payment(id, userId, agencyId, key, "payment:$agencyId:$id", amount,
                feeBps, feeFor(amount, feeBps), "PENDING", null, null)
            if (!repo.insertPayment(p)) return@execute (repo.paymentByKey(agencyId, key)!! to false)
            if (!repo.reserve(userId, amount)) throw BusinessError("INSUFFICIENT_FUNDS", 409, "Insufficient balance")
            p to true
        }!!
        val p = result.first
        if (p.userId != userId || p.amount != amount || request.currency != "KRW")
            throw BusinessError("IDEMPOTENCY_CONFLICT", 409, "Key already used with another payment")
        if (!result.second) return p to when (p.status) { "PENDING" -> 202; "FAILED" -> 422; else -> 200 }
        val outcome = bank.transfer(p, fault)
        return finish(p, outcome, true)
    }

    fun get(agencyId: Long, id: UUID): Payment = repo.paymentById(agencyId, id)
        ?: throw BusinessError("PAYMENT_NOT_FOUND", 404, "Payment not found")

    private fun finish(p: Payment, outcome: BankResult, first: Boolean): Pair<Payment, Int> {
        tx.execute {
            when (outcome) {
                is BankResult.Succeeded -> repo.succeed(p, outcome.transferId)
                BankResult.Declined -> repo.fail(p)
                else -> repo.retryLater(p.id)
            }
        }
        val current = repo.paymentById(p.id)!!
        val code = when (current.status) { "SUCCEEDED" -> if (first) 201 else 200; "FAILED" -> 422; else -> 202 }
        return current to code
    }

    @Scheduled(fixedDelay = 250)
    fun recover() {
        val available = recoverySlots.availablePermits()
        if (available == 0) return
        val ids = try { repo.claimDue(available) } catch (_: Exception) { return }
        for (id in ids) {
            if (!recoverySlots.tryAcquire()) continue
            recoveryWorkers.execute {
                try {
                    val p = repo.paymentById(id) ?: return@execute
                    if (p.status != "PENDING") return@execute
                    val lookup = bank.lookup(p)
                    val result = if (lookup == BankResult.Absent) bank.transfer(p) else lookup
                    finish(p, result, false)
                } catch (_: Exception) { /* lease expires; a later cycle retries */ }
                finally { recoverySlots.release() }
            }
        }
    }

    @PreDestroy
    fun shutdownRecovery() { recoveryWorkers.shutdownNow() }
}
