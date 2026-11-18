"""Negative controls for the pilot-03 adapter's reconcile (same idea as evaluation/test_reconcile.py)."""
import copy, unittest
from adapter import reconcile

P = dict(id='p1', agency_id=1, user_id=7, idempotency_key='k', amount_minor=10001, currency='KRW', fee_bps=100, fee_minor=100,
         status='SUCCEEDED', bank_idempotency_key='payment:1:p1', bank_transfer_id='b1', succeeded_at='2026-11-18T01:00:00+00:00',
         business_date='2026-11-18', settlement_id='s1')
L = [dict(id=1, payment_id='p1', settlement_id=None, account='USER_BALANCE', account_ref=7, direction='DEBIT', amount_minor=10001),
     dict(id=2, payment_id='p1', settlement_id=None, account='AGENCY_PAYABLE', account_ref=1, direction='CREDIT', amount_minor=9901),
     dict(id=3, payment_id='p1', settlement_id=None, account='FEE_REVENUE', account_ref=1, direction='CREDIT', amount_minor=100),
     dict(id=4, payment_id=None, settlement_id='s1', account='AGENCY_PAYABLE', account_ref=1, direction='DEBIT', amount_minor=9901),
     dict(id=5, payment_id=None, settlement_id='s1', account='AGENCY_SETTLED', account_ref=1, direction='CREDIT', amount_minor=9901)]
S = dict(id='s1', agency_id=1, business_date='2026-11-18', gross_minor=10001, fee_minor=100, net_minor=9901, payment_count=1,
         created_at='2026-11-18T02:00:00+00:00')
I = dict(payment_id='p1', settlement_id='s1', amount_minor=10001, fee_minor=100, net_minor=9901)
B = dict(key='payment:1:p1', payload=dict(paymentId='p1', userId=7, agencyId=1, amount=10001, currency='KRW'), result=dict(bankTransferId='b1'))


class T(unittest.TestCase):
    def run_with(self, mutate=None):
        d = copy.deepcopy(dict(p=[P], l=L, s=[S], i=[I], b=[B]))
        if mutate: mutate(d)
        return reconcile(d['p'], d['l'], d['s'], d['i'], d['b'], {1: 100})

    def test_clean_passes(self): self.assertEqual(self.run_with(), [])
    def test_duplicate_debit(self): self.assertTrue(self.run_with(lambda d: d['l'].append(dict(d['l'][0], id=9))))
    def test_missing_ledger(self): self.assertTrue(self.run_with(lambda d: d['l'].pop(1)))
    def test_bank_payload_changed(self): self.assertTrue(self.run_with(lambda d: d['b'][0]['payload'].update(amount=1)))
    def test_missing_bank(self): self.assertTrue(self.run_with(lambda d: d['b'].clear()))
    def test_orphan_bank(self): self.assertTrue(self.run_with(lambda d: d['b'].append(dict(key='x', payload=dict(paymentId='zz'), result={}))))
    def test_wrong_settlement_total(self): self.assertTrue(self.run_with(lambda d: d['s'][0].update(net_minor=1)))
    def test_double_item(self): self.assertTrue(self.run_with(lambda d: d['i'].append(dict(d['i'][0]))))
    def test_wrong_fee(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(fee_minor=1)))
    def test_wrong_date(self): self.assertTrue(self.run_with(lambda d: d['s'][0].update(business_date='2026-11-17')))
    def test_failed_with_bank_record(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(status='FAILED')))
    def test_pending_flagged(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(status='PENDING')))


if __name__ == '__main__':
    unittest.main()
