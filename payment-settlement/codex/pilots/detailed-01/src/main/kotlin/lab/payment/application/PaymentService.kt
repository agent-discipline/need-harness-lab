package lab.payment.application

import lab.payment.domain.BusinessError
import lab.payment.domain.Payment
import lab.payment.domain.feeFor
import lab.payment.infrastructure.PaymentRepository
import org.slf4j.LoggerFactory
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Service
import org.springframework.transaction.support.TransactionTemplate
import jakarta.annotation.PreDestroy
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.Semaphore

@Service
class PaymentService(private val repo: PaymentRepository, private val tx: TransactionTemplate, private val bank: BankPort) {
    private val log = LoggerFactory.getLogger(PaymentService::class.java)
    private val recoverySlots = Semaphore(32)
    private val recoveryWorkers = Executors.newFixedThreadPool(8) { task ->
        Thread(task, "payment-recovery").apply { isDaemon = true }
    }

    fun create(agencyId: Long, key: String, command: PaymentCommand, fault: String?): PaymentCreation {
        val prior = repo.paymentByKey(agencyId, key)
        if (prior != null) {
            requireSamePayload(prior, command)
            return PaymentCreation(prior, false)
        }
        if (command.userId <= 0 || command.amount !in 1..1_000_000 || command.currency != "KRW")
            throw BusinessError("INVALID_REQUEST", "Invalid payment fields")
        if (fault != null && fault !in setOf("decline", "timeout-before", "timeout-after", "5xx"))
            throw BusinessError("INVALID_BANK_FAULT", "Invalid bank fault")
        val result = tx.execute {
            val existing = repo.paymentByKey(agencyId, key)
            if (existing != null) return@execute existing to false
            val feeBps = repo.agencyFee(agencyId) ?: throw BusinessError("AGENCY_NOT_FOUND", "Agency not found")
            if (!repo.userExists(command.userId)) throw BusinessError("USER_NOT_FOUND", "User not found")
            val id = UUID.randomUUID()
            val p = Payment(id, command.userId, agencyId, key, "payment:$agencyId:$id", command.amount,
                feeBps, feeFor(command.amount, feeBps), "PENDING", null, null)
            if (!repo.insertPayment(p)) return@execute (repo.paymentByKey(agencyId, key)!! to false)
            if (!repo.reserve(command.userId, command.amount)) throw BusinessError("INSUFFICIENT_FUNDS", "Insufficient balance")
            p to true
        }!!
        val p = result.first
        requireSamePayload(p, command)
        if (!result.second) return PaymentCreation(p, false)
        val outcome = try { bank.transfer(p, fault) } catch (_: Exception) { BankResult.Unknown }
        // The reservation is committed. A failed finalization must retain the durable ID and uncertainty.
        val final = try { finish(p, outcome) } catch (ex: Exception) {
            log.warn("Payment {} finalization uncertain; scheduled recovery will retry", p.id, ex)
            p
        }
        return PaymentCreation(final, true)
    }

    fun get(agencyId: Long, id: UUID): Payment = repo.paymentById(agencyId, id)
        ?: throw BusinessError("PAYMENT_NOT_FOUND", "Payment not found")

    private fun requireSamePayload(payment: Payment, command: PaymentCommand) {
        if (payment.userId != command.userId || payment.amount != command.amount || command.currency != "KRW")
            throw BusinessError("IDEMPOTENCY_CONFLICT", "Key already used with another payment")
    }

    private fun finish(p: Payment, outcome: BankResult): Payment {
        tx.execute {
            when (outcome) {
                is BankResult.Succeeded -> repo.succeed(p, outcome.transferId)
                BankResult.Declined -> repo.fail(p)
                else -> repo.retryLater(p.id)
            }
        }
        return repo.paymentById(p.id)!!
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
                    finish(p, result)
                } catch (ex: Exception) {
                    log.warn("Payment {} recovery attempt failed; lease will expire", id, ex)
                }
                finally { recoverySlots.release() }
            }
        }
    }

    @PreDestroy
    fun shutdownRecovery() { recoveryWorkers.shutdownNow() }
}
