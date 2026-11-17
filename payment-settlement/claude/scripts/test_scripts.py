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
                actual_models={'1': 'claude-opus-5-5', '2': 'claude-opus-5-5', '3': 'claude-fable-5-2'}, **READY_FLAGS)
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
        arm3 = dict(base, arm=3, requested_worker_model='claude-opus-5-5')
        self.assertEqual(preflight.blockers(arm3), [])
        self.assertTrue(preflight.blockers(dict(arm3, hook_enforcement_verified='yes')))


class FreezeTest(unittest.TestCase):
    def smoke(self, runs, run_id, arm, roles, selftest=None, usage_complete=True, credentials=False):
        requested = 'claude-fable-5-2' if arm == 3 else 'claude-opus-5-5'
        write(runs / run_id / 'manifest.json', dict(
            arm=arm, requested_model=requested, requested_worker_model='claude-opus-5-5' if arm == 3 else None,
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
            self.smoke(runs, 's3', 3, {'pm': ['claude-fable-5-2-20261101'], 'reviewer': ['claude-fable-5-2']})
            flags, actual, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's3'])
            self.assertTrue(all(flags.values()), flags)
            self.assertEqual(actual, {'1': 'claude-opus-5-5', '3': 'claude-fable-5-2-20261101'})
            self.assertEqual(detail['s3']['role_model_check'], {'pm': True, 'reviewer': True})
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
                attest.write_text(json.dumps({'pilot_passed': {'value': True, 'evidence': ['evidence.json']}}))
                flags, hashes = freeze_readiness.attested(attest)
                self.assertEqual(flags, {'pilot_passed': True})
                self.assertEqual(list(hashes['pilot_passed']), ['evidence.json'])
                for bad in ({'isolation_verified': {'value': True, 'evidence': ['evidence.json']}},
                            {'pilot_passed': {'value': True, 'evidence': []}},
                            {'pilot_passed': {'value': True, 'evidence': ['../outside.json']}}):
                    attest.write_text(json.dumps(bad))
                    with self.assertRaises(ValueError):
                        freeze_readiness.attested(attest)

    def test_model_match(self):
        self.assertTrue(freeze_readiness.model_matches(['claude-opus-5-5'], 'claude-opus-5-5'))
        self.assertFalse(freeze_readiness.model_matches(['claude-opus-5-5', 'claude-opus-5'], 'claude-opus-5-5'))
        self.assertFalse(freeze_readiness.model_matches(['claude-opus-5'], 'claude-opus-5-5'))
        self.assertFalse(freeze_readiness.model_matches(['x'], None))


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
            write(path, ready())
            argv = ['start_cycle.py', '--cycle', '1', '--readiness', str(path), '--env-file', '/nonexistent', '--dry-run']
            out = io.StringIO()
            with patch.object(sys, 'argv', argv), patch.object(start_cycle.subprocess, 'run') as run, contextlib.redirect_stdout(out):
                self.assertEqual(start_cycle.main(), 0)
                run.assert_not_called()
            plan = json.loads(out.getvalue()[out.getvalue().index('{'):])     # lab.verify prints PASS lines first
            self.assertEqual([r['run_id'] for r in plan['runs']], ['claude-arm1-core01', 'claude-arm2-core01', 'claude-arm3-core01'])
            self.assertEqual(plan['image'], 'sha256:1')


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
                with self.assertRaises(FileExistsError):
                    capture_turn.capture('claude-arm3-core01', 2, analysis, 'ITERATING')


class ConsultContractTest(unittest.TestCase):
    def setup_run(self, tmp, arm=3, proxy_model='decided-model'):
        env = Path(tmp) / 'claude.env'
        env.write_text('CLAUDE_CODE_OAUTH_TOKEN=DO-NOT-PRINT-0123456789\n')
        os.chmod(env, 0o600)
        questions = Path(tmp) / 'q.txt'
        questions.write_text('질문')
        path = Path(tmp) / 'readiness.json'
        write(path, ready(**({'user_proxy_model': proxy_model} if proxy_model else {})))
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

    def fake(self, copy_ok=True):
        state = {}
        def run(cmd, **kw):
            if cmd[:2] == ['docker', 'create']:
                state['session'] = cmd[cmd.index('--session-id') + 1]
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'start']:
                kw['stdout'].write(json.dumps({'type': 'result', 'result': 'APPROVED', 'session_id': state['session'],
                                               'usage': USAGE, 'total_cost_usd': 0.07}).encode() + b'\n')
                return SimpleNamespace(returncode=0)
            if cmd[:2] == ['docker', 'cp']:
                if not copy_ok:
                    return SimpleNamespace(returncode=1, stdout=b'', stderr=b'copy failed')
                target = Path(cmd[-1]) / '-tmp'
                target.mkdir(parents=True)
                (target / (state['session'] + '.jsonl')).write_text(json.dumps(
                    {'type': 'assistant', 'requestId': 'req-proxy', 'sessionId': state['session'],
                     'message': {'model': 'decided-model', 'usage': USAGE}}) + '\n')
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
            code, run, out = self.call(argv + ['--model', 'decided-model', '--dry-run'])
            self.assertEqual(code, 0, out)
            run.assert_not_called()
            self.assertNotIn('DO-NOT-PRINT', out)
            self.assertFalse((run_dir / 'consults').exists())
            self.assertNotEqual(self.call(argv + ['--model', 'other-model', '--dry-run'])[0], 0)

    def test_proxy_model_must_be_frozen_and_only_arm3_consults(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, _ = self.setup_run(tmp, proxy_model=None)
            code, run, out = self.call(argv + ['--model', 'any-model', '--dry-run'])
            self.assertNotEqual(code, 0)
            self.assertIn('user_proxy_model', out)
        with tempfile.TemporaryDirectory() as tmp:
            argv, _ = self.setup_run(tmp, arm=1)
            self.assertNotEqual(self.call(argv + ['--model', 'decided-model', '--dry-run'])[0], 0)

    def test_proxy_call_passes_the_run_cap_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = self.setup_run(tmp)
            (run_dir / 'censored.json').write_text(json.dumps({'status': 'CENSORED_TIME'}))
            code, run, out = self.call(argv + ['--model', 'decided-model'], self.fake())
            self.assertNotEqual(code, 0)
            self.assertIn('CENSORED_TIME', out)
            run.assert_not_called()
            self.assertFalse((run_dir / 'consults').exists())

    def test_proxy_usage_lands_in_the_run_ledger_and_unobserved_usage_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = self.setup_run(tmp)
            code, _, out = self.call(argv + ['--model', 'decided-model'], self.fake())
            self.assertEqual(code, 0, out)
            usage = json.loads((run_dir / 'consults/0001/usage.json').read_text())
            self.assertEqual(usage['usage'], USAGE)
            self.assertEqual(usage['cost_usd'], 0.07)
            self.assertTrue(usage['usage_complete'])
            ledger = json.loads((run_dir / 'ledger.json').read_text())
            self.assertEqual(ledger['roles']['user-proxy']['requests'], 1)
            self.assertEqual(ledger['cost_usd'], 0.07)
            code, _, out = self.call(argv + ['--model', 'decided-model'], self.fake(copy_ok=False))
            usage = json.loads((run_dir / 'consults/0002/usage.json').read_text())
            self.assertIsNone(usage['usage'])
            self.assertTrue(usage['usage_unobserved'])
            self.assertFalse(usage['usage_complete'])
            ledger = json.loads((run_dir / 'ledger.json').read_text())
            self.assertFalse(ledger['usage_complete'])
            self.assertIn('consult/0002', ledger['incomplete_calls'])


class LoadCheckTest(unittest.TestCase):
    def test_only_claude_owned_projects(self):
        done = subprocess.run([sys.executable, str(HERE / 'load_check.py'), '--project', 'payment-codex-c01-a1',
                               '--output', '/nonexistent', '--pending-sql-file', '/dev/null', '--audit-command-json', '[]'],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn('payment-claude-*', done.stderr)


if __name__ == '__main__':
    unittest.main()
