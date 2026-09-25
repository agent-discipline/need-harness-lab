package lab.payment.application

import lab.payment.domain.BusinessError
import lab.payment.domain.Settlement
import lab.payment.infrastructure.PaymentRepository
import org.springframework.stereotype.Service
import org.springframework.transaction.support.TransactionTemplate
import java.time.LocalDate
import java.util.UUID

@Service
class SettlementService(private val repo: PaymentRepository, private val tx: TransactionTemplate) {
    fun create(agencyId: Long, key: String, date: LocalDate): Pair<Settlement, Int> = tx.execute {
        val existing = repo.settlementByKey(agencyId, key)
        if (existing != null) {
            if (!existing.businessDate.isEqual(date)) throw BusinessError("IDEMPOTENCY_CONFLICT", 409, "Key already used with another settlement")
            return@execute existing to 200
        }
        if (!repo.lockAgency(agencyId)) throw BusinessError("AGENCY_NOT_FOUND", 404, "Agency not found")
        val afterLock = repo.settlementByKey(agencyId, key)
        if (afterLock != null) {
            if (!afterLock.businessDate.isEqual(date)) throw BusinessError("IDEMPOTENCY_CONFLICT", 409, "Key already used with another settlement")
            return@execute afterLock to 200
        }
        val id = UUID.randomUUID()
        repo.createSettlement(id, agencyId, key, date)
        (repo.settlementById(agencyId, id)!! to 201)
    }!!

    fun get(agencyId: Long, id: UUID): Settlement = repo.settlementById(agencyId, id)
        ?: throw BusinessError("SETTLEMENT_NOT_FOUND", 404, "Settlement not found")
}
