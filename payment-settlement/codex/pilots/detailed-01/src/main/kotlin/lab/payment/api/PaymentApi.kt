package lab.payment.api

import lab.payment.application.PaymentService
import lab.payment.application.SettlementService
import lab.payment.application.PaymentCommand
import lab.payment.domain.BusinessError
import lab.payment.domain.Payment
import lab.payment.domain.Settlement
import lab.payment.domain.validateKey
import org.springframework.http.HttpStatusCode
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*
import org.springframework.web.servlet.resource.NoResourceFoundException
import org.springframework.web.HttpRequestMethodNotSupportedException
import org.springframework.web.servlet.NoHandlerFoundException
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
    ?: throw BusinessError("INVALID_AGENCY", "X-Agency-Id must be a positive integer")

@RestController
@RequestMapping("/api")
class PaymentApi(private val payments: PaymentService, private val settlements: SettlementService) {
    @PostMapping("/payments")
    fun createPayment(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                      @RequestHeader("Idempotency-Key", required = false) keyHeader: String?,
                      @RequestHeader("X-Bank-Fault", required = false) fault: String?,
                      @RequestBody request: PaymentRequest): ResponseEntity<PaymentBody> {
        val agencyId = agency(agencyHeader)
        if (request.agencyId == null) throw BusinessError("INVALID_AGENCY", "agencyId is required")
        if (request.agencyId != agencyId) throw BusinessError("AGENCY_MISMATCH", "Agency does not match request")
        val command = PaymentCommand(
            request.userId ?: throw BusinessError("INVALID_USER", "userId is required"),
            request.amount ?: throw BusinessError("INVALID_AMOUNT", "amount is required"),
            request.currency ?: throw BusinessError("INVALID_REQUEST", "currency is required")
        )
        val result = payments.create(agencyId, validateKey(keyHeader), command, fault)
        val payment = result.payment
        if (payment.status == "FAILED") throw BusinessError("BANK_DECLINED", "Bank declined transfer")
        val status = when (payment.status) { "PENDING" -> 202; else -> if (result.created) 201 else 200 }
        return ResponseEntity.status(HttpStatusCode.valueOf(status)).body(payment.body())
    }

    @GetMapping("/payments/{id}")
    fun getPayment(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                   @PathVariable id: UUID): PaymentBody = payments.get(agency(agencyHeader), id).body()

    @PostMapping("/settlements")
    fun createSettlement(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                         @RequestHeader("Idempotency-Key", required = false) keyHeader: String?,
                         @RequestBody request: SettlementRequest): ResponseEntity<SettlementBody> {
        val agencyId = agency(agencyHeader)
        if (request.agencyId == null) throw BusinessError("INVALID_AGENCY", "agencyId is required")
        if (request.agencyId != agencyId) throw BusinessError("AGENCY_MISMATCH", "Agency does not match request")
        val date = try { LocalDate.parse(request.businessDate ?: "") }
                   catch (_: DateTimeParseException) { throw BusinessError("INVALID_DATE", "businessDate must be YYYY-MM-DD") }
        val result = settlements.create(agencyId, validateKey(keyHeader), date)
        return ResponseEntity.status(HttpStatusCode.valueOf(if (result.created) 201 else 200)).body(result.settlement.body())
    }

    @GetMapping("/settlements/{id}")
    fun getSettlement(@RequestHeader("X-Agency-Id", required = false) agencyHeader: String?,
                      @PathVariable id: UUID): SettlementBody = settlements.get(agency(agencyHeader), id).body()
}

@RestControllerAdvice
class ApiErrors {
    @ExceptionHandler(BusinessError::class)
    fun business(ex: BusinessError): ResponseEntity<ErrorBody> {
        val status = when (ex.code) {
            "AGENCY_MISMATCH" -> 403
            "AGENCY_NOT_FOUND", "USER_NOT_FOUND", "PAYMENT_NOT_FOUND", "SETTLEMENT_NOT_FOUND" -> 404
            "INSUFFICIENT_FUNDS", "IDEMPOTENCY_CONFLICT" -> 409
            "BANK_DECLINED" -> 422
            else -> 400
        }
        return ResponseEntity.status(status).body(ErrorBody(ex.code, ex.message ?: ex.code))
    }

    @ExceptionHandler(NoResourceFoundException::class, NoHandlerFoundException::class)
    fun missingPath(): ResponseEntity<ErrorBody> =
        ResponseEntity.status(404).body(ErrorBody("NOT_FOUND", "Resource not found"))

    @ExceptionHandler(HttpRequestMethodNotSupportedException::class)
    fun wrongMethod(): ResponseEntity<ErrorBody> =
        ResponseEntity.status(405).body(ErrorBody("METHOD_NOT_ALLOWED", "Method not allowed"))

    @ExceptionHandler(Exception::class)
    fun unexpected(ex: Exception): ResponseEntity<ErrorBody> {
        val invalid = ex is org.springframework.http.converter.HttpMessageNotReadableException ||
            ex is org.springframework.web.method.annotation.MethodArgumentTypeMismatchException
        return if (invalid) ResponseEntity.badRequest().body(ErrorBody("INVALID_REQUEST", "Invalid request"))
        else if (ex is org.springframework.dao.DataAccessException || ex is org.springframework.transaction.TransactionException)
            ResponseEntity.status(503).body(ErrorBody("SERVICE_UNAVAILABLE", "Service temporarily unavailable"))
        else ResponseEntity.status(500).body(ErrorBody("INTERNAL_ERROR", "Internal server error"))
    }
}
