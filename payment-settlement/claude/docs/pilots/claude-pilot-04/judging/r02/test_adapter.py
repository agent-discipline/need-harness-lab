"""Negative controls for the pilot-04 round-01 adapter's reconcile (same idea as evaluation/test_reconcile.py)."""
import copy, unittest
from adapter import reconcile

P = dict(id='p1', agency_id=1, user_id=7, idempotency_key='k', amount_minor=10001, currency='KRW', fee_bps=100, fee_minor=100,
         net_minor=9901, status='SUCCEEDED', bank_transfer_id='b1', completed_at='2026-11-18T01:00:00+00:00', failure_code=None)
L = [dict(id=1, payment_id='p1', entry_type='AUTHORIZE', account_type='USER', account_id=7, amount_minor=-10001),
     dict(id=2, payment_id='p1', entry_type='AUTHORIZE', account_type='PAYMENT_CLEARING', account_id=0, amount_minor=10001),
     dict(id=3, payment_id='p1', entry_type='CAPTURE', account_type='PAYMENT_CLEARING', account_id=0, amount_minor=-10001),
     dict(id=4, payment_id='p1', entry_type='CAPTURE', account_type='AGENCY_SETTLEMENT', account_id=1, amount_minor=9901),
     dict(id=5, payment_id='p1', entry_type='CAPTURE', account_type='PLATFORM_FEE', account_id=0, amount_minor=100)]
B = dict(key='p1', payload=dict(paymentId='p1', userId=7, agencyId=1, amount=10001, currency='KRW'), result=dict(bankTransferId='b1'))
RELEASE = [dict(id=8, payment_id='p1', entry_type='RELEASE', account_type='PAYMENT_CLEARING', account_id=0, amount_minor=-10001),
           dict(id=9, payment_id='p1', entry_type='RELEASE', account_type='USER', account_id=7, amount_minor=10001)]


class T(unittest.TestCase):
    def run_with(self, mutate=None):
        d = copy.deepcopy(dict(p=[P], l=L, s=[], i=[], b=[B]))
        if mutate: mutate(d)
        return reconcile(d['p'], d['l'], d['s'], d['i'], d['b'], {1: 100})

    def failed(self, d):
        d['p'][0].update(status='FAILED', bank_transfer_id=None, failure_code='BANK_DECLINED'); d['b'].clear()

    def test_clean_passes(self): self.assertEqual(self.run_with(), [])
    def test_clean_failed_released_passes(self):
        def m(d): self.failed(d); d['l'][2:] = copy.deepcopy(RELEASE)
        self.assertEqual(self.run_with(m), [])
    def test_duplicate_debit(self): self.assertTrue(self.run_with(lambda d: d['l'].append(dict(d['l'][0], id=9))))
    def test_missing_ledger(self): self.assertTrue(self.run_with(lambda d: d['l'].pop(3)))
    def test_wrong_agency_net(self): self.assertTrue(self.run_with(lambda d: d['l'][3].update(amount_minor=9900)))
    def test_bank_payload_changed(self): self.assertTrue(self.run_with(lambda d: d['b'][0]['payload'].update(amount=1)))
    def test_missing_bank(self): self.assertTrue(self.run_with(lambda d: d['b'].clear()))
    def test_orphan_bank(self): self.assertTrue(self.run_with(lambda d: d['b'].append(dict(key='x', payload=dict(paymentId='zz'), result={}))))
    def test_wrong_fee(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(fee_minor=1, net_minor=10000)))
    def test_fee_bps_differs_from_agency(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(fee_bps=0, fee_minor=0, net_minor=10001)))
    def test_failed_with_bank_record(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(status='FAILED')))
    def test_failed_hold_not_released(self):
        def m(d): self.failed(d); del d['l'][2:]
        self.assertTrue(self.run_with(m))
    def test_pending_flagged(self):
        def m(d): d['p'][0].update(status='PENDING', bank_transfer_id=None); d['b'].clear(); del d['l'][2:]
        self.assertTrue(self.run_with(m))
    def test_settlement_rows_without_schema(self): self.assertTrue(self.run_with(lambda d: d['s'].append(dict(id='s1'))))


if __name__ == '__main__':
    unittest.main()
