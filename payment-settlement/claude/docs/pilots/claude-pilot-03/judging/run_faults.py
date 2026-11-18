#!/usr/bin/env python3
"""Destructive fault/clock checks for claude-pilot-03 verify stacks only. Scenario logic is evaluation/pilot_faults.py
(C13 app SIGKILL, C14 db SIGKILL, C17 KST boundary fixture + late success) with only the schema-specific SQL changed:
- status lookup: same payments(id uuid, status)
- C17 timestamp fixture: this schema has CHECK business_date = (succeeded_at AT TIME ZONE 'Asia/Seoul')::date, so the
  evaluator sets succeeded_at and business_date together (the implementer's guide names this as the fixture path)
- late success: created_at fixture and succeeded_at readback are the same columns
pilot_faults.py itself refuses any project other than the Codex pilot, so it cannot be run against this stack."""
import argparse, concurrent.futures, datetime, json, subprocess, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[4] / 'evaluation'))
sys.path.insert(0, str(HERE))
from acceptance import Evaluator
from adapter import Adapter


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def stamp_sql(identifier, stamp):
    return ("UPDATE payments SET succeeded_at='" + stamp + "'::timestamptz, business_date=('" + stamp +
            "'::timestamptz AT TIME ZONE 'Asia/Seoul')::date WHERE id='" + identifier + "'::uuid")


def run(base, namespace, project):
    if not project.startswith('payment-claude-pilot-03-v'):
        raise ValueError('Fault injector is restricted to claude-pilot-03 verify projects')
    adapter = Adapter(project)
    api = Evaluator(base, namespace)
    evidence = {}
    for case, service in [('C13', 'app'), ('C14', 'db')]:
        container = project + '-' + service + '-1'
        before = {b['key'] for b in adapter.bank()}
        record = None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(api.pay, case, user=900010, agency=910, amount=100, fault='timeout-after')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    new = [b for b in adapter.bank() if b['key'] not in before]
                    if new:
                        record = new[0]
                        break
                    time.sleep(.02)
                if record is None: raise AssertionError('bank commit boundary not observed')
                identifier = record['payload']['paymentId']
                status = adapter.sql("SELECT status FROM payments WHERE id='" + identifier + "'::uuid")
                if status != 'PENDING': raise AssertionError('missed pre-finalization boundary: ' + status)
                docker('kill', '--signal=KILL', container)
                killed = time.time()
                try:
                    initial = future.result(timeout=20)
                except Exception as error:
                    initial = {'transport_error': str(error)}
                docker('start', container)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    state = docker('inspect', '--format', '{{.State.Health.Status}}', container)
                    if state == 'healthy': break
                    time.sleep(.5)
                else: raise AssertionError('container did not become healthy')
                recovered = time.time()
                final = api.final({'body': {'paymentId': identifier}}, agency=910)
                if final['status'] != 'SUCCEEDED': raise AssertionError(final)
                replay = api.pay(case, user=900010, agency=910, amount=100)
                if replay['status'] != 200 or replay['body'].get('paymentId') != identifier:
                    raise AssertionError(replay)
                audit = adapter.audit()
                if audit['status'] != 'PASS': raise AssertionError(audit)
                evidence[case] = {'status': 'PASS', 'observed_before_kill': status, 'bank_record': record,
                                  'killed_at': killed, 'healthy_at': recovered, 'final_at': time.time(),
                                  'initial': initial, 'replay': replay, 'audit': audit}
        except Exception as error:
            evidence[case] = {'status': 'FAIL', 'error': str(error), 'bank_record': record}
        finally:
            docker('start', container)

    try:
        ids = []
        for n in range(3):
            r = api.pay('clock-' + str(n), user=900020, agency=920, amount=101 + n)
            if r['status'] != 201: raise AssertionError(r)
            ids.append(r['body']['paymentId'])
        stamps = ['2020-01-01T14:59:59Z', '2020-01-01T15:00:00Z', '2020-01-01T15:00:01Z']
        for identifier, stamp in zip(ids, stamps):
            adapter.sql(stamp_sql(identifier, stamp))
        def settle(key, date):
            r = api.request('/api/settlements', {'agencyId': 920, 'businessDate': date}, 920, key)
            if r['status'] not in (200, 201): raise AssertionError(r)
            return r['body']
        first = settle('clock-before', '2020-01-01')
        second = settle('clock-after', '2020-01-02')
        if first['paymentCount'] != 1 or first['grossAmount'] != 101 or second['paymentCount'] != 2 or second['grossAmount'] != 205:
            raise AssertionError([first, second])
        late = api.pay('clock-late', user=900020, agency=920, amount=104)
        if late['status'] != 201: raise AssertionError(late)
        identifier = late['body']['paymentId']
        adapter.sql(stamp_sql(identifier, '2020-01-01T15:00:02Z'))
        replay = settle('clock-after', '2020-01-02')
        additional = settle('clock-additional', '2020-01-02')
        if replay != second or additional['paymentCount'] != 1 or additional['grossAmount'] != 104:
            raise AssertionError([replay, additional])
        audit = adapter.audit()
        if audit['status'] != 'PASS': raise AssertionError(audit)
        evidence['C17'] = {'status': 'PASS', 'boundary_fixture': list(zip(ids, stamps)),
                           'first': first, 'second': second, 'additional': additional, 'audit': audit,
                           'scope': 'KST timestamp fixture and immutable additional snapshot'}
    except Exception as error:
        evidence['C17'] = {'status': 'FAIL', 'error': str(error)}
    bank_container = project + '-bank-1'
    try:
        docker('kill', '--signal=KILL', bank_container)
        r = api.pay('late-success', user=900021, agency=921, amount=107)
        if r['status'] != 202: raise AssertionError(r)
        pid = r['body']['paymentId']
        adapter.sql("UPDATE payments SET created_at='2020-01-01T14:59:59Z'::timestamptz WHERE id='" + pid + "'::uuid")
        date = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date().isoformat()
        body = {'agencyId': 921, 'businessDate': date}
        empty = api.request('/api/settlements', body, 921, 'late-before')
        if empty['body'].get('paymentCount') != 0: raise AssertionError(empty)
        docker('start', bank_container)
        final = api.final({'body': {'paymentId': pid}}, agency=921)
        if final['status'] != 'SUCCEEDED': raise AssertionError(final)
        recorded = adapter.sql("SELECT (succeeded_at AT TIME ZONE 'Asia/Seoul')::date FROM payments WHERE id='" + pid + "'::uuid")
        if recorded != date: raise AssertionError('success date not recovery date: ' + recorded)
        replay = api.request('/api/settlements', body, 921, 'late-before')
        additional = api.request('/api/settlements', body, 921, 'late-after')
        if replay['body'] != empty['body'] or additional['body'].get('paymentCount') != 1 or additional['body'].get('grossAmount') != 107:
            raise AssertionError([replay, additional])
        old = api.request('/api/settlements', {'agencyId': 921, 'businessDate': '2020-01-01'}, 921, 'late-old')
        if old['body'].get('paymentCount') != 0: raise AssertionError(old)
        evidence['C17']['late_success'] = {'status': 'PASS', 'recorded_date': recorded, 'before': empty, 'after': additional, 'old': old}
    except Exception as error:
        evidence.setdefault('C17', {})['status'] = 'FAIL'
        evidence['C17']['late_success'] = {'status': 'FAIL', 'error': str(error)}
    finally:
        docker('start', bank_container)
    try:
        evidence['final_audit'] = adapter.audit()
    except Exception as error:
        evidence['final_audit'] = {'status': 'FAIL', 'error': str(error)}
    return {'cases': evidence, 'requests': api.requests}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--base-url', required=True)
    p.add_argument('--namespace', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise SystemExit('refusing to overwrite evidence: %s' % args.output)
    report = run(args.base_url, args.namespace, args.project)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v.get('status') for k, v in report['cases'].items()}))
    raise SystemExit(any(c.get('status') == 'FAIL' for c in report['cases'].values()))
