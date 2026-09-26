#!/usr/bin/env python3
"""External HTTP checks. Missing independent evidence is explicitly NOT_RUN.

Run against a fresh disposable stack. This program creates financial fixture data.
It never treats an implementation's self-reported tests as external evidence.
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


class Evaluator:
    def __init__(self, base, namespace, adapter=None):
        self.base, self.ns = base.rstrip('/'), namespace
        self.adapter = adapter
        self.results = {}
        self.requests = []

    def request(self, path, body=None, agency=901, key=None, fault=None):
        headers = {'Content-Type': 'application/json', 'X-Agency-Id': str(agency)}
        if key is not None:
            headers['Idempotency-Key'] = self.ns + '-' + key
        if fault:
            headers['X-Bank-Fault'] = fault
        request = urllib.request.Request(self.base + path, headers=headers,
                                        data=None if body is None else json.dumps(body).encode())
        start = time.monotonic()
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.read().decode()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {'raw': raw[:2000]}
        result = {'status': response.status, 'body': parsed}
        self.requests.append({'path': path, 'request': body, 'key': key, 'agency': agency,
                              'fault': fault, 'elapsed_seconds': time.monotonic()-start,
                              'response': result})
        return result

    def pay(self, key, user=900001, agency=901, amount=100, fault=None, **overrides):
        body = dict(userId=user, agencyId=agency, amount=amount, currency='KRW')
        body.update(overrides)
        return self.request('/api/payments', body, agency, key, fault)

    def final(self, response, agency=901):
        payment_id = response['body'].get('paymentId')
        assert payment_id, response
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = self.request('/api/payments/' + payment_id, agency=agency)
            if response['body'].get('status') != 'PENDING':
                assert response['status'] == 200, response
                return response['body']
            time.sleep(.25)
        raise AssertionError('PENDING did not converge within 30 seconds')

    def check(self, case, callback, scope='HTTP_ONLY'):
        begin = len(self.requests)
        try:
            before = self.adapter.snapshot() if self.adapter else None
            callback()
            if self.adapter:
                audit = self.adapter.audit()
                self.require(audit['status'] == 'PASS', audit)
                changes = self.adapter.verify_requests(before, self.requests[begin:], self.ns)
                self.require(not changes, changes)
                after = self.adapter.snapshot()
                snapshots = {name: {'sha256':hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest(), 'row_counts':{k:len(v) for k,v in value.items()}} for name,value in [('before',before),('after',after)]}
                self.results[case] = {'status':'PASS', 'scope':'HTTP+DB+BANK', 'audit':audit, 'snapshots':snapshots, 'request_indices':[begin,len(self.requests)]}
                return
            self.results[case] = {'status': 'PARTIAL', 'scope': scope,
                                  'observed_checks_passed': True,
                                  'remaining': 'independent database/bank invariants'}
        except Exception as error:
            self.results[case] = {'status': 'FAIL', 'error': str(error), 'scope': scope}
        self.results[case]['request_indices'] = [begin, len(self.requests)]

    @staticmethod
    def require(condition, detail):
        if not condition:
            raise AssertionError(detail)

    def run(self):
        def normal():
            r = self.pay('normal')
            self.require(r['status'] == 201 and r['body'].get('status') == 'SUCCEEDED', r)
        self.check('C01', normal)

        def invalid():
            bodies = [dict(amount=x) for x in (-1, 0, 1000001, 1.5)] + [dict(currency='USD')]
            for i, override in enumerate(bodies):
                r = self.pay('invalid-' + str(i), **override)
                self.require(r['status'] == 400, r)
            for field in ('userId', 'agencyId', 'amount', 'currency'):
                body = dict(userId=900001, agencyId=901, amount=100, currency='KRW')
                del body[field]
                r = self.request('/api/payments', body, key='missing-' + field)
                self.require(r['status'] == 400, r)
        self.check('C02', invalid)

        def missing():
            for kwargs in (dict(user=1000001), dict(agency=1001)):
                r = self.pay('missing-' + str(kwargs), **kwargs)
                self.require(r['status'] == 404, r)
        self.check('C03', missing)

        def replay():
            first = self.pay('replay')
            identifier = first['body'].get('paymentId')
            self.require(first['status'] == 201 and identifier, first)
            for _ in range(20):
                r = self.pay('replay')
                self.require(r['status'] == 200 and r['body'].get('paymentId') == identifier, r)
        self.check('C04', replay)

        def conflict():
            self.pay('conflict')
            for kwargs in (dict(amount=101), dict(user=900002)):
                r = self.pay('conflict', **kwargs)
                self.require(r['status'] == 409, r)
        self.check('C05', conflict)

        def concurrent_payments(same_key):
            with concurrent.futures.ThreadPoolExecutor(max_workers=100) as pool:
                responses = list(pool.map(lambda n: self.pay('race' if same_key else 'hot-' + str(n),
                    user=900002 if same_key else 900003, amount=100 if same_key else 20000), range(100)))
            allowed = (200, 201, 202) if same_key else (201, 202, 409)
            self.require(all(r['status'] in allowed for r in responses), responses)
            accepted = [r for r in responses if r['status'] != 409]
            ids = {r['body'].get('paymentId') for r in accepted}
            self.require(None not in ids and (len(ids) == 1 if same_key else True), responses)
            finals = [self.final(r) for r in accepted]
            if not same_key:
                self.require(sum(r['amount'] for r in finals if r['status'] == 'SUCCEEDED') <= 1000000,
                             'accepted total exceeded initial balance')
        self.check('C06', lambda: concurrent_payments(True))
        self.check('C07', lambda: concurrent_payments(False))

        def tenants():
            a, b = self.pay('tenant', agency=902), self.pay('tenant', agency=903)
            self.require(a['status'] == b['status'] == 201 and
                         a['body'].get('paymentId') != b['body'].get('paymentId'), [a,b])
        self.check('C08', tenants)

        def isolation():
            r = self.request('/api/payments', dict(userId=900001, agencyId=902, amount=100,
                                                  currency='KRW'), key='mismatch')
            self.require(r['status'] == 403, r)
            own = self.pay('private')
            other = self.request('/api/payments/' + own['body']['paymentId'], agency=902)
            self.require(other['status'] == 404, other)
        self.check('C09', isolation)
        self.check('C10', lambda: self.require(self.pay('decline', fault='decline')['status'] == 422,
                                             'decline must return 422'))

        def recover(fault):
            r = self.pay('fault-' + fault, fault=fault)
            self.require(r['status'] in (201,202), r)
            self.require(self.final(r)['status'] == 'SUCCEEDED', r)
        self.check('C11', lambda: [recover(f) for f in ('timeout-before', '5xx')])
        self.check('C12', lambda: recover('timeout-after'))

        date = datetime.datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
        def settle(key, agency=904, business_date=date):
            return self.request('/api/settlements', dict(agencyId=agency, businessDate=business_date),
                                agency, key)
        def settlement():
            for i, amount in enumerate((101,199,10001)):
                self.require(self.pay('settle-pay-' + str(i), agency=904, amount=amount)['status'] == 201,
                             'settlement fixture payment failed')
            a = settle('settle')
            b = settle('settle')
            self.require(a['status'] in (200,201) and b['status'] == 200, [a,b])
            self.require(a['body'].get('settlementId') == b['body'].get('settlementId') and
                all(a['body'].get(k) == v for k,v in dict(grossAmount=10301,feeAmount=102,
                    netAmount=10199,paymentCount=3).items()), [a,b])
        self.check('C15', settlement)

        def settlement_race():
            self.require(self.pay('settle-race-pay', agency=905, amount=101)['status'] == 201, 'fixture failed')
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
                responses = list(pool.map(lambda n: settle('settle-race-'+str(n), agency=905), range(50)))
            self.require(all(r['status'] in (200,201) for r in responses), responses)
            self.require(sum(r['body'].get('paymentCount', -1000) for r in responses) == 1, responses)
        self.check('C16', settlement_race)

        def empty():
            a = settle('empty', agency=906, business_date='2000-01-01')
            b = settle('empty', agency=906, business_date='2000-01-01')
            self.require(a['status'] in (200,201) and b['status'] == 200 and
                         a['body'].get('settlementId') == b['body'].get('settlementId') and
                         all(a['body'].get(k) == 0 for k in ('paymentCount','grossAmount','feeAmount','netAmount')),
                         [a,b])
        self.check('C18', empty)
        for number in range(1,21):
            self.results.setdefault(f'C{number:02}', {'status':'NOT_RUN',
                'remaining':'external fault/clock/SQL/performance evidence required'})
        return {'all_acceptance_passed': False, 'namespace':self.ns,
                'cases':dict(sorted(self.results.items())), 'requests':self.requests}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pilot-project', default='payment-codex-pilot-verify')
    parser.add_argument('--pilot-audit', action='store_true', help='Use evaluator-owned pilot schema adapter')
    args = parser.parse_args()
    from pilot_adapter import PilotAdapter
    report = Evaluator(args.base_url, args.namespace, PilotAdapter(args.pilot_project) if args.pilot_audit else None).run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v['status'] for k,v in report['cases'].items()}))
    raise SystemExit(1 if any(v['status']=='FAIL' for v in report['cases'].values()) else 2)
