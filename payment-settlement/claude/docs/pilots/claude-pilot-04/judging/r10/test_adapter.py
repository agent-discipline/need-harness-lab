"""Negative controls for the pilot-04 round-08 adapter's reconcile (same idea as evaluation/test_reconcile.py)."""
import copy, unittest
from adapter import reconcile

P = dict(id='p1', agency_id=1, user_id=7, idempotency_key='k', amount_minor=10001, currency='KRW', fee_bps=100, fee_minor=100,
         net_minor=9901, status='SUCCEEDED', bank_transfer_id='b1', completed_at='2026-11-18T01:00:00+00:00', failure_code=None,
         settlement_id='s1', settled_at='2026-11-18T02:00:00+00:00', business_date='2026-11-18')
L = [dict(id=1, payment_id='p1', entry_type='AUTHORIZE', account_type='USER', account_id=7, amount_minor=-10001),
     dict(id=2, payment_id='p1', entry_type='AUTHORIZE', account_type='PAYMENT_CLEARING', account_id=0, amount_minor=10001),
     dict(id=3, payment_id='p1', entry_type='CAPTURE', account_type='PAYMENT_CLEARING', account_id=0, amount_minor=-10001),
     dict(id=4, payment_id='p1', entry_type='CAPTURE', account_type='AGENCY_PAYABLE', account_id=1, amount_minor=9901),
     dict(id=5, payment_id='p1', entry_type='CAPTURE', account_type='PLATFORM_FEE', account_id=0, amount_minor=100),
     dict(id=6, payment_id='p1', entry_type='SETTLE', account_type='AGENCY_PAYABLE', account_id=1, amount_minor=-9901),
     dict(id=7, payment_id='p1', entry_type='SETTLE', account_type='AGENCY_SETTLEMENT', account_id=1, amount_minor=9901)]
S = dict(id='s1', agency_id=1, business_date='2026-11-18', gross_minor=10001, fee_minor=100, net_minor=9901, payment_count=1,
         created_at='2026-11-18T02:00:00+00:00')
B = dict(key='p1', payload=dict(paymentId='p1', userId=7, agencyId=1, amount=10001, currency='KRW'), result=dict(bankTransferId='b1'))
RELEASE = [dict(id=8, payment_id='p1', entry_type='RELEASE', account_type='PAYMENT_CLEARING', account_id=0, amount_minor=-10001),
           dict(id=9, payment_id='p1', entry_type='RELEASE', account_type='USER', account_id=7, amount_minor=10001)]


class T(unittest.TestCase):
    def run_with(self, mutate=None):
        d = copy.deepcopy(dict(p=[P], l=L, s=[S], b=[B]))
        if mutate: mutate(d)
        return reconcile(d['p'], d['l'], d['s'], [p for p in d['p'] if p['settlement_id'] is not None], d['b'], {1: 100})

    def unsettle(self, d):
        d['p'][0].update(settlement_id=None, settled_at=None); del d['l'][5:]; d['s'].clear()

    def failed(self, d):
        self.unsettle(d); d['p'][0].update(status='FAILED', bank_transfer_id=None, failure_code='BANK_DECLINED', business_date=None); d['b'].clear()

    def test_clean_passes(self): self.assertEqual(self.run_with(), [])
    def test_clean_unsettled_passes(self): self.assertEqual(self.run_with(self.unsettle), [])
    def test_clean_failed_released_passes(self):
        def m(d): self.failed(d); d['l'][2:] = copy.deepcopy(RELEASE)
        self.assertEqual(self.run_with(m), [])
    def test_duplicate_debit(self): self.assertTrue(self.run_with(lambda d: d['l'].append(dict(d['l'][0], id=19))))
    def test_missing_ledger(self): self.assertTrue(self.run_with(lambda d: d['l'].pop(3)))
    def test_bank_payload_changed(self): self.assertTrue(self.run_with(lambda d: d['b'][0]['payload'].update(amount=1)))
    def test_missing_bank(self): self.assertTrue(self.run_with(lambda d: d['b'].clear()))
    def test_orphan_bank(self): self.assertTrue(self.run_with(lambda d: d['b'].append(dict(key='x', payload=dict(paymentId='zz'), result={}))))
    def test_wrong_fee(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(fee_minor=1, net_minor=10000)))
    def test_failed_with_bank_record(self): self.assertTrue(self.run_with(lambda d: d['p'][0].update(status='FAILED')))
    def test_failed_hold_not_released(self):
        def m(d): self.failed(d); del d['l'][2:]
        self.assertTrue(self.run_with(m))
    def test_pending_flagged(self):
        def m(d): self.unsettle(d); d['p'][0].update(status='PENDING', bank_transfer_id=None, business_date=None); d['b'].clear(); del d['l'][2:]
        self.assertTrue(self.run_with(m))
    def test_wrong_settlement_total(self): self.assertTrue(self.run_with(lambda d: d['s'][0].update(net_minor=1)))
    def test_double_settle_effect(self): self.assertTrue(self.run_with(lambda d: d['l'].extend([dict(d['l'][5], id=21), dict(d['l'][6], id=22)])))
    def test_settled_without_settle_effect(self): self.assertTrue(self.run_with(lambda d: d['l'].__delitem__(slice(5, 7))))
    def test_orphan_item(self): self.assertTrue(self.run_with(lambda d: d['s'].clear()))
    def test_wrong_date(self): self.assertTrue(self.run_with(lambda d: d['s'][0].update(business_date='2026-11-17')))
    def test_missing_business_date(self): self.assertTrue(self.run_with(lambda d: d['s'][0].pop('business_date')))
    def test_settled_before_success(self): self.assertTrue(self.run_with(lambda d: d['s'][0].update(created_at='2026-11-18T00:00:00+00:00')))
    def test_payment_business_date_not_kst_success_date(self): self.assertTrue(self.run_with(lambda d: (d['p'][0].update(business_date='2026-11-19'), d['s'][0].update(business_date='2026-11-19'))))
    def test_wrong_tenant(self): self.assertTrue(self.run_with(lambda d: d['s'][0].update(agency_id=2)))


if __name__ == '__main__':
    unittest.main()
