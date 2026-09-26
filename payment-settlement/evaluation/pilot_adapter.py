#!/usr/bin/env python3
"""Evaluator-owned adapter for the excluded detailed-01 pilot schema only.

Never infer that another implementation has this schema. This adapter reads all
financial rows and compares them with the real bank SQLite journal independently.
"""
import argparse
import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import json
import subprocess
from pathlib import Path


class PilotAdapter:
    def __init__(self, project='payment-codex-pilot-verify'):
        self.project = project

    def sql(self, query):
        raw = subprocess.check_output(['docker','exec',self.project+'-db-1','psql',
            '-U','lab','-d','payment','-X','-A','-t','-v','ON_ERROR_STOP=1','-c',query], text=True)
        return raw.strip()

    def rows(self, table):
        return json.loads(self.sql(f"SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT * FROM {table}) t"))

    def bank(self):
        code = "import sqlite3,json; c=sqlite3.connect('/data/bank.sqlite3'); print(json.dumps([{'key':k,'payload':json.loads(p),'result':json.loads(r)} for k,p,r in c.execute('select key,payload,result from transfers')]))"
        return json.loads(subprocess.check_output(['docker','exec',self.project+'-bank-1',
                                                   'python','-c',code], text=True))

    def snapshot(self):
        return {table:self.rows(table) for table in ('payments','ledger','settlements','settlement_items')}

    def verify_requests(self, before, requests, namespace):
        """Bind every new persisted effect to actual requests, not only totals."""
        after=self.snapshot()
        errors=[]
        prior={str(p['id']):p for p in before['payments']}
        current={str(p['id']):p for p in after['payments']}
        accepted={}
        for r in requests:
            if r['path']!='/api/payments' or not isinstance(r['request'],dict):
                continue
            response=r['response']
            pid=response['body'].get('paymentId')
            if response['status'] in (200,201,202) and not pid:
                errors.append('accepted_without_payment_id')
            if pid:
                if pid not in current:
                    errors.append('accepted_not_persisted:'+str(pid)); continue
                p=current[pid]
                body=r['request']
                if any(p[column]!=body.get(field) for column,field in
                       [('user_id','userId'),('agency_id','agencyId'),('amount_minor','amount'),('currency','currency')]):
                    errors.append('persisted_payload:'+pid)
                if p['idempotency_key']!=namespace+'-'+str(r['key']):
                    errors.append('persisted_key:'+pid)
                accepted[pid]=p
        created={pid:p for pid,p in current.items() if pid not in prior}
        for pid,p in created.items():
            # Decline responses may deliberately omit the payment representation.
            matching=[r for r in requests if r['path']=='/api/payments' and r['key'] is not None
                      and namespace+'-'+str(r['key'])==p['idempotency_key']
                      and r['agency']==p['agency_id']]
            if not matching: errors.append('unrequested_payment:'+pid)
            if pid not in accepted and not (p['status']=='FAILED' and any(r['response']['status']==422 for r in matching)):
                errors.append('rejected_request_changed_state:'+pid)
        for pid,p in prior.items():
            q=current.get(pid)
            if q is None: errors.append('existing_payment_deleted:'+pid); continue
            # Settlement may claim an existing payment; its financial identity is immutable.
            if any(p[k]!=q[k] for k in ('agency_id','user_id','idempotency_key','bank_key','amount_minor','fee_minor','status','bank_transfer_id')):
                errors.append('existing_payment_changed:'+pid)
        return errors

    def audit(self):
        payments, ledger, settlements, items = [self.rows(t) for t in ('payments','ledger','settlements','settlement_items')]
        bank = self.bank()
        errors = reconcile(payments, ledger, settlements, items, bank)
        queries = {
            'user_seed_count': 'SELECT abs(count(*)-1000000) FROM users',
            'agency_seed_count': 'SELECT abs(count(*)-1000) FROM agencies',
            'negative_balance': 'SELECT count(*) FROM users WHERE balance_minor<0 OR reserved_minor<0 OR reserved_minor>balance_minor',
            'balance_conservation': '''SELECT count(*) FROM (SELECT u.id FROM users u LEFT JOIN ledger l ON l.user_id=u.id GROUP BY u.id,u.balance_minor HAVING u.balance_minor+coalesce(sum(l.amount_minor),0)<>1000000) t''',
            'reservation_conservation': '''SELECT count(*) FROM (SELECT u.id FROM users u LEFT JOIN payments p ON p.user_id=u.id AND p.status='PENDING' GROUP BY u.id,u.reserved_minor HAVING u.reserved_minor<>coalesce(sum(p.amount_minor),0)) t''',
            'agency_balance': '''SELECT count(*) FROM (SELECT a.id FROM agencies a LEFT JOIN settlements s ON s.agency_id=a.id GROUP BY a.id,a.settlement_balance_minor HAVING a.settlement_balance_minor<>coalesce(sum(s.net_amount),0)) t''',
            'orphan_user': 'SELECT count(*) FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE u.id IS NULL',
            'orphan_agency': 'SELECT count(*) FROM payments p LEFT JOIN agencies a ON a.id=p.agency_id WHERE a.id IS NULL',
        }
        aggregates = {name:int(self.sql(query)) for name,query in queries.items()}
        errors.extend(name for name,value in aggregates.items() if value != 0)
        return {'case':'C19','status':'PASS' if not errors else 'FAIL','errors':errors,
                'aggregates':aggregates,'queries':queries,
                'counts':dict(payments=len(payments),ledger=len(ledger),settlements=len(settlements),
                              items=len(items),bank=len(bank))}


def reconcile(payments, ledger, settlements, items, bank):
    errors = []
    def check(value, name):
        if not value: errors.append(name)
    ps = {str(p['id']):p for p in payments}
    ss = {str(s['id']):s for s in settlements}
    bs = {b['key']:b for b in bank}
    ledger_by_payment, items_by_payment, items_by_settlement = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in ledger: ledger_by_payment[str(row['payment_id'])].append(row)
    for row in items:
        items_by_payment[str(row['payment_id'])].append(row)
        items_by_settlement[str(row['settlement_id'])].append(row)
    check(len(bs)==len(bank),'bank_duplicate_key')
    for p in payments:
        pid = str(p['id'])
        rows = ledger_by_payment[pid]
        entries = items_by_payment[pid]
        success = p['status']=='SUCCEEDED'
        check(p['status']!='PENDING',f'pending:{pid}')
        check(len(rows)==int(success),f'ledger_count:{pid}')
        check(all(l['user_id']==p['user_id'] and l['amount_minor']==p['amount_minor'] for l in rows),f'ledger_amount:{pid}')
        check(p['fee_minor']==p['amount_minor']*p['fee_bps']//10000,f'fee:{pid}')
        b = bs.get(p['bank_key'])
        check(bool(b)==success,f'bank_presence:{pid}')
        if b:
            expected = dict(paymentId=pid,userId=p['user_id'],agencyId=p['agency_id'],amount=p['amount_minor'],currency=p['currency'])
            check(b['payload']==expected and b['result'].get('bankTransferId')==p['bank_transfer_id'],f'bank_payload:{pid}')
        check(len(entries)==int(p['settlement_id'] is not None),f'item_count:{pid}')
        for i in entries:
            check(success and str(i['settlement_id'])==str(p['settlement_id']) and
                  i['amount_minor']==p['amount_minor'] and i['fee_minor']==p['fee_minor'],f'item_amount:{pid}')
    for l in ledger: check(str(l['payment_id']) in ps,'orphan_ledger')
    for i in items: check(str(i['payment_id']) in ps and str(i['settlement_id']) in ss,'orphan_item')
    for b in bank: check(str(b['payload'].get('paymentId')) in ps,'orphan_bank')
    for s in settlements:
        rows = items_by_settlement[str(s['id'])]
        gross,fee = sum(i['amount_minor'] for i in rows),sum(i['fee_minor'] for i in rows)
        check((s['gross_amount'],s['fee_amount'],s['net_amount'],s['payment_count'])==
              (gross,fee,gross-fee,len(rows)),f'settlement_totals:{s["id"]}')
        check(all(ps.get(str(i['payment_id']),{}).get('agency_id')==s['agency_id'] for i in rows),f'settlement_tenant:{s["id"]}')
        for i in rows:
            p=ps.get(str(i['payment_id']),{})
            if p.get('succeeded_at') is not None and s.get('business_date') is not None:
                succeeded=datetime.datetime.fromisoformat(p['succeeded_at'])
                check(succeeded.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()==s['business_date'],f'settlement_date:{s["id"]}')
                if s.get('cutoff_at') is not None:
                    check(succeeded<=datetime.datetime.fromisoformat(s['cutoff_at']),f'settlement_cutoff:{s["id"]}')
    return errors


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--project',default='payment-codex-pilot-verify')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    result=PilotAdapter(args.project).audit()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    raise SystemExit(result['status']!='PASS')
