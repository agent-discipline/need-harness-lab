import copy
import unittest
from pilot_adapter import reconcile


class ReconciliationControls(unittest.TestCase):
    def setUp(self):
        self.p=[dict(id='p',user_id=1,agency_id=1,amount_minor=101,fee_bps=100,fee_minor=1,
                     status='SUCCEEDED',bank_key='k',bank_transfer_id='b',settlement_id='s',currency='KRW')]
        self.l=[dict(payment_id='p',user_id=1,amount_minor=101)]
        self.s=[dict(id='s',agency_id=1,gross_amount=101,fee_amount=1,net_amount=100,payment_count=1)]
        self.i=[dict(payment_id='p',settlement_id='s',amount_minor=101,fee_minor=1)]
        self.b=[dict(key='k',payload=dict(paymentId='p',userId=1,agencyId=1,amount=101,currency='KRW'),
                     result=dict(bankTransferId='b'))]

    def audit(self): return reconcile(self.p,self.l,self.s,self.i,self.b)
    def test_valid(self): self.assertEqual([],self.audit())
    def test_duplicate_debit(self):
        self.l.append(copy.deepcopy(self.l[0])); self.assertTrue(self.audit())
    def test_missing_ledger(self):
        self.l=[]; self.assertTrue(self.audit())
    def test_bank_payload_reuse(self):
        self.b[0]['payload']['amount']=102; self.assertTrue(self.audit())
    def test_wrong_settlement(self):
        self.s[0]['net_amount']=101; self.assertTrue(self.audit())
    def test_orphan_bank(self):
        self.b.append(dict(key='orphan',payload={'paymentId':'none'},result={})); self.assertTrue(self.audit())
    def test_wrong_kst_settlement_date(self):
        self.p[0]['succeeded_at']='2020-01-01T15:00:00+00:00'
        self.s[0]['business_date']='2020-01-01'
        self.assertTrue(self.audit())
    def test_double_settlement_item(self):
        self.i.append(copy.deepcopy(self.i[0])); self.assertTrue(self.audit())


if __name__=='__main__': unittest.main()
