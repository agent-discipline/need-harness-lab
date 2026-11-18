#!/usr/bin/env python3
"""Evaluator-owned adapter for the claude-pilot-03 implementation schema only (written by the conductor, not by the
implementer). Same meaning as evaluation/pilot_adapter.py (snapshot / verify_requests / audit), re-expressed for this
schema: payments, ledger_entries (3 rows per succeeded payment, 2 per settlement), settlements, settlement_items.
It reads every financial row and the real bank SQLite journal itself; it does not run the implementer's SQL."""
import argparse
import datetime
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

TABLES = ('payments', 'ledger_entries', 'settlements', 'settlement_items')
IMMUTABLE = ('agency_id', 'user_id', 'idempotency_key', 'bank_idempotency_key', 'amount_minor', 'currency', 'fee_bps',
             'fee_minor', 'status', 'bank_transfer_id', 'succeeded_at', 'business_date', 'failure_code')


class Adapter:
    def __init__(self, project):
        if not project.startswith('payment-claude-pilot-03-v'):
            raise ValueError('adapter is restricted to the claude-pilot-03 verify stacks')
        self.project = project

    def sql(self, query):
        return subprocess.check_output(['docker', 'exec', self.project + '-db-1', 'psql', '-U', 'lab', '-d', 'payment',
                                        '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', query], text=True).strip()

    def rows(self, table):
        return json.loads(self.sql(f"SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT * FROM {table}) t"))

    def bank(self):
        code = ("import sqlite3,json; c=sqlite3.connect('/data/bank.sqlite3'); "
                "print(json.dumps([{'key':k,'payload':json.loads(p),'result':json.loads(r)} "
                "for k,p,r in c.execute('select key,payload,result from transfers')]))")
        return json.loads(subprocess.check_output(['docker', 'exec', self.project + '-bank-1', 'python', '-c', code], text=True))

    def snapshot(self):
        return {table: self.rows(table) for table in TABLES}

    def verify_requests(self, before, requests, namespace):
        """Bind every new persisted effect to actual requests, not only totals."""
        after = self.snapshot()
        errors = []
        prior = {str(p['id']): p for p in before['payments']}
        current = {str(p['id']): p for p in after['payments']}
        accepted = {}
        for r in requests:
            if r['path'] != '/api/payments' or not isinstance(r['request'], dict):
                continue
            response = r['response']
            pid = response['body'].get('paymentId')
            if response['status'] in (200, 201, 202) and not pid:
                errors.append('accepted_without_payment_id')
            if pid:
                if pid not in current:
                    errors.append('accepted_not_persisted:' + str(pid)); continue
                p = current[pid]
                body = r['request']
                if any(p[column] != body.get(field) for column, field in
                       [('user_id', 'userId'), ('agency_id', 'agencyId'), ('amount_minor', 'amount'), ('currency', 'currency')]):
                    errors.append('persisted_payload:' + pid)
                if p['idempotency_key'] != namespace + '-' + str(r['key']):
                    errors.append('persisted_key:' + pid)
                accepted[pid] = p
        created = {pid: p for pid, p in current.items() if pid not in prior}
        ledger_by_payment = defaultdict(list)
        for row in after['ledger_entries']:
            if row.get('payment_id'):
                ledger_by_payment[str(row['payment_id'])].append(row)
        for pid, p in created.items():
            matching = [r for r in requests if r['path'] == '/api/payments' and r['key'] is not None
                        and namespace + '-' + str(r['key']) == p['idempotency_key'] and r['agency'] == p['agency_id']]
            if not matching:
                errors.append('unrequested_payment:' + pid)
            if pid not in accepted:
                # A request answered with a rejection may leave a FAILED record only when the answer was a business
                # rejection of an accepted-shape request (bank decline 422, insufficient balance 409) and the record
                # carries no financial effect. Any row after 400/403/404, or any non-FAILED row, is an error.
                rejected_ok = (p['status'] == 'FAILED' and not ledger_by_payment[pid] and p['bank_transfer_id'] is None
                               and any(r['response']['status'] in (409, 422) for r in matching)
                               and not any(r['response']['status'] in (400, 403, 404) for r in matching))
                if not rejected_ok:
                    errors.append('rejected_request_changed_state:' + pid)
        for pid, p in prior.items():
            q = current.get(pid)
            if q is None:
                errors.append('existing_payment_deleted:' + pid); continue
            # Settlement may claim an existing payment (settlement_id); its financial identity is immutable.
            if any(p[k] != q[k] for k in IMMUTABLE):
                errors.append('existing_payment_changed:' + pid)
        return errors

    def audit(self):
        payments, ledger, settlements, items = [self.rows(t) for t in TABLES]
        bank = self.bank()
        agencies = {int(k): int(v) for k, v in (line.split('|') for line in self.sql(
            'SELECT id, fee_bps FROM agencies').splitlines() if line)}
        errors = reconcile(payments, ledger, settlements, items, bank, agencies)
        queries = {
            'user_seed_count': 'SELECT abs(count(*)-1000000) FROM users',
            'agency_seed_count': 'SELECT abs(count(*)-1000) FROM agencies',
            'negative_balance': 'SELECT count(*) FROM users WHERE balance_minor<0',
            'balance_conservation': '''SELECT count(*) FROM (SELECT u.id FROM users u LEFT JOIN payments p ON p.user_id=u.id AND p.status='SUCCEEDED' GROUP BY u.id,u.balance_minor HAVING u.balance_minor+coalesce(sum(p.amount_minor),0)<>1000000) t''',
            'balance_vs_ledger_debit': '''SELECT count(*) FROM (SELECT u.id FROM users u LEFT JOIN ledger_entries l ON l.account='USER_BALANCE' AND l.direction='DEBIT' AND l.account_ref=u.id GROUP BY u.id,u.balance_minor HAVING u.balance_minor+coalesce(sum(l.amount_minor),0)<>1000000) t''',
            'pending_hold_exceeds_balance': '''SELECT count(*) FROM (SELECT u.id FROM users u JOIN payments p ON p.user_id=u.id AND p.status='PENDING' GROUP BY u.id,u.balance_minor HAVING sum(p.amount_minor)>u.balance_minor) t''',
            'agency_balance': '''SELECT count(*) FROM (SELECT a.id FROM agencies a LEFT JOIN settlements s ON s.agency_id=a.id GROUP BY a.id,a.settlement_balance_minor HAVING a.settlement_balance_minor<>coalesce(sum(s.net_minor),0)) t''',
            'ledger_debit_credit_total': '''SELECT abs(coalesce(sum(CASE WHEN direction='DEBIT' THEN amount_minor ELSE -amount_minor END),0)) FROM ledger_entries''',
            'orphan_user': 'SELECT count(*) FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE u.id IS NULL',
            'orphan_agency': 'SELECT count(*) FROM payments p LEFT JOIN agencies a ON a.id=p.agency_id WHERE a.id IS NULL',
            'orphan_settlement_agency': 'SELECT count(*) FROM settlements s LEFT JOIN agencies a ON a.id=s.agency_id WHERE a.id IS NULL',
        }
        aggregates = {name: int(self.sql(query)) for name, query in queries.items()}
        errors.extend(name for name, value in aggregates.items() if value != 0)
        return {'case': 'C19', 'status': 'PASS' if not errors else 'FAIL', 'passed': not errors,
                'errors': errors[:200], 'error_count': len(errors),
                'aggregates': aggregates, 'queries': queries,
                'counts': dict(payments=len(payments), ledger=len(ledger), settlements=len(settlements), items=len(items),
                               bank=len(bank), pending=sum(p['status'] == 'PENDING' for p in payments),
                               succeeded=sum(p['status'] == 'SUCCEEDED' for p in payments),
                               failed=sum(p['status'] == 'FAILED' for p in payments))}


def reconcile(payments, ledger, settlements, items, bank, agencies=None):
    errors = []

    def check(value, name):
        if not value:
            errors.append(name)
    ps = {str(p['id']): p for p in payments}
    ss = {str(s['id']): s for s in settlements}
    bs = {b['key']: b for b in bank}
    ledger_by_payment, ledger_by_settlement = defaultdict(list), defaultdict(list)
    items_by_payment, items_by_settlement = defaultdict(list), defaultdict(list)
    for row in ledger:
        if row.get('payment_id') is not None:
            ledger_by_payment[str(row['payment_id'])].append(row)
        if row.get('settlement_id') is not None:
            ledger_by_settlement[str(row['settlement_id'])].append(row)
        check((row.get('payment_id') is None) != (row.get('settlement_id') is None), f'ledger_source:{row["id"]}')
    for row in items:
        items_by_payment[str(row['payment_id'])].append(row)
        items_by_settlement[str(row['settlement_id'])].append(row)
    check(len(bs) == len(bank), 'bank_duplicate_key')
    for p in payments:
        pid = str(p['id'])
        rows = ledger_by_payment[pid]
        entries = items_by_payment[pid]
        success = p['status'] == 'SUCCEEDED'
        net = p['amount_minor'] - p['fee_minor']
        check(p['status'] != 'PENDING', f'pending:{pid}')
        check(len(rows) == (3 if success else 0), f'ledger_count:{pid}')
        if success:
            got = sorted((l['account'], l['direction'], l['account_ref'], l['amount_minor']) for l in rows)
            want = sorted([('USER_BALANCE', 'DEBIT', p['user_id'], p['amount_minor']),
                           ('AGENCY_PAYABLE', 'CREDIT', p['agency_id'], net),
                           ('FEE_REVENUE', 'CREDIT', p['agency_id'], p['fee_minor'])])
            check(got == want, f'ledger_amount:{pid}')
        check(p['fee_minor'] == p['amount_minor'] * p['fee_bps'] // 10000, f'fee:{pid}')
        if agencies is not None:
            check(agencies.get(p['agency_id']) == p['fee_bps'], f'fee_bps_vs_agency:{pid}')
        b = bs.get(p['bank_idempotency_key'])
        check(bool(b) == success, f'bank_presence:{pid}')
        if b:
            expected = dict(paymentId=pid, userId=p['user_id'], agencyId=p['agency_id'], amount=p['amount_minor'],
                            currency=p['currency'])
            check(b['payload'] == expected and b['result'].get('bankTransferId') == p['bank_transfer_id'], f'bank_payload:{pid}')
        check(len(entries) == int(p['settlement_id'] is not None), f'item_count:{pid}')
        for i in entries:
            check(success and str(i['settlement_id']) == str(p['settlement_id']) and i['amount_minor'] == p['amount_minor']
                  and i['fee_minor'] == p['fee_minor'] and i['net_minor'] == net, f'item_amount:{pid}')
    for l in ledger:
        if l.get('payment_id') is not None:
            check(str(l['payment_id']) in ps, 'orphan_ledger')
        if l.get('settlement_id') is not None:
            check(str(l['settlement_id']) in ss, 'orphan_ledger_settlement')
    for i in items:
        check(str(i['payment_id']) in ps and str(i['settlement_id']) in ss, 'orphan_item')
    for b in bank:
        check(str(b['payload'].get('paymentId')) in ps, 'orphan_bank')
    for s in settlements:
        sid = str(s['id'])
        rows = items_by_settlement[sid]
        gross, fee = sum(i['amount_minor'] for i in rows), sum(i['fee_minor'] for i in rows)
        check((s['gross_minor'], s['fee_minor'], s['net_minor'], s['payment_count']) == (gross, fee, gross - fee, len(rows)),
              f'settlement_totals:{sid}')
        got = sorted((l['account'], l['direction'], l['account_ref'], l['amount_minor']) for l in ledger_by_settlement[sid])
        want = sorted([('AGENCY_PAYABLE', 'DEBIT', s['agency_id'], s['net_minor']),
                       ('AGENCY_SETTLED', 'CREDIT', s['agency_id'], s['net_minor'])])
        check(got == want, f'settlement_ledger:{sid}')
        check(all(ps.get(str(i['payment_id']), {}).get('agency_id') == s['agency_id'] for i in rows), f'settlement_tenant:{sid}')
        for i in rows:
            p = ps.get(str(i['payment_id']), {})
            if p.get('succeeded_at') is not None and s.get('business_date') is not None:
                succeeded = datetime.datetime.fromisoformat(p['succeeded_at'])
                check(succeeded.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat() == s['business_date'],
                      f'settlement_date:{sid}')
                if s.get('created_at') is not None:      # this schema's snapshot cut time
                    check(succeeded <= datetime.datetime.fromisoformat(s['created_at']), f'settlement_cutoff:{sid}')
    return errors


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--project', required=True)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    result = Adapter(args.project).audit()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(result['status'] != 'PASS')
