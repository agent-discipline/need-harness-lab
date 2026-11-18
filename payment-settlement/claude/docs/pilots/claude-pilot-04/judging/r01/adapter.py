#!/usr/bin/env python3
"""Evaluator-owned adapter for the claude-pilot-04 implementation as of judging round 01 (written by the conductor, not
by the implementer). Same meaning as evaluation/pilot_adapter.py (snapshot / verify_requests / audit), re-expressed for
this schema: payments + ledger_entries (signed double entry: AUTHORIZE / CAPTURE / RELEASE postings). This build has
NO settlements / settlement_items tables; they are read as empty and the contract checks that involve them stay on.
It reads every financial row and the real bank SQLite journal itself; it does not run the implementer's SQL."""
import argparse
import json
import subprocess
from collections import defaultdict
from pathlib import Path

TABLES = ('payments', 'ledger_entries', 'settlements', 'settlement_items')
IMMUTABLE = ('agency_id', 'user_id', 'idempotency_key', 'amount_minor', 'currency', 'fee_bps', 'fee_minor', 'net_minor',
             'status', 'bank_transfer_id', 'completed_at', 'failure_code')


class Adapter:
    def __init__(self, project):
        if not project.startswith('payment-claude-pilot-04-v'):
            raise ValueError('adapter is restricted to the claude-pilot-04 verify stacks')
        self.project = project

    def sql(self, query):
        return subprocess.check_output(['docker', 'exec', self.project + '-db-1', 'psql', '-U', 'lab', '-d', 'payment',
                                        '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', query], text=True).strip()

    def has_table(self, table):
        return self.sql("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='%s'" % table) == '1'

    def rows(self, table):
        if table in ('settlements', 'settlement_items') and not self.has_table(table):
            return []          # absent in this build; recorded in audit()['absent_tables']
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
            ledger_by_payment[str(row['payment_id'])].append(row)
        bank_keys = {b['key'] for b in self.bank()}
        for pid, p in created.items():
            matching = [r for r in requests if r['path'] == '/api/payments' and r['key'] is not None
                        and namespace + '-' + str(r['key']) == p['idempotency_key'] and r['agency'] == p['agency_id']]
            if not matching:
                errors.append('unrequested_payment:' + pid)
            if pid not in accepted:
                # A request answered with a rejection may leave a FAILED record only when the answer was a business
                # rejection of an accepted-shape request (bank decline 422, insufficient balance 409) and the record
                # carries no net financial effect (no ledger rows, or a hold that was fully released) and no bank
                # record. Any row after 400/403/404, or any non-FAILED row, is an error.
                rows = ledger_by_payment[pid]
                no_effect = (not rows) or failed_ledger_ok(p, rows)
                rejected_ok = (p['status'] == 'FAILED' and no_effect and pid not in bank_keys and p['bank_transfer_id'] is None
                               and any(r['response']['status'] in (409, 422) for r in matching)
                               and not any(r['response']['status'] in (400, 403, 404) for r in matching))
                if not rejected_ok:
                    errors.append('rejected_request_changed_state:' + pid)
        for pid, p in prior.items():
            q = current.get(pid)
            if q is None:
                errors.append('existing_payment_deleted:' + pid); continue
            if any(p[k] != q[k] for k in IMMUTABLE):
                errors.append('existing_payment_changed:' + pid)
        return errors

    def audit(self):
        absent = [t for t in ('settlements', 'settlement_items') if not self.has_table(t)]
        payments, ledger, settlements, items = [self.rows(t) for t in TABLES]
        bank = self.bank()
        agencies = {int(k): int(v) for k, v in (line.split('|') for line in self.sql(
            'SELECT id, fee_bps FROM agencies').splitlines() if line)}
        errors = reconcile(payments, ledger, settlements, items, bank, agencies)
        settled_net = ('(SELECT agency_id, sum(net_minor) AS net FROM settlements GROUP BY agency_id)'
                       if 'settlements' not in absent else '(SELECT NULL::bigint AS agency_id, 0::bigint AS net WHERE false)')
        queries = {
            'user_seed_count': 'SELECT abs(count(*)-1000000) FROM users',
            'agency_seed_count': 'SELECT abs(count(*)-1000) FROM agencies',
            'negative_balance': 'SELECT count(*) FROM users WHERE balance_minor<0',
            # this build debits the user when the payment is accepted (hold), so PENDING amounts are out of the balance too
            'balance_conservation': '''SELECT count(*) FROM (SELECT u.id FROM users u LEFT JOIN payments p ON p.user_id=u.id AND p.status IN ('SUCCEEDED','PENDING') GROUP BY u.id,u.balance_minor HAVING u.balance_minor+coalesce(sum(p.amount_minor),0)<>1000000) t''',
            'balance_vs_ledger_user': '''SELECT count(*) FROM (SELECT u.id FROM users u LEFT JOIN ledger_entries l ON l.account_type='USER' AND l.account_id=u.id GROUP BY u.id,u.balance_minor HAVING u.balance_minor-coalesce(sum(l.amount_minor),0)<>1000000) t''',
            # contract: the agency settlement balance grows by the net amount of each settlement, exactly once
            'agency_balance_vs_settlements': '''SELECT count(*) FROM (SELECT a.id FROM agencies a LEFT JOIN ''' + settled_net + ''' s ON s.agency_id=a.id WHERE a.settlement_balance_minor<>coalesce(s.net,0)) t''',
            'ledger_signed_total': 'SELECT abs(coalesce(sum(amount_minor),0)) FROM ledger_entries',
            'orphan_user': 'SELECT count(*) FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE u.id IS NULL',
            'orphan_agency': 'SELECT count(*) FROM payments p LEFT JOIN agencies a ON a.id=p.agency_id WHERE a.id IS NULL',
        }
        aggregates = {name: int(self.sql(query)) for name, query in queries.items()}
        errors.extend(name for name, value in aggregates.items() if value != 0)
        # not a check: how this build's own model (credit at payment success) relates to the agency balance
        info = {'agency_balance_vs_succeeded_net_mismatches': int(self.sql(
            '''SELECT count(*) FROM (SELECT a.id FROM agencies a LEFT JOIN payments p ON p.agency_id=a.id AND p.status='SUCCEEDED' GROUP BY a.id,a.settlement_balance_minor HAVING a.settlement_balance_minor<>coalesce(sum(p.net_minor),0)) t'''))}
        return {'case': 'C19', 'status': 'PASS' if not errors else 'FAIL', 'passed': not errors,
                'errors': errors[:200], 'error_count': len(errors),
                'aggregates': aggregates, 'queries': queries, 'absent_tables': absent, 'info': info,
                'counts': dict(payments=len(payments), ledger=len(ledger), settlements=len(settlements), items=len(items),
                               bank=len(bank), pending=sum(p['status'] == 'PENDING' for p in payments),
                               succeeded=sum(p['status'] == 'SUCCEEDED' for p in payments),
                               failed=sum(p['status'] == 'FAILED' for p in payments))}


def legs(rows):
    return sorted((l['entry_type'], l['account_type'], l['account_id'], l['amount_minor']) for l in rows)


def authorize(p):
    return [('AUTHORIZE', 'USER', p['user_id'], -p['amount_minor']), ('AUTHORIZE', 'PAYMENT_CLEARING', 0, p['amount_minor'])]


def failed_ledger_ok(p, rows):
    """FAILED with a hold that was fully released (net user effect 0)."""
    want = authorize(p) + [('RELEASE', 'PAYMENT_CLEARING', 0, -p['amount_minor']), ('RELEASE', 'USER', p['user_id'], p['amount_minor'])]
    return legs(rows) == sorted(want)


def reconcile(payments, ledger, settlements, items, bank, agencies=None):
    errors = []

    def check(value, name):
        if not value:
            errors.append(name)
    ps = {str(p['id']): p for p in payments}
    bs = {b['key']: b for b in bank}
    ledger_by_payment = defaultdict(list)
    for row in ledger:
        ledger_by_payment[str(row['payment_id'])].append(row)
    check(len(bs) == len(bank), 'bank_duplicate_key')
    check(not settlements and not items, 'settlement_rows_present_but_adapter_has_no_settlement_schema')
    for p in payments:
        pid = str(p['id'])
        rows = ledger_by_payment[pid]
        success = p['status'] == 'SUCCEEDED'
        fee = p['amount_minor'] * p['fee_bps'] // 10000
        check(p['status'] != 'PENDING', f'pending:{pid}')
        if success:
            want = authorize(p) + [('CAPTURE', 'PAYMENT_CLEARING', 0, -p['amount_minor']),
                                   ('CAPTURE', 'AGENCY_SETTLEMENT', p['agency_id'], p['net_minor'])]
            if p['fee_minor']:
                want.append(('CAPTURE', 'PLATFORM_FEE', 0, p['fee_minor']))
            check(legs(rows) == sorted(want), f'ledger_amount:{pid}')
        elif p['status'] == 'FAILED':
            check(not rows or failed_ledger_ok(p, rows), f'ledger_failed_effect:{pid}')
        else:
            check(legs(rows) == sorted(authorize(p)), f'ledger_pending_hold:{pid}')
        check(p['fee_minor'] == fee and p['net_minor'] == p['amount_minor'] - fee, f'fee:{pid}')
        if agencies is not None:
            check(agencies.get(p['agency_id']) == p['fee_bps'], f'fee_bps_vs_agency:{pid}')
        b = bs.get(pid)                                   # this build's bank key is the payment id
        check(bool(b) == success, f'bank_presence:{pid}')
        if b:
            expected = dict(paymentId=pid, userId=p['user_id'], agencyId=p['agency_id'], amount=p['amount_minor'],
                            currency=p['currency'].strip())
            check(b['payload'] == expected and b['result'].get('bankTransferId') == p['bank_transfer_id'], f'bank_payload:{pid}')
    for l in ledger:
        check(str(l['payment_id']) in ps, 'orphan_ledger')
    for b in bank:
        check(str(b['payload'].get('paymentId')) in ps and b['key'] in ps, 'orphan_bank')
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
