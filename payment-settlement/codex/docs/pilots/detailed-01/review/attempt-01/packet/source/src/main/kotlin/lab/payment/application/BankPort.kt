package lab.payment.application

import lab.payment.domain.Payment

sealed interface BankResult {
    data class Succeeded(val transferId: String) : BankResult
    data object Declined : BankResult
    data object Unknown : BankResult
    data object Absent : BankResult
}

interface BankPort {
    fun transfer(payment: Payment, fault: String? = null): BankResult
    fun lookup(payment: Payment): BankResult
}
