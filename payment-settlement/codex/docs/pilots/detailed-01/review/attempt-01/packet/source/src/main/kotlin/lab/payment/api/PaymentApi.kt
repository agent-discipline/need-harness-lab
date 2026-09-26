package lab.payment.api

import lab.payment.application.PaymentService
import lab.payment.application.SettlementService
import lab.payment.domain.BusinessError
import lab.payment.domain.Payment
import lab.payment.domain.Settlement
import lab.payment.domain.validateKey
import org.springframework.http.HttpStatusCode
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*
import java.time.LocalDate
import java.time.format.DateTimeParseException
import java.util.UUID

data class PaymentRequest(val userId: Long?, val agencyId: Long?, val amount: Long?, val currency: String?)
data class SettlementRequest(val agencyId: Long?, val businessDate: String?)
data class ErrorBody(val code: String, val message: String)
data class PaymentBody(val paymentId: UUID, val userId: Long, val agencyId: Long, val amount: Long,
                       val currency: String, val status: String, val bankTransferId: String?)
data class SettlementBody(val settlementId: UUID, val agencyId: Long, val businessDate: LocalDate,
                          val status: String, val grossAmount: Long, val feeAmount: Long,
                          val netAmount: Long, val paymentCount: Long)

private fun Payment.body() = PaymentBody(id, userId, agencyId, amount, "KRW", status, bankTransferId)
private fun Settlement.body() = SettlementBody(id, agencyId, businessDate, status, gross, fee, net, count)
private fun agency(header: String?): Long = header?.toLongOrNull()?.takeIf { it > 0 }
    ?: throw BusinessError("INVALID_AGENCY", 400, "X-Agency-Id must be a positive integer")

@RestController
@RequestMapping("/api")
class PaymentApi(private val payments: PaymentService, private val settlements: SettlementService) {
    @PostMapping("/payments")
    fun createPayment(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                      @RequestHeader("Idempotency-Key", required = false) keyHeader: String?,
                      @RequestHeader("X-Bank-Fault", required = false) fault: String?,
                      @RequestBody request: PaymentRequest): ResponseEntity<PaymentBody> {
        val agencyId = agency(agencyHeader)
        if (request.agencyId == null) throw BusinessError("INVALID_AGENCY", 400, "agencyId is required")
        if (request.agencyId != agencyId) throw BusinessError("AGENCY_MISMATCH", 403, "Agency does not match request")
        val result = payments.create(agencyId, validateKey(keyHeader), request, fault)
        if (result.second == 422) throw BusinessError("BANK_DECLINED", 422, "Bank declined transfer")
        return ResponseEntity.status(HttpStatusCode.valueOf(result.second)).body(result.first.body())
    }

    @GetMapping("/payments/{id}")
    fun getPayment(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                   @PathVariable id: UUID): PaymentBody = payments.get(agency(agencyHeader), id).body()

    @PostMapping("/settlements")
    fun createSettlement(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                         @RequestHeader("Idempotency-Key", required = false) keyHeader: String?,
                         @RequestBody request: SettlementRequest): ResponseEntity<SettlementBody> {
        val agencyId = agency(agencyHeader)
        if (request.agencyId == null) throw BusinessError("INVALID_AGENCY", 400, "agencyId is required")
        if (request.agencyId != agencyId) throw BusinessError("AGENCY_MISMATCH", 403, "Agency does not match request")
        val date = try { LocalDate.parse(request.businessDate ?: "") }
                   catch (_: DateTimeParseException) { throw BusinessError("INVALID_DATE", 400, "businessDate must be YYYY-MM-DD") }
        val result = settlements.create(agencyId, validateKey(keyHeader), date)
        return ResponseEntity.status(HttpStatusCode.valueOf(result.second)).body(result.first.body())
    }

    @GetMapping("/settlements/{id}")
    fun getSettlement(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                      @PathVariable id: UUID): SettlementBody = settlements.get(agency(agencyHeader), id).body()
}

@RestControllerAdvice
class ApiErrors {
    @ExceptionHandler(BusinessError::class)
    fun business(ex: BusinessError): ResponseEntity<ErrorBody> =
        ResponseEntity.status(ex.httpStatus).body(ErrorBody(ex.code, ex.message ?: ex.code))

    @ExceptionHandler(Exception::class)
    fun unexpected(ex: Exception): ResponseEntity<ErrorBody> {
        val invalid = ex is org.springframework.http.converter.HttpMessageNotReadableException ||
            ex is org.springframework.web.method.annotation.MethodArgumentTypeMismatchException
        return if (invalid) ResponseEntity.badRequest().body(ErrorBody("INVALID_REQUEST", "Invalid request"))
        else ResponseEntity.status(503).body(ErrorBody("SERVICE_UNAVAILABLE", "Service temporarily unavailable"))
    }
}
