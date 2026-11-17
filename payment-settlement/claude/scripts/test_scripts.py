"""No-cost tests for the Claude-arm scripts (no docker, no model call)."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture_turn
import consult_contract
import freeze_readiness
import preflight
import start_cycle

USAGE = {'input_tokens': 1, 'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 3, 'output_tokens': 4}
READY_FLAGS = dict(isolation_verified=True, usage_collector_verified=True, harness_adapter_verified=True,
                   acceptance_suite_verified=True, pilot_passed=True, oauth_token_source_verified=True,
                   role_models_verified=True, cache_observation_verified=True, hook_enforcement_verified=True)


def ready(**extra):
    data = dict(protocol='payment-settlement-v3-claude', provider='claude-code', auth='subscription-oauth-token',
                reasoning_effort='high', cache_policy='observed-cache', billed_cost=None, baseline_sha='abc',
                contract_sha256='def', image_digests={'runner': 'sha256:1'}, token_limit=20000000,
                time_limit_seconds=28800, feedback_limit=30,
                actual_models={'1': 'claude-opus-5-5', '2': 'claude-opus-5-5', '3': 'claude-fable-5-2'},
                user_proxy_model='claude-opus-5-5', **READY_FLAGS)
    data.update(extra)
    return data


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


class PreflightTest(unittest.TestCase):
    def test_protocol_and_limits_are_part_of_the_gate(self):
        base = dict(ready(), arm=1, actual_model='claude-opus-5-5')
        self.assertEqual(preflight.blockers(base), [])
        self.assertIn('protocol must be payment-settlement-v3-claude', preflight.blockers(dict(base, protocol='payment-settlement-v1')))
        self.assertIn('token_limit must be 20000000', preflight.blockers(dict(base, token_limit=2000000)))
        arm3 = dict(base, arm=3, requested_worker_model='claude-opus-5-5', user_proxy_model='claude-opus-5-5')
        self.assertEqual(preflight.blockers(arm3), [])
        for other in (None, 'claude-fable-5-2', 'claude-opus-5-5-x'):
            self.assertIn('arm 3 requires user_proxy_model claude-opus-5-5', preflight.blockers(dict(arm3, user_proxy_model=other)))
        self.assertEqual(preflight.blockers(dict(base, user_proxy_model=None)), [])     # arms 1/2 never consult a proxy
        self.assertTrue(preflight.blockers(dict(arm3, hook_enforcement_verified='yes')))


class PreflightStageTest(unittest.TestCase):
    def test_pilot_stage_waives_only_the_pilot_result(self):
        base = dict(ready(), arm=3, actual_model='claude-fable-5-2', requested_worker_model='claude-opus-5-5', pilot_passed=False)
        self.assertEqual(preflight.blockers(base, 'pilot'), [])
        self.assertIn('pilot_passed: must be boolean true (got False)', preflight.blockers(base))       # default = cycle
        self.assertTrue(preflight.blockers(dict(base, role_models_verified=False), 'pilot'))
        self.assertTrue(preflight.blockers(dict(base, hook_enforcement_verified='true'), 'pilot'))
        with self.assertRaises(ValueError):
            preflight.blockers(base, 'anything')
        self.assertTrue(start_cycle.gate(dict(ready(), pilot_passed=False), 1))   # cycles always use the full gate


class FreezeTest(unittest.TestCase):
    def smoke(self, runs, run_id, arm, roles, selftest=None, usage_complete=True, credentials=False):
        requested = 'claude-fable-5-2' if arm == 3 else 'claude-opus-5-5'
        write(runs / run_id / 'manifest.json', dict(
            arm=arm, requested_model=requested, requested_worker_model='claude-opus-5-5' if arm == 3 else None,
            observed_tools=list(freeze_readiness.launch.ALLOWED_TOOLS_ARM3 if arm == 3 else freeze_readiness.launch.ALLOWED_TOOLS_ARM12),
            requested_reviewer_model='claude-fable-5-2' if arm == 3 else None, actual_role_models=roles,
            selftest=selftest or dict(passed=True, gate_l1_write_exit='2', gate_docs_write_exit='0', git_guard_push='ask',
                                      harness_hash_match='1', reviewer_on_path='1')))
        write(runs / run_id / 'turns/0001/result.json', dict(
            status='ok', session_id_confirmed=True, usage_complete=usage_complete,
            config_volume=dict(credentials_file_in_volume=credentials), usage_cross_check=dict(reported=USAGE)))

    def test_flags_derive_only_from_smoke_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            self.smoke(runs, 's1', 1, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 's2', 2, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 's3', 3, {'pm': ['claude-fable-5-2-20261101'], 'reviewer': ['claude-fable-5-2']})
            flags, actual, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'])
            self.assertTrue(all(flags.values()), flags)
            self.assertEqual(actual, {'1': 'claude-opus-5-5', '2': 'claude-opus-5-5', '3': 'claude-fable-5-2-20261101'})
            self.assertEqual(detail['s3']['role_model_check'], {'pm': True, 'reviewer': True})
            # required main role missing → not verified; a requested-and-served model other than the agreed one → not verified
            self.smoke(runs, 'n3', 3, {'reviewer': ['claude-fable-5-2']})
            self.assertEqual(freeze_readiness.smoke_evidence(runs, ['s1', 'n3'])[2]['n3']['role_model_check'],
                             {'reviewer': True, 'pm': False})
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 'n3'])[0]['role_models_verified'])
            self.smoke(runs, 'x1', 1, {'implementer': ['claude-opus-5-5x']})
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['x1', 's3'])[0]['role_models_verified'])
            # arm 3 tools must be exactly arms 1/2 tools plus the subagent tool
            manifest3 = json.loads((runs / 's3/manifest.json').read_text())
            manifest3['observed_tools'] += ['Skill']
            write(runs / 's3/manifest.json', manifest3)
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'])[0]['isolation_verified'])
            # wrong worker model, missing arm 3, incomplete usage, credentials file present: each flips its flag
            self.smoke(runs, 'w3', 3, {'pm': ['claude-fable-5-2'], 'worker': ['claude-sonnet-5']})
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 'w3'])[0]['role_models_verified'])
            only1 = freeze_readiness.smoke_evidence(runs, ['s1'])[0]
            self.assertFalse(only1['isolation_verified'] or only1['hook_enforcement_verified'] or only1['role_models_verified'])
            self.smoke(runs, 'u1', 1, {'implementer': ['claude-opus-5-5']}, usage_complete=False, credentials=None)
            bad = freeze_readiness.smoke_evidence(runs, ['u1', 's3'])[0]
            self.assertFalse(bad['usage_collector_verified'])
            self.assertFalse(bad['oauth_token_source_verified'])         # unknown (None) is not "absent"
            self.assertFalse(any(freeze_readiness.smoke_evidence(runs, [])[0].values()))

    def test_attestation_is_limited_and_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / 'evidence.json'
            evidence.write_text('{}')
            attest = Path(tmp) / 'attest.json'
            with patch.object(freeze_readiness, 'REPO', Path(tmp).resolve()):
                attest.write_text(json.dumps({'acceptance_suite_verified': {'value': True, 'evidence': ['evidence.json']}}))
                flags, hashes = freeze_readiness.attested(attest)
                self.assertEqual(flags, {'acceptance_suite_verified': True})
                self.assertEqual(list(hashes['acceptance_suite_verified']), ['evidence.json'])
                for bad in ({'isolation_verified': {'value': True, 'evidence': ['evidence.json']}},
                            {'pilot_passed': {'value': True, 'evidence': ['evidence.json']}},     # derived, never attested
                            {'acceptance_suite_verified': {'value': True, 'evidence': []}},
                            {'acceptance_suite_verified': {'value': True, 'evidence': ['../outside.json']}}):
                    attest.write_text(json.dumps(bad))
                    with self.assertRaises(ValueError):
                        freeze_readiness.attested(attest)

    def pilot(self, runs, roles=('pm', 'worker', 'reviewer'), models=None, last='ok', pending=False, censored=False, arm=3):
        run = runs / 'pilot'
        models = models or {'pm': ['claude-fable-5-2'], 'worker': ['claude-opus-5-5'], 'reviewer': ['claude-fable-5-2']}
        write(run / 'manifest.json', dict(arm=arm, actual_role_models=models))
        for n, status in ((1, 'ok'), (2, last)):
            t = run / 'turns' / ('%04d' % n)
            write(t / 'result.json', dict(status=status, usage_complete=status == 'ok', reviewer_usage_pending=pending,
                                         cost={'pinned_session_usd': 0.1, 'reviewer_usd': 0.0, 'reviewer_applicable': True}))
            agreed = {'pm': 'claude-fable-5-2', 'worker': 'claude-opus-5-5', 'reviewer': 'claude-fable-5-2'}
            (t / 'events.jsonl').write_text(''.join(json.dumps(dict(type='message', id='%s-%d' % (r, n), model=agreed[r], role=r,
                                                                    turn=t.name, usage=USAGE)) + '\n' for r in roles))
        if censored:
            write(run / 'censored.json', {'status': 'CENSORED_TIME'})
        return 'pilot'

    def test_pilot_pass_is_derived_from_the_pilot_run(self):
        cases = {'complete pilot': ({}, True), 'no worker usage': ({'roles': ('pm', 'reviewer')}, False),
                 'no reviewer usage': ({'roles': ('pm', 'worker')}, False),
                 'reviewer pending': ({'pending': True}, False), 'last turn failed': ({'last': 'failed'}, False),
                 'censored': ({'censored': True}, False), 'not arm 3': ({'arm': 1}, False),
                 'worker on another model': ({'models': {'pm': ['claude-fable-5-2'], 'worker': ['claude-sonnet-5'],
                                                         'reviewer': ['claude-fable-5-2']}}, False),
                 'reviewer model unobserved': ({'models': {'pm': ['claude-fable-5-2'], 'worker': ['claude-opus-5-5']}}, False)}
        for name, (kw, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                passed, detail = freeze_readiness.pilot_evidence(Path(tmp), self.pilot(Path(tmp), **kw))
                self.assertIs(passed, expected, detail['checks'])

    def test_arm3_workers_explorer_on_sonnet_others_on_opus(self):
        def events(*rows):
            return [dict(role='worker', model=m, agent_type=t) for m, t in rows]
        check = freeze_readiness.worker_requests_check
        self.assertTrue(check(events(('claude-opus-5-5', 'general-purpose'), ('claude-sonnet-5', 'explorer'), ('claude-opus-5-5', None))))
        self.assertFalse(check(events(('claude-sonnet-5', None))))                 # unidentified Sonnet is not an explorer
        self.assertFalse(check(events(('claude-sonnet-5', 'general-purpose'))))
        self.assertFalse(check(events(('claude-opus-5-5', 'explorer'))))           # explorer keeps the harness definition
        self.assertFalse(check([]))
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'pilot'
            self.pilot(Path(tmp))
            t = run / 'turns/0003'
            write(t / 'result.json', dict(status='ok', usage_complete=True, cost={'pinned_session_usd': 0.1, 'reviewer_usd': 0.0, 'reviewer_applicable': True}))
            (t / 'events.jsonl').write_text(json.dumps(dict(type='message', id='ex', model='claude-sonnet-5', role='worker',
                                                           agent_type='explorer', turn='0003', usage=USAGE)) + '\n')
            manifest = json.loads((run / 'manifest.json').read_text())
            manifest['actual_role_models']['worker'] = ['claude-opus-5-5', 'claude-sonnet-5']
            write(run / 'manifest.json', manifest)
            passed, detail = freeze_readiness.pilot_evidence(Path(tmp), 'pilot')
            self.assertTrue(passed, detail)
            (t / 'events.jsonl').write_text(json.dumps(dict(type='message', id='ex', model='claude-sonnet-5', role='worker',
                                                           agent_type=None, turn='0003', usage=USAGE)) + '\n')
            self.assertFalse(freeze_readiness.pilot_evidence(Path(tmp), 'pilot')[0])
            (t / 'events.jsonl').write_text('')
            consult = run / 'consults/0001'
            consult.mkdir(parents=True)
            write(consult / 'usage.json', {'usage_complete': True, 'cost_usd': 0.0})
            (consult / 'events.jsonl').write_text(json.dumps(dict(type='message', id='px', model='claude-fable-5-2',
                                                                 role='user-proxy', turn='p', usage=USAGE)) + '\n')
            passed, detail = freeze_readiness.pilot_evidence(Path(tmp), 'pilot')
            self.assertFalse(passed)
            self.assertFalse(detail['role_model_check']['user-proxy'])

    def test_model_match(self):
        self.assertTrue(freeze_readiness.model_matches(['claude-opus-5-5'], 'claude-opus-5-5'))
        self.assertTrue(freeze_readiness.model_matches(['claude-opus-5-5-20261101'], 'claude-opus-5-5'))
        self.assertFalse(freeze_readiness.model_matches(['claude-opus-5-50'], 'claude-opus-5-5'))
        self.assertEqual(freeze_readiness.AGREED[(3, 'reviewer')], 'claude-fable-5-2')
        self.assertFalse(freeze_readiness.model_matches(['claude-opus-5-5', 'claude-opus-5'], 'claude-opus-5-5'))
        self.assertFalse(freeze_readiness.model_matches(['claude-opus-5'], 'claude-opus-5-5'))
        self.assertFalse(freeze_readiness.model_matches(['x'], None))


class FreezeArgumentsTest(unittest.TestCase):
    def test_user_proxy_model_is_the_agreed_constant_not_an_argument(self):
        self.assertEqual(freeze_readiness.AGREED[(3, 'user-proxy')], 'claude-opus-5-5')
        err = io.StringIO()
        with patch.object(sys, 'argv', ['freeze_readiness.py', '--user-proxy-model', 'other']), \
             contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            freeze_readiness.main()
        self.assertIn('unrecognized arguments', err.getvalue())


class StartCycleTest(unittest.TestCase):
    def test_gate_is_per_arm_and_dry_run_launches_nothing(self):
        self.assertEqual(start_cycle.gate(ready(), 1), [])
        failures = start_cycle.gate(ready(pilot_passed=False, actual_models={'1': 'claude-opus-5-5'}), 1)
        self.assertTrue(any(f.startswith('arm 3: actual_model') for f in failures))
        self.assertTrue(any('pilot_passed' in f for f in failures))
        self.assertEqual(start_cycle.run_plan(2, 3), ('claude-arm3-core02', 'payment-claude-c02-a3',
                                                      {'APP_PORT': '38023', 'DB_PORT': '39023', 'BANK_PORT': '40023'}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'readiness.json'
            frozen = freeze_readiness.inventory()
            write(path, ready(contract_sha256=__import__('hashlib').sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()))
            argv = ['start_cycle.py', '--cycle', '1', '--readiness', str(path), '--env-file', '/nonexistent', '--dry-run']
            out = io.StringIO()
            with patch.object(sys, 'argv', argv), patch.object(start_cycle.subprocess, 'run') as run, contextlib.redirect_stdout(out):
                self.assertEqual(start_cycle.main(), 0)
                run.assert_not_called()
            plan = json.loads(out.getvalue()[out.getvalue().index('{'):])     # lab.verify prints PASS lines first
            self.assertEqual([r['run_id'] for r in plan['runs']], ['claude-arm1-core01', 'claude-arm2-core01', 'claude-arm3-core01'])
            self.assertEqual(plan['image'], 'sha256:1')


class CycleOrderTest(unittest.TestCase):
    def test_next_cycle_needs_terminal_committed_clean_and_discarded_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'claude'
            for cycle in (1, 2):
                for arm in (1, 2, 3):
                    write(root / 'docs/experiments' / str(arm) / ('core%d' % cycle) / 'run.json',
                          {'status': 'PASSED' if cycle == 1 else 'NOT_STARTED'})
            state = {'subjects': ['experiment: claude-arm%d-core01 PASSED' % a for a in (1, 2, 3)], 'dirty': ''}
            def fake_git(*args):
                return '\n'.join(state['subjects']) + '\n' if args[0] == 'log' else state['dirty']
            def discard(run_id, complete):
                write(root / '.runs' / run_id / 'discard-2026.json', {'complete': complete})
            with patch.object(start_cycle, 'ROOT', root), patch.object(start_cycle, 'git', side_effect=fake_git):
                for arm in (1, 2, 3):
                    discard('claude-arm%d-core01' % arm, True)
                self.assertEqual(start_cycle.cycle_order_blockers(2), [])
                state['subjects'] = state['subjects'][:2]
                self.assertEqual(start_cycle.cycle_order_blockers(2), ['claude-arm3-core01: no run commit'])
                state['subjects'] += ['experiment: claude-arm3-core01 PASSED']
                state['dirty'] = ' M x'
                self.assertEqual(len(start_cycle.cycle_order_blockers(2)), 3)
                state['dirty'] = ''
                discard('claude-arm2-core01', False)
                self.assertEqual(start_cycle.cycle_order_blockers(2), ['claude-arm2-core01: volumes not removed (launch.py discard incomplete or missing)'])
                discard('claude-arm2-core01', True)
                write(root / 'docs/experiments/1/core1/run.json', {'status': 'RUNNING'})
                self.assertEqual(start_cycle.cycle_order_blockers(2), ['claude-arm1-core01: status RUNNING is not terminal'])
                write(root / 'docs/experiments/1/core1/run.json', {'status': 'CENSORED_TIME'})
                (root / '.runs/claude-arm2-core02').mkdir(parents=True)          # this cycle already has a run
                self.assertEqual(start_cycle.cycle_order_blockers(2), ['claude-arm2-core02: already started (status NOT_STARTED); recover its exact session instead'])


class ContractDriftTest(unittest.TestCase):
    def test_cycle_refuses_inputs_that_changed_after_the_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp)
            (shared / 'docs').mkdir()
            (shared / 'docs/acceptance.md').write_text('C01 v1')
            with patch.object(freeze_readiness, 'SHARED', shared):
                frozen = freeze_readiness.inventory()
                digest = __import__('hashlib').sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
                listing = shared / 'frozen-inputs.json'
                listing.write_text(json.dumps(frozen))
                self.assertEqual(start_cycle.contract_drift({'contract_sha256': digest}, listing), [])
                (shared / 'docs/acceptance.md').write_text('C01 v2')
                self.assertEqual(start_cycle.contract_drift({'contract_sha256': digest}, listing), ['docs/acceptance.md'])
                self.assertTrue(start_cycle.contract_drift({'contract_sha256': digest}, None))
            path = shared / 'readiness.json'
            write(path, ready(contract_sha256='0' * 64))
            argv = ['start_cycle.py', '--cycle', '1', '--readiness', str(path), '--env-file', '/nonexistent', '--dry-run']
            with patch.object(sys, 'argv', argv), patch.object(start_cycle.subprocess, 'run') as run, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                start_cycle.main()
            run.assert_not_called()


class CyclePartialFailureTest(unittest.TestCase):
    def run_cycle(self, tmp, fail_on):
        root = Path(tmp) / 'claude'
        for arm in (1, 2, 3):
            write(root / 'docs/experiments' / str(arm) / 'core1/run.json',
                  dict(status='NOT_STARTED', arm=arm, protocol='payment-settlement-v3-claude'))
        write(root / 'docs/results/cycles.json', [dict(cycle=c, status='NOT_STARTED') for c in range(1, 11)])
        readiness = Path(tmp) / 'readiness.json'
        write(readiness, ready())
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            text = ' '.join(str(c) for c in cmd)
            return SimpleNamespace(returncode=1 if fail_on and fail_on(text) else 0)
        argv = ['start_cycle.py', '--cycle', '1', '--readiness', str(readiness), '--env-file', '/x.env']
        with patch.object(start_cycle, 'ROOT', root), patch.object(sys, 'argv', argv), \
             patch.object(start_cycle, 'gate', return_value=[]), patch.object(start_cycle, 'contract_drift', return_value=[]), \
             patch.object(start_cycle.subprocess, 'run', side_effect=fake_run), patch.object(start_cycle.lab, 'verify'), \
             patch.object(start_cycle.lab, 'prompt', side_effect=lambda arm: 'p'), contextlib.redirect_stdout(io.StringIO()):
            code = start_cycle.main()
        return code, root, calls

    def test_setup_failure_of_one_arm_is_recorded_and_no_turn_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, root, calls = self.run_cycle(tmp, lambda t: 'compose' in t and 'c01-a2' in t)
            self.assertEqual(code, 1)
            self.assertFalse(any('turn' in c for c in calls))
            run2 = json.loads((root / 'docs/experiments/2/core1/run.json').read_text())
            self.assertEqual(run2['status'], 'SETUP_FAILED')
            self.assertIn('compose up exited 1', run2['setup_error'])
            for arm in (1, 3):
                self.assertEqual(json.loads((root / ('docs/experiments/%d/core1/run.json' % arm)).read_text())['status'], 'NOT_STARTED')
            cycle = json.loads((root / 'docs/results/cycles.json').read_text())[0]
            self.assertEqual(cycle['status'], 'SETUP_FAILED')
            self.assertEqual(list(cycle['setup_failures']), ['claude-arm2-core01'])

    def test_failed_initial_turn_is_kept_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, root, calls = self.run_cycle(tmp, lambda t: ' turn ' in t and 'claude-arm3-core01' in t)
            self.assertEqual(code, 1)
            run3 = json.loads((root / 'docs/experiments/3/core1/run.json').read_text())
            self.assertEqual((run3['status'], run3['initial_turn_exit'], run3['initial_turn_failed']), ('RUNNING', 1, True))
            run1 = json.loads((root / 'docs/experiments/1/core1/run.json').read_text())
            self.assertEqual(run1['initial_turn_exit'], 0)
            cycle = json.loads((root / 'docs/results/cycles.json').read_text())[0]
            self.assertEqual(cycle['initial_turn_exits']['claude-arm3-core01'], 1)


class CaptureTurnTest(unittest.TestCase):
    def test_capture_normalizes_runner_events_once_with_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp)
            root = shared / 'claude'
            shutil.copytree(capture_turn.ROOT.parent / 'recording', shared / 'recording',
                            ignore=shutil.ignore_patterns('__pycache__'))
            sys.path.insert(0, str(shared / 'recording'))
            spec_record = __import__('importlib').util.spec_from_file_location('rec', shared / 'recording/record.py')
            record = __import__('importlib').util.module_from_spec(spec_record)
            spec_record.loader.exec_module(record)
            records = root / 'docs/experiments/3/core1'
            record.init_run(records)
            run = root / '.runs/claude-arm3-core01'
            session = '11111111-2222-4333-8444-555555555555'
            write(run / 'manifest.json', dict(arm=3, core=1, session_id=session, requested_model='claude-fable-5-2',
                                              requested_worker_model='claude-opus-5-5', requested_reviewer_model='claude-fable-5-2'))
            events = [dict(type='message', id='r-pm', model='claude-fable-5-2', usage=USAGE, role='pm'),
                      dict(type='message', id='r-wk', model='claude-opus-5-5', usage=USAGE, role='worker'),
                      dict(type='message', id='r-rv', model='claude-fable-5-2', usage=USAGE, role='reviewer')]
            def turn(number, rows, complete):
                raw = run / 'turns' / ('%04d' % number)
                write(raw / 'result.json', dict(status='ok', session_id=session, event_count=len(rows), usage_complete=complete))
                for name in ('prompt.txt', 'invocation.json', 'stderr.txt'):
                    (raw / name).write_text('x')
                (raw / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in [
                    dict(type='assistant', message=dict(content=[dict(type='text', text='PM says')])),
                    dict(type='assistant', parent_tool_use_id='t1', message=dict(content=[dict(type='text', text='worker says')]))]) + '\n')
                (raw / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in rows))
            turn(1, events, True)
            analysis = shared / 'analysis.md'
            analysis.write_text('analysis')
            with patch.object(capture_turn, 'ROOT', root):
                capture_turn.capture('claude-arm3-core01', 1, analysis, 'ITERATING')
                rows = [json.loads(l) for l in (records / 'turns/0001/usage.jsonl').read_text().splitlines()]
                self.assertEqual([(r['event_id'], r['role'], r['model_label']) for r in rows],
                                 [('r-pm', 'pm', 'claude-fable-5-2'), ('r-wk', 'worker', 'claude-opus-5-5'),
                                  ('r-rv', 'reviewer', 'claude-fable-5-2')])
                self.assertEqual(rows[0]['input_tokens'], 6)               # anthropic input = input + cache read + creation
                self.assertEqual((records / 'turns/0001/response.txt').read_text(), 'PM says')
                turn(2, events[:1] + [dict(type='message', id='r-pm2', model='claude-fable-5-2', usage=USAGE, role='pm')], False)
                analysis.write_text('analysis 2')
                capture_turn.capture('claude-arm3-core01', 2, analysis, 'ITERATING')
                rows2 = [json.loads(l)['event_id'] for l in (records / 'turns/0002/usage.jsonl').read_text().splitlines()]
                self.assertEqual(rows2, ['r-pm2'])                          # already captured request not recounted
                summary = json.loads((records / 'usage-summary.json').read_text())
                self.assertFalse(summary['usage_complete'])
                # the run ledger (all roles, consultations included) travels with the records
                consult = run / 'consults/0001'
                consult.mkdir(parents=True)
                (consult / 'events.jsonl').write_text(json.dumps(dict(type='message', id='r-px', model='x', role='user-proxy',
                                                                     turn='p', usage=USAGE)) + '\n')
                (consult / 'usage.json').write_text(json.dumps({'usage_complete': True, 'cost_usd': 0.01}))
                turn(3, [dict(type='message', id='r-pm3', model='claude-fable-5-2', usage=USAGE, role='pm')], True)
                analysis.write_text('analysis 3')
                capture_turn.capture('claude-arm3-core01', 3, analysis, 'ITERATING')
                ledger = json.loads((records / 'ledger.json').read_text())
                self.assertEqual(sorted(ledger['roles']), ['pm', 'reviewer', 'user-proxy', 'worker'])
                self.assertEqual(ledger['roles']['pm']['requests'], 3)
                with self.assertRaises(FileExistsError):
                    capture_turn.capture('claude-arm3-core01', 3, analysis, 'ITERATING')


class ConsultContractTest(unittest.TestCase):
    def setup_run(self, tmp, arm=3, proxy_model='claude-opus-5-5'):
        env = Path(tmp) / 'claude.env'
        env.write_text('CLAUDE_CODE_OAUTH_TOKEN=DO-NOT-PRINT-0123456789\n')
        os.chmod(env, 0o600)
        questions = Path(tmp) / 'q.txt'
        questions.write_text('질문')
        path = Path(tmp) / 'readiness.json'
        write(path, ready(user_proxy_model=proxy_model))
        runs = Path(tmp) / 'runs'
        write(runs / 'claude-arm3-core01' / 'manifest.json', {'arm': arm, 'run_id': 'claude-arm3-core01'})
        (runs / 'claude-arm3-core01' / 'turns').mkdir()
        return ['consult_contract.py', '--run-id', 'claude-arm3-core01', '--questions', str(questions), '--env-file', str(env),
                '--readiness', str(path), '--runs', str(runs)], runs / 'claude-arm3-core01'

    def call(self, argv, fake=None):
        out = io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(consult_contract.subprocess, 'run', side_effect=fake) as run, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = consult_contract.main()
            except SystemExit as exc:
                code = exc.code
        return code, run, out.getvalue()

    def fake(self, copy_ok=True, reply='APPROVED', start_rc=0):
        state = {}
        def run(cmd, **kw):
            if cmd[:2] == ['docker', 'create']:
                state['session'] = cmd[cmd.index('--session-id') + 1]
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'start']:
                kw['stdout'].write(json.dumps({'type': 'result', 'result': reply, 'session_id': state['session'],
                                               'usage': USAGE, 'total_cost_usd': 0.07}).encode() + b'\n')
                return SimpleNamespace(returncode=start_rc)
            if cmd[:2] == ['docker', 'cp']:
                if not copy_ok:
                    return SimpleNamespace(returncode=1, stdout=b'', stderr=b'copy failed')
                target = Path(cmd[-1]) / '-tmp'
                target.mkdir(parents=True)
                (target / (state['session'] + '.jsonl')).write_text(json.dumps(
                    {'type': 'assistant', 'requestId': 'req-proxy', 'sessionId': state['session'],
                     'message': {'model': 'claude-opus-5-5', 'usage': USAGE}}) + '\n')
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        return run

    def test_proxy_has_no_tools_no_mounts_and_dry_run_spends_nothing(self):
        cmd = consult_contract.proxy_command('img', 'n', Path('/e'), 'proxy-model', 'sid')
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertNotIn('--mount', cmd)
        self.assertNotIn('--dangerously-skip-permissions', cmd)
        self.assertEqual(cmd[cmd.index('--model') + 1], 'proxy-model')
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = self.setup_run(tmp)
            code, run, out = self.call(argv + ['--model', 'claude-opus-5-5', '--dry-run'])
            self.assertEqual(code, 0, out)
            run.assert_not_called()
            self.assertNotIn('DO-NOT-PRINT', out)
            self.assertFalse((run_dir / 'consults').exists())
            self.assertNotEqual(self.call(argv + ['--model', 'other-model', '--dry-run'])[0], 0)

    def test_proxy_model_must_be_frozen_and_only_arm3_consults(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, _ = self.setup_run(tmp, proxy_model=None)
            code, run, out = self.call(argv + ['--model', 'claude-opus-5-5', '--dry-run'])
            self.assertNotEqual(code, 0)
            self.assertIn('user_proxy_model', out)
        with tempfile.TemporaryDirectory() as tmp:                     # a readiness frozen with another model is refused too
            argv, _ = self.setup_run(tmp, proxy_model='claude-fable-5-2')
            self.assertNotEqual(self.call(argv + ['--model', 'claude-fable-5-2', '--dry-run'])[0], 0)
        with tempfile.TemporaryDirectory() as tmp:
            argv, _ = self.setup_run(tmp, arm=1)
            self.assertNotEqual(self.call(argv + ['--model', 'claude-opus-5-5', '--dry-run'])[0], 0)

    def test_proxy_call_passes_the_run_cap_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = self.setup_run(tmp)
            (run_dir / 'censored.json').write_text(json.dumps({'status': 'CENSORED_TIME'}))
            code, run, out = self.call(argv + ['--model', 'claude-opus-5-5'], self.fake())
            self.assertNotEqual(code, 0)
            self.assertIn('CENSORED_TIME', out)
            run.assert_not_called()
            self.assertFalse((run_dir / 'consults').exists())

    def test_proxy_usage_lands_in_the_run_ledger_and_unobserved_usage_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = self.setup_run(tmp)
            code, _, out = self.call(argv + ['--model', 'claude-opus-5-5'], self.fake())
            self.assertEqual(code, 0, out)
            usage = json.loads((run_dir / 'consults/0001/usage.json').read_text())
            self.assertEqual(usage['usage'], USAGE)
            self.assertEqual(usage['cost_usd'], 0.07)
            self.assertTrue(usage['usage_complete'])
            ledger = json.loads((run_dir / 'ledger.json').read_text())
            self.assertEqual(ledger['roles']['user-proxy']['requests'], 1)
            self.assertEqual(ledger['cost_usd'], 0.07)
            code, _, out = self.call(argv + ['--model', 'claude-opus-5-5'], self.fake(copy_ok=False))
            usage = json.loads((run_dir / 'consults/0002/usage.json').read_text())
            self.assertIsNone(usage['usage'])
            self.assertTrue(usage['usage_unobserved'])
            self.assertFalse(usage['usage_complete'])
            ledger = json.loads((run_dir / 'ledger.json').read_text())
            self.assertFalse(ledger['usage_complete'])
            self.assertIn('consult/0002', ledger['incomplete_calls'])


    def test_failed_or_empty_consultation_is_recorded_as_not_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = self.setup_run(tmp)
            code, _, out = self.call(argv + ['--model', 'claude-opus-5-5'], self.fake())
            self.assertEqual(code, 0, out)
            self.assertIs(json.loads((run_dir / 'consults/0001/usage.json').read_text())['completed'], True)
            for n, fake in ((2, self.fake(reply='')), (3, self.fake(start_rc=1))):
                code, _, out = self.call(argv + ['--model', 'claude-opus-5-5'], fake)
                self.assertNotEqual(code, 0)
                self.assertIn('not completed', out)
                self.assertIs(json.loads((run_dir / ('consults/%04d/usage.json' % n)).read_text())['completed'], False)
            ledger = json.loads((run_dir / 'ledger.json').read_text())
            self.assertEqual((ledger['consults'], ledger['consults_completed'], ledger['consults_not_completed']),
                             (3, 1, ['0002', '0003']))


class LoadCheckTest(unittest.TestCase):
    def test_only_claude_owned_projects(self):
        done = subprocess.run([sys.executable, str(HERE / 'load_check.py'), '--project', 'payment-codex-c01-a1',
                               '--output', '/nonexistent', '--pending-sql-file', '/dev/null', '--audit-command-json', '[]'],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn('payment-claude-*', done.stderr)


if __name__ == '__main__':
    unittest.main()
