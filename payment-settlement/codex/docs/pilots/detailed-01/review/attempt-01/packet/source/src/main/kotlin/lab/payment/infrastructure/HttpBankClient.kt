package lab.payment.infrastructure

import com.fasterxml.jackson.databind.ObjectMapper
import lab.payment.application.BankPort
import lab.payment.application.BankResult
import lab.payment.domain.Payment
import org.springframework.beans.factory.annotation.Value
import org.springframework.stereotype.Component
import java.net.URI
import java.net.URLEncoder
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.time.Duration

@Component
class HttpBankClient(
    @param:Value("\${bank.base-url}") private val baseUrl: String,
    @Value("\${bank.timeout-ms:1000}") timeoutMs: Long,
    private val mapper: ObjectMapper
) : BankPort {
    private val timeout = Duration.ofMillis(timeoutMs)
    private val client = HttpClient.newBuilder().connectTimeout(timeout).build()

    override fun transfer(payment: Payment, fault: String?): BankResult {
        val body = mapper.writeValueAsString(mapOf(
            "paymentId" to payment.id.toString(), "userId" to payment.userId,
            "agencyId" to payment.agencyId, "amount" to payment.amount, "currency" to "KRW"
        ))
        val request = HttpRequest.newBuilder(URI.create("$baseUrl/transfers"))
            .timeout(timeout).header("Content-Type", "application/json")
            .header("Idempotency-Key", payment.bankKey)
            .apply { if (fault != null) header("X-Fault", fault) }
            .POST(HttpRequest.BodyPublishers.ofString(body)).build()
        return send(request, false)
    }

    override fun lookup(payment: Payment): BankResult {
        val encoded = URLEncoder.encode(payment.bankKey, StandardCharsets.UTF_8)
        val request = HttpRequest.newBuilder(URI.create("$baseUrl/transfers/$encoded"))
            .timeout(timeout).GET().build()
        return send(request, true)
    }

    private fun send(request: HttpRequest, lookup: Boolean): BankResult = try {
        val response = client.send(request, HttpResponse.BodyHandlers.ofString())
        when (response.statusCode()) {
            200 -> {
                val json = mapper.readTree(response.body())
                if (json.path("status").asText() == "SUCCEEDED" && json.path("bankTransferId").asText().isNotBlank())
                    BankResult.Succeeded(json.path("bankTransferId").asText()) else BankResult.Unknown
            }
            404 -> if (lookup) BankResult.Absent else BankResult.Unknown
            422 -> BankResult.Declined
            else -> BankResult.Unknown
        }
    } catch (_: Exception) { BankResult.Unknown }
}
