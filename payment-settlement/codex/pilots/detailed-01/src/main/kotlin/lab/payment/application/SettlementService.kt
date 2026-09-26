package lab.payment.application

import lab.payment.domain.BusinessError
import lab.payment.domain.Settlement
import lab.payment.infrastructure.PaymentRepository
import org.springframework.stereotype.Service
import org.springframework.transaction.support.TransactionTemplate
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.util.UUID

@Service
class SettlementService(private val repo: PaymentRepository, private val tx: TransactionTemplate) {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun create(agencyId: Long, key: String, date: LocalDate): SettlementCreation = tx.execute {
        val existing = repo.settlementByKey(agencyId, key)
        if (existing != null) {
            requireSameDate(existing, date)
            return@execute SettlementCreation(existing, false)
        }
        if (!repo.lockAgency(agencyId)) throw BusinessError("AGENCY_NOT_FOUND", "Agency not found")
        val afterLock = repo.settlementByKey(agencyId, key)
        if (afterLock != null) {
            requireSameDate(afterLock, date)
            return@execute SettlementCreation(afterLock, false)
        }
        val id = UUID.randomUUID()
        val start = OffsetDateTime.ofInstant(date.atStartOfDay(seoul).toInstant(), ZoneOffset.UTC)
        val end = OffsetDateTime.ofInstant(date.plusDays(1).atStartOfDay(seoul).toInstant(), ZoneOffset.UTC)
        repo.insertSettlement(id, agencyId, key, date)
        repo.claimSettlementPayments(id, agencyId, start, end)
        val totals = repo.settlementTotals(id)
        repo.updateSettlementTotals(id, totals)
        repo.creditAgency(agencyId, totals.net)
        SettlementCreation(repo.settlementById(agencyId, id)!!, true)
    }!!

    fun get(agencyId: Long, id: UUID): Settlement = repo.settlementById(agencyId, id)
        ?: throw BusinessError("SETTLEMENT_NOT_FOUND", "Settlement not found")

    private fun requireSameDate(settlement: Settlement, date: LocalDate) {
        if (!settlement.businessDate.isEqual(date))
            throw BusinessError("IDEMPOTENCY_CONFLICT", "Key already used with another settlement")
    }
}
