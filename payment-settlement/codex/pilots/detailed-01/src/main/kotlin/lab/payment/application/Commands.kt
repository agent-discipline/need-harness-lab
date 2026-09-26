package lab.payment.application

import lab.payment.domain.Payment
import lab.payment.domain.Settlement

data class PaymentCommand(val userId: Long, val amount: Long, val currency: String)
data class PaymentCreation(val payment: Payment, val created: Boolean)
data class SettlementCreation(val settlement: Settlement, val created: Boolean)
