"""No-cost tests for the Claude-arm scripts (no docker, no model call)."""
import contextlib
import hashlib
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
import finish_run
import preflight
import prepare
import record_evaluation_cost
import start_cycle

IMG = 'sha256:smoke-runner'
USAGE = {'input_tokens': 1, 'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 3, 'output_tokens': 4}
READY_FLAGS = dict(isolation_verified=True, usage_collector_verified=True, harness_adapter_verified=True,
                   acceptance_suite_verified=True, pilot_passed=True, oauth_token_source_verified=True,
                   role_models_verified=True, cache_observation_verified=True, hook_enforcement_verified=True)


def ready(**extra):
    data = dict(protocol='payment-settlement-v3-claude', provider='claude-code', auth='subscription-oauth-token',
                reasoning_effort='high', cache_policy='observed-cache', billed_cost=None, baseline_sha='abc',
                contract_sha256='def', image_digests={'runner': 'sha256:1'}, token_limit=150000000,
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
        self.assertIn('token_limit must be 150000000', preflight.blockers(dict(base, token_limit=20000000)))
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

    def cli(self, tmp, readiness, *argv):
        path = Path(tmp) / 'readiness.json'
        write(path, readiness)
        out = io.StringIO()
        with patch.object(sys, 'argv', ['preflight.py', *[str(path) if a == 'R' else a for a in argv]]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = preflight.main()
            except SystemExit as exc:
                code = exc.code
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)

    def test_pilot_gate_command_merges_readiness_with_the_run_records(self):
        # readiness carries actual_models (plural) and no arm: the command merges it with each arm's run record
        with tempfile.TemporaryDirectory() as tmp:
            code, report = self.cli(tmp, ready(pilot_passed=False), '--stage', 'pilot', '--readiness', 'R', '--core', '1')
            self.assertEqual((code, report['blockers']), (0, []))
            code, report = self.cli(tmp, ready(pilot_passed=False, harness_adapter_verified=False),
                                    '--stage', 'pilot', '--readiness', 'R', '--core', '1')
            self.assertEqual(code, 2)
            self.assertEqual(sorted(report['blockers']), ['arm %d: harness_adapter_verified: must be boolean true (got False)' % a
                                                          for a in (1, 2, 3)])
            code, report = self.cli(tmp, ready(pilot_passed=False, actual_models={'1': 'claude-opus-5-5'}),
                                    '--stage', 'pilot', '--readiness', 'R', '--core', '1', '--arm', '3')
            self.assertEqual((code, report['blockers']), (2, ['arm 3: actual_model: missing/unverified']))
            code, _ = self.cli(tmp, ready(pilot_passed=False), '--readiness', 'R', '--core', '1')    # default stage = cycle
            self.assertEqual(code, 2)
            self.assertEqual(start_cycle.gate(ready(pilot_passed=False), 1),
                             preflight.readiness_blockers(ready(pilot_passed=False), 1, 'cycle'))   # one merge for both gates
            for argv in (('--readiness', 'R'), ('R', '--readiness', 'R', '--core', '1'), (), ('R', '--core', '1')):
                self.assertEqual(self.cli(tmp, ready(), *argv)[0], 2, argv)                         # usage errors


class FreezeTest(unittest.TestCase):
    REVIEWER_OK = dict(calls_this_turn=1, calls_succeeded_this_turn=1, event_count=1)

    def smoke(self, runs, run_id, arm, roles, selftest=None, usage_complete=True, credentials=False, reviewer=None):
        requested = 'claude-fable-5-2' if arm == 3 else 'claude-opus-5-5'
        write(runs / run_id / 'manifest.json', dict(
            arm=arm, requested_model=requested, requested_worker_model='claude-opus-5-5' if arm == 3 else None,
            observed_tools=list(freeze_readiness.launch.ALLOWED_TOOLS_ARM3 if arm == 3 else freeze_readiness.launch.ALLOWED_TOOLS_ARM12),
            requested_reviewer_model='claude-fable-5-2' if arm == 3 else None, actual_role_models=roles,
            selftest=selftest or dict(passed=True, gate_l1_write_exit='2', gate_docs_write_exit='0', git_guard_push='ask',
                                      harness_hash_match='1', reviewer_on_path='1')))
        if reviewer is None and arm == 3:
            reviewer = self.REVIEWER_OK
        write(runs / run_id / 'turns/0001/result.json', dict(
            status='ok', session_id_confirmed=True, usage_complete=usage_complete, reviewer=reviewer,
            roles=sorted(roles) + (['reviewer'] if reviewer and reviewer.get('event_count') and 'reviewer' not in roles else []),
            config_volume=dict(credentials_file_in_volume=credentials), usage_cross_check=dict(reported=USAGE)))
        write(runs / run_id / 'turns/0001/container-inspect.json', [{'Image': IMG}])   # ran on the image being frozen

    def test_flags_derive_only_from_smoke_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            self.smoke(runs, 's1', 1, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 's2', 2, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 's3', 3, {'pm': ['claude-fable-5-2-20261101'], 'reviewer': ['claude-fable-5-2']})
            flags, actual, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], IMG)
            self.assertTrue(all(flags.values()), flags)
            self.assertEqual(actual, {'1': 'claude-opus-5-5', '2': 'claude-opus-5-5', '3': 'claude-fable-5-2-20261101'})
            self.assertEqual(detail['s3']['role_model_check'], {'pm': True, 'reviewer': True})
            # required main role missing → not verified; a requested-and-served model other than the agreed one → not verified
            self.smoke(runs, 'n3', 3, {'reviewer': ['claude-fable-5-2']})
            self.assertEqual(freeze_readiness.smoke_evidence(runs, ['s1', 'n3'], IMG)[2]['n3']['role_model_check'],
                             {'reviewer': True, 'pm': False})
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 'n3'], IMG)[0]['role_models_verified'])
            self.smoke(runs, 'x1', 1, {'implementer': ['claude-opus-5-5x']})
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['x1', 's3'], IMG)[0]['role_models_verified'])
            # arm 3 tools must be exactly arms 1/2 tools plus the subagent tool
            manifest3 = json.loads((runs / 's3/manifest.json').read_text())
            manifest3['observed_tools'] += ['Skill']
            write(runs / 's3/manifest.json', manifest3)
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], IMG)[0]['isolation_verified'])
            # wrong worker model, missing arm 3, incomplete usage, credentials file present: each flips its flag
            self.smoke(runs, 'w3', 3, {'pm': ['claude-fable-5-2'], 'worker': ['claude-sonnet-5']})
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 'w3'], IMG)[0]['role_models_verified'])
            only1 = freeze_readiness.smoke_evidence(runs, ['s1'], IMG)[0]
            self.assertFalse(only1['isolation_verified'] or only1['hook_enforcement_verified'] or only1['role_models_verified'])
            self.smoke(runs, 'u1', 1, {'implementer': ['claude-opus-5-5']}, usage_complete=False, credentials=None)
            bad = freeze_readiness.smoke_evidence(runs, ['u1', 's3'], IMG)[0]
            self.assertFalse(bad['usage_collector_verified'])
            self.assertFalse(bad['oauth_token_source_verified'])         # unknown (None) is not "absent"
            self.assertFalse(any(freeze_readiness.smoke_evidence(runs, [], IMG)[0].values()))
            for missing in (None, ''):                                      # no way to switch the image check off
                with self.assertRaises(ValueError):
                    freeze_readiness.smoke_evidence(runs, ['s1'], missing)

    def test_usage_collector_needs_a_real_reviewer_call_in_an_arm3_smoke(self):
        # spec 11.5: reviewer authentication and reviewer-role usage collection are verified before the freeze
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            self.smoke(runs, 's1', 1, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 's2', 2, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 'hook3', 3, {'pm': ['claude-fable-5-2']}, reviewer=dict(calls_this_turn=0, event_count=0))
            flags, _, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's2', 'hook3'], IMG)
            self.assertFalse(flags['usage_collector_verified'])                    # the hook smoke never calls the reviewer
            self.assertEqual(detail['hook3']['reviewer_smoke_turns'], [])
            cases = {'reviewer call failed (Not logged in)': dict(calls_this_turn=2, calls_succeeded_this_turn=0, event_count=0),
                     'call succeeded but no reviewer request collected': dict(calls_this_turn=1, calls_succeeded_this_turn=1, event_count=0)}
            for name, reviewer in cases.items():
                with self.subTest(name):
                    self.smoke(runs, 'rev3', 3, {'pm': ['claude-fable-5-2']}, reviewer=reviewer)
                    self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 's2', 'hook3', 'rev3'], IMG)[0]['usage_collector_verified'])
            self.smoke(runs, 'rev3', 3, {'pm': ['claude-fable-5-2'], 'reviewer': ['claude-fable-5-2']}, usage_complete=False)
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['s1', 's2', 'hook3', 'rev3'], IMG)[0]['usage_collector_verified'])
            self.smoke(runs, 'rev3', 3, {'pm': ['claude-fable-5-2'], 'reviewer': ['claude-fable-5-2']})
            flags, _, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's2', 'hook3', 'rev3'], IMG)
            self.assertTrue(flags['usage_collector_verified'], flags)
            self.assertEqual(detail['rev3']['reviewer_smoke_turns'], ['rev3/0001'])
            # a reviewer call in an arm 1/2 run proves nothing about arm 3
            self.smoke(runs, 'odd1', 1, {'implementer': ['claude-opus-5-5']}, reviewer=self.REVIEWER_OK)
            self.assertFalse(freeze_readiness.smoke_evidence(runs, ['odd1', 's2', 'hook3'], IMG)[0]['usage_collector_verified'])

    def inspect(self, runs, run_id, image, turn='0001'):
        write(runs / run_id / 'turns' / turn / 'container-inspect.json', [{'Image': image}])

    def test_smoke_evidence_counts_only_runs_on_the_runner_image_being_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            for run_id, arm, roles in (('s1', 1, {'implementer': ['claude-opus-5-5']}), ('s2', 2, {'implementer': ['claude-opus-5-5']}),
                                       ('s3', 3, {'pm': ['claude-fable-5-2'], 'reviewer': ['claude-fable-5-2']})):
                self.smoke(runs, run_id, arm, roles)
                self.inspect(runs, run_id, 'sha256:new')
            flags, _, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], 'sha256:new')
            self.assertTrue(all(flags.values()), flags)
            self.assertTrue(detail['s3']['image_matches_runner'])
            self.inspect(runs, 's2', 'sha256:old')                       # one smoke ran on the image before the rebuild
            flags, _, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], 'sha256:new')
            self.assertFalse(any(flags.values()), flags)
            self.assertEqual((detail['s2']['image_matches_runner'], detail['s2']['image_ids']), (False, ['sha256:old']))
            (runs / 's2/turns/0001/container-inspect.json').unlink()       # unknown image: never a match
            self.assertFalse(any(freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], 'sha256:new')[0].values()))

    def test_a_smoke_run_counts_as_a_whole_and_tools_are_checked_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            self.smoke(runs, 's1', 1, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 's2', 2, {'implementer': ['claude-opus-5-5']})
            self.smoke(runs, 's3', 3, {'pm': ['claude-fable-5-2'], 'reviewer': ['claude-fable-5-2']})
            self.assertTrue(freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], IMG)[0]['usage_collector_verified'])
            write(runs / 's1/turns/0002/result.json', dict(status='failed', usage_complete=False))   # a failed turn in the run
            flags, _, detail = freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], IMG)
            self.assertFalse(flags['usage_collector_verified'])
            self.assertFalse(detail['s1']['all_turns_ok_and_complete'])
            shutil.rmtree(runs / 's1/turns/0002')
            # two arm 1 smokes whose union is the allow-list but each lacks a tool: not isolation evidence
            for run_id, drop in (('a', 'WebSearch'), ('b', 'WebFetch')):
                self.smoke(runs, run_id, 1, {'implementer': ['claude-opus-5-5']})
                m = json.loads((runs / run_id / 'manifest.json').read_text())
                m['observed_tools'] = [t for t in m['observed_tools'] if t != drop]
                write(runs / run_id / 'manifest.json', m)
            flags, _, detail = freeze_readiness.smoke_evidence(runs, ['a', 'b', 's2', 's3'], IMG)
            self.assertFalse(flags['isolation_verified'])
            self.assertFalse(detail['a']['tools_equal_arm_allow_list'])
            self.assertTrue(freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'], IMG)[0]['isolation_verified'])

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

    CONTRACT, IMAGE = 'c' * 64, 'sha256:frozen'

    def pilot(self, runs, roles=('pm', 'worker', 'reviewer'), models=None, last='ok', pending=False, censored=False, arm=3,
              contract=CONTRACT, image=IMAGE):
        run = runs / 'pilot'
        models = models or {'pm': ['claude-fable-5-2'], 'worker': ['claude-opus-5-5'], 'reviewer': ['claude-fable-5-2']}
        write(run / 'manifest.json', dict(arm=arm, actual_role_models=models, contract_sha256=contract, image=image))
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
                 'reviewer model unobserved': ({'models': {'pm': ['claude-fable-5-2'], 'worker': ['claude-opus-5-5']}}, False),
                 'ran under another contract': ({'contract': 'd' * 64}, False),
                 'ran before any freeze': ({'contract': None}, False),
                 'ran on another image': ({'image': 'payment-claude-runner:2.1.326'}, False)}
        for name, (kw, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                passed, detail = freeze_readiness.pilot_evidence(Path(tmp), self.pilot(Path(tmp), **kw), self.CONTRACT, self.IMAGE)
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
            passed, detail = freeze_readiness.pilot_evidence(Path(tmp), 'pilot', self.CONTRACT, self.IMAGE)
            self.assertTrue(passed, detail)
            (t / 'events.jsonl').write_text(json.dumps(dict(type='message', id='ex', model='claude-sonnet-5', role='worker',
                                                           agent_type=None, turn='0003', usage=USAGE)) + '\n')
            self.assertFalse(freeze_readiness.pilot_evidence(Path(tmp), 'pilot', self.CONTRACT, self.IMAGE)[0])
            (t / 'events.jsonl').write_text('')
            consult = run / 'consults/0001'
            consult.mkdir(parents=True)
            write(consult / 'usage.json', {'usage_complete': True, 'cost_usd': 0.0})
            (consult / 'events.jsonl').write_text(json.dumps(dict(type='message', id='px', model='claude-fable-5-2',
                                                                 role='user-proxy', turn='p', usage=USAGE)) + '\n')
            passed, detail = freeze_readiness.pilot_evidence(Path(tmp), 'pilot', self.CONTRACT, self.IMAGE)
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


class FreezeReplaceTest(unittest.TestCase):
    FOUND = {'payment-claude-runner:' + freeze_readiness.launch.CLI_VERSION: 'sha256:frozen', 'postgres:16': 'sha256:pg'}

    def main(self, tmp, inventory, pilot=None):
        out = Path(tmp) / 'readiness.json'
        argv = ['freeze_readiness.py', '--output', str(out), '--runs', str(Path(tmp) / 'runs'), '--replace']
        if pilot:
            argv += ['--pilot-run', pilot]
        flags = {k: True for k in ('isolation_verified', 'hook_enforcement_verified', 'oauth_token_source_verified',
                                   'cache_observation_verified', 'usage_collector_verified', 'role_models_verified')}
        smoke = (flags, {'1': 'claude-opus-5-5', '2': 'claude-opus-5-5', '3': 'claude-fable-5-2'},
                 {'s%d' % a: {'arm': a} for a in (1, 2, 3)})
        err = io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(freeze_readiness, 'clean_baseline', return_value=('abc', [])), \
             patch.object(freeze_readiness, 'images', return_value=(dict(self.FOUND), [])), \
             patch.object(freeze_readiness, 'smoke_evidence', return_value=smoke), \
             patch.object(freeze_readiness, 'inventory', return_value=inventory), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                code = freeze_readiness.main()
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue(), out

    def test_replace_rederives_flags_but_refuses_changed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'readiness.json'
            code, err, _ = self.main(tmp, {'docs/acceptance.md': 'v1'})      # (output absent: first freeze)
            self.assertEqual(code, 0, err)
            first = json.loads(out.read_text())
            self.assertFalse(first['pilot_passed'])
            # a pilot run initialized under this freeze carries its contract hash and frozen image
            FreezeTest.pilot(None, Path(tmp) / 'runs', contract=first['contract_sha256'], image='sha256:frozen')
            code, err, _ = self.main(tmp, {'docs/acceptance.md': 'v1'}, pilot='pilot')
            self.assertEqual(code, 0, err)
            second = json.loads(out.read_text())
            self.assertTrue(second['pilot_passed'], second['evidence']['pilot'])
            self.assertEqual(second['contract_sha256'], first['contract_sha256'])
            self.assertEqual(len(list(Path(tmp).glob('readiness.superseded-*.json'))), 1)
            # the acceptance criteria changed after the freeze: --replace is refused, the freeze stays as it was
            code, err, _ = self.main(tmp, {'docs/acceptance.md': 'v2'}, pilot='pilot')
            self.assertNotEqual(code, 0)
            self.assertIn('docs/acceptance.md', err)
            self.assertIn('protocol version change', err)
            self.assertEqual(json.loads(out.read_text()), second)
            self.assertEqual(len(list(Path(tmp).glob('readiness.superseded-*.json'))), 1)

    def supersede(self, tmp, inventory, root, *extra):
        out = Path(tmp) / 'readiness.json'
        argv = ['freeze_readiness.py', '--output', str(out), '--runs', str(root / '.runs')] + list(extra)
        flags = {k: True for k in ('isolation_verified', 'hook_enforcement_verified', 'oauth_token_source_verified',
                                   'cache_observation_verified', 'usage_collector_verified', 'role_models_verified')}
        smoke = (flags, {'1': 'claude-opus-5-5', '2': 'claude-opus-5-5', '3': 'claude-fable-5-2'}, {'s%d' % a: {'arm': a} for a in (1, 2, 3)})
        err = io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(freeze_readiness, 'clean_baseline', return_value=('abc', [])), \
             patch.object(freeze_readiness, 'images', return_value=(dict(self.FOUND), [])), \
             patch.object(freeze_readiness, 'smoke_evidence', return_value=smoke), \
             patch.object(freeze_readiness, 'inventory', return_value=inventory), patch.object(freeze_readiness, 'ROOT', root), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                code = freeze_readiness.main()
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue(), out

    def experiment_root(self, tmp):
        root = Path(tmp) / 'claude'
        for arm in (1, 2, 3):
            for core in range(1, 11):
                write(root / 'docs/experiments' / str(arm) / ('core%d' % core) / 'run.json', {'status': 'NOT_STARTED'})
        write(root / 'docs/results/cycles.json', [{'cycle': c, 'status': 'NOT_STARTED'} for c in range(1, 11)])
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
                        '-m', 'setup'], check=True)
        return root

    def test_supersede_before_runs_replaces_changed_inputs_only_while_nothing_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.experiment_root(tmp)
            code, err, out = self.supersede(tmp, {'claude/runner/launch.py': 'v1'}, root)
            self.assertEqual(code, 0, err)
            first = json.loads(out.read_text())
            first_listing = (Path(tmp) / 'frozen-inputs.json').read_text()
            # inputs changed after the pilot: --replace refuses (unchanged rule), the supersede path needs a reason
            changed = {'claude/runner/launch.py': 'v2'}
            self.assertNotEqual(self.supersede(tmp, changed, root, '--replace')[0], 0)
            self.assertNotEqual(self.supersede(tmp, changed, root, '--supersede-before-runs')[0], 0)
            self.assertNotEqual(self.supersede(tmp, changed, root, '--supersede-before-runs', '--reason', 'x', '--pilot-run', 'p')[0], 0)
            self.assertNotEqual(self.supersede(tmp, changed, root, '--reason', 'x')[0], 0)
            self.assertEqual(json.loads(out.read_text()), first)                 # nothing changed by the refusals
            code, err, _ = self.supersede(tmp, changed, root, '--supersede-before-runs', '--reason', 'pilot 01: reviewer token')
            self.assertEqual(code, 0, err)
            second = json.loads(out.read_text())
            self.assertFalse(second['pilot_passed'])
            self.assertEqual(second['protocol'], first['protocol'])
            sup = second['supersedes']
            self.assertEqual(sup['reason'], 'pilot 01: reviewer token')
            self.assertEqual(sup['previous_contract_sha256'], first['contract_sha256'])
            self.assertEqual(sup['changed_inputs'], ['claude/runner/launch.py'])
            self.assertTrue(sup['pilot_passed_reset'])
            archived = {k: v['archived_as'] for k, v in sup['archived'].items()}
            self.assertEqual(sorted(archived), ['frozen-inputs.json', 'readiness.json'])
            self.assertEqual(json.loads((Path(tmp) / archived['readiness.json']).read_text()), first)   # kept, not deleted
            self.assertEqual((Path(tmp) / archived['frozen-inputs.json']).read_text(), first_listing)
            self.assertEqual(sup['archived']['readiness.json']['sha256'],
                             hashlib.sha256((Path(tmp) / archived['readiness.json']).read_bytes()).hexdigest())
            self.assertEqual(json.loads((Path(tmp) / 'frozen-inputs.json').read_text()), changed)

    def test_supersede_is_refused_once_any_measured_run_started(self):
        starts = {'run record': lambda r: write(r / 'docs/experiments/2/core1/run.json', {'status': 'SETUP_FAILED'}),
                  'run directory': lambda r: (r / '.runs/claude-arm3-core01').mkdir(parents=True),
                  'cycle opened': lambda r: write(r / 'docs/results/cycles.json',
                                                  [{'cycle': 1, 'status': 'RUNNING'}] + [{'cycle': c, 'status': 'NOT_STARTED'} for c in range(2, 11)]),
                  'run commit': lambda r: subprocess.run(['git', '-C', str(r), '-c', 'user.email=a@b', '-c', 'user.name=a', 'commit',
                                                          '-q', '--allow-empty', '-m', 'experiment: claude-arm1-core01 FAILED'], check=True),
                  'record unreadable': lambda r: (r / 'docs/experiments/1/core4/run.json').write_text('{')}
        for name, start in starts.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = self.experiment_root(tmp)
                self.assertEqual(freeze_readiness.experiment_started(root), [])
                self.assertEqual(self.supersede(tmp, {'a': '1'}, root)[0], 0)
                start(root)
                self.assertTrue(freeze_readiness.experiment_started(root))
                code, err, out = self.supersede(tmp, {'a': '2'}, root, '--supersede-before-runs', '--reason', 'late change')
                self.assertNotEqual(code, 0)
                self.assertIn('measured run has started', err)
                self.assertNotIn('supersedes', json.loads(out.read_text()))
                self.assertEqual(list(Path(tmp).glob('*.superseded-*.json')), [])

    def test_supersede_is_refused_for_shared_inputs_and_looks_at_the_fixed_run_folder_and_all_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.experiment_root(tmp)
            self.assertEqual(self.supersede(tmp, {'docs/acceptance.md': 'v1', 'claude/runner/launch.py': 'v1'}, root)[0], 0)
            first = json.loads((Path(tmp) / 'readiness.json').read_text())
            for name in ('docs/acceptance.md', 'evaluation/acceptance.py'):           # the contract / evaluator changed
                code, err, out = self.supersede(tmp, {'docs/acceptance.md': 'v1', 'claude/runner/launch.py': 'v1', name: 'v2'},
                                                root, '--supersede-before-runs', '--reason', 'x')
                self.assertNotEqual(code, 0)
                self.assertIn('protocol version change', err)
                self.assertEqual(json.loads(out.read_text()), first)
            # a measured run directory in the fixed folder is seen even when --runs points elsewhere
            (root / '.runs/claude-arm2-core01').mkdir(parents=True)
            out = Path(tmp) / 'readiness.json'
            argv = ['freeze_readiness.py', '--output', str(out), '--runs', str(Path(tmp) / 'empty'), '--supersede-before-runs',
                    '--reason', 'x']
            with patch.object(sys, 'argv', argv), patch.object(freeze_readiness, 'ROOT', root), \
                 contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                freeze_readiness.main()
            self.assertIn('claude-arm2-core01: run directory exists', err.getvalue())
            shutil.rmtree(root / '.runs')
            # a run commit on another local branch counts
            git = ['git', '-C', str(root), '-c', 'user.email=a@b', '-c', 'user.name=a']
            subprocess.run(git + ['checkout', '-q', '-b', 'other'], check=True)
            subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', 'experiment: claude-arm1-core01 FAILED'], check=True)
            subprocess.run(git + ['checkout', '-q', '-'], check=True)
            self.assertIn('commit: experiment: claude-arm1-core01 FAILED', freeze_readiness.experiment_started(root))

    def test_freeze_files_are_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.experiment_root(tmp)
            self.assertEqual(self.supersede(tmp, {'claude/runner/launch.py': 'v1'}, root)[0], 0)
            out = Path(tmp) / 'readiness.json'
            before = (out.read_text(), (Path(tmp) / 'frozen-inputs.json').read_text())
            real_replace = Path.replace
            def failing(self, target):
                raise OSError('disk full')
            with patch.object(Path, 'replace', failing), self.assertRaises(OSError):
                self.supersede(tmp, {'claude/runner/launch.py': 'v2'}, root, '--supersede-before-runs', '--reason', 'x')
            self.assertEqual((out.read_text(), (Path(tmp) / 'frozen-inputs.json').read_text()), before)   # old freeze back in place
            self.assertEqual(list(Path(tmp).glob('*superseded*')) + list(Path(tmp).glob('.*.tmp-*')), [])
            self.assertIs(Path.replace, real_replace)

    def test_write_freeze_rolls_back_both_files_at_every_step(self):
        real_rename, real_replace = Path.rename, Path.replace
        for fail_at in range(1, 5):                     # archive readiness, archive listing, new readiness, new listing
            for first_freeze in (False, True):
                with self.subTest(fail_at=fail_at, first_freeze=first_freeze), tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / 'readiness.json'
                    listing = Path(tmp) / 'frozen-inputs.json'
                    moves = []
                    if not first_freeze:
                        out.write_text('old readiness')
                        listing.write_text('old listing')
                        moves = [(out, Path(tmp) / 'readiness.superseded-x.json'), (listing, Path(tmp) / 'frozen-inputs.superseded-x.json')]
                    steps = []
                    def counted(real):
                        def step(path, target):
                            steps.append(path)
                            if len(steps) == fail_at:
                                raise OSError('injected failure at step %d' % fail_at)
                            return real(path, target)
                        return step
                    with patch.object(Path, 'rename', counted(real_rename)), patch.object(Path, 'replace', counted(real_replace)):
                        try:
                            freeze_readiness.write_freeze(out, {'new': 1}, {'new': 2}, moves)
                            failed = False
                        except OSError:
                            failed = True
                    names = sorted(p.name for p in Path(tmp).iterdir())
                    if failed:
                        expected = [] if first_freeze else ['frozen-inputs.json', 'readiness.json']
                        self.assertEqual(names, expected)            # no archive, temp or backup left, no mix
                        if not first_freeze:
                            self.assertEqual((out.read_text(), listing.read_text()), ('old readiness', 'old listing'))
                    else:                                            # fewer steps than fail_at: the write completed
                        self.assertEqual(json.loads(out.read_text()), {'new': 1})
                        self.assertEqual(json.loads(listing.read_text()), {'new': 2})
            self.assertIs(Path.rename, real_rename)

    def test_replace_blockers(self):
        found = dict(self.FOUND)
        previous = {'contract_sha256': 'a', 'image_digests': dict(found, runner='sha256:frozen')}
        self.assertEqual(freeze_readiness.replace_blockers(previous, {}, 'a', found), [])
        self.assertTrue(freeze_readiness.replace_blockers(previous, {}, 'b', found))
        self.assertEqual(freeze_readiness.replace_blockers(previous, {}, 'a', dict(found, **{'postgres:16': 'sha256:new'})),
                         ['image ids differ from the readiness being replaced'])


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
            write(records / 'run.json', {'run_id': 'claude-arm3-core01', 'status': 'RUNNING'})
            run = root / '.runs/claude-arm3-core01'
            session = '11111111-2222-4333-8444-555555555555'
            write(run / 'manifest.json', dict(run_id='claude-arm3-core01', arm=3, core=1, session_id=session, requested_model='claude-fable-5-2',
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
                marker = json.loads((records / 'turns/0003' / capture_turn.CAPTURED).read_text())
                self.assertEqual(marker['files'], {p.name: finish_run.sha(p) for p in (records / 'turns/0003').iterdir()
                                                   if p.name != capture_turn.CAPTURED})
                self.assertEqual(sorted(marker['files']), ['analysis.md', 'invocation.json', 'prompt.txt', 'response.txt',
                                                           'result.json', 'stderr.txt', 'usage.jsonl'])
                # a capture that stops part-way (before the journal entry) leaves no marker; capturing again replaces it
                turn(4, [dict(type='message', id='r-pm4', model='claude-fable-5-2', usage=USAGE, role='pm')], True)
                analysis.write_text('analysis 4')
                journal = (records / 'events.jsonl').read_bytes()
                with patch.object(capture_turn, 'main_text', side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):
                        capture_turn.capture('claude-arm3-core01', 4, analysis, 'ITERATING')
                self.assertTrue((records / 'turns/0004/result.json').is_file())
                self.assertFalse((records / 'turns/0004' / capture_turn.CAPTURED).exists())
                self.assertEqual((records / 'events.jsonl').read_bytes(), journal)
                with contextlib.redirect_stderr(io.StringIO()):
                    capture_turn.capture('claude-arm3-core01', 4, analysis, 'ITERATING')
                rows4 = [json.loads(l)['event_id'] for l in (records / 'turns/0004/usage.jsonl').read_text().splitlines()]
                self.assertEqual(rows4, ['r-pm4'])
                self.assertTrue((records / 'turns/0004' / capture_turn.CAPTURED).is_file())
                # stopped after the journal entry: not replaced (the journal is append-only), inspected instead
                (records / 'turns/0004' / capture_turn.CAPTURED).unlink()
                journal = (records / 'events.jsonl').read_bytes()
                with self.assertRaises(ValueError) as ctx:
                    capture_turn.capture('claude-arm3-core01', 4, analysis, 'ITERATING')
                self.assertIn('0004', str(ctx.exception))
                self.assertEqual((records / 'events.jsonl').read_bytes(), journal)
                self.assertTrue((records / 'turns/0004/usage.jsonl').is_file())


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
        write(runs / 'claude-arm3-core01' / 'manifest.json', {'arm': arm, 'run_id': 'claude-arm3-core01', 'image': 'sha256:run-image'})
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


    def test_proxy_timeout_is_reduced_to_the_remaining_run_time(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = self.setup_run(tmp)
            t = run_dir / 'turns/0001'
            write(t / 'invocation.json', {'turn_kind': 'initial',
                                          'started_at': (datetime.now(timezone.utc) - timedelta(seconds=28800 - 100)).isoformat()})
            write(t / 'result.json', {'status': 'ok', 'usage_complete': True, 'cost': {'pinned_session_usd': 0.0, 'reviewer_applicable': False}})
            (t / 'events.jsonl').write_text('')
            timeouts = []
            inner = self.fake()
            def fake(cmd, **kw):
                if cmd[:2] == ['docker', 'start']:
                    timeouts.append(kw.get('timeout'))
                return inner(cmd, **kw)
            code, _, out = self.call(argv + ['--model', 'claude-opus-5-5'], fake)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(timeouts), 1)
            self.assertLessEqual(timeouts[0], 100)
            invocation = json.loads((run_dir / 'consults/0001/invocation.json').read_text())
            self.assertEqual(invocation['timeout_seconds'], 1800)
            self.assertLessEqual(invocation['effective_timeout_seconds'], 100)

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


class EvaluationCostLedgerTest(unittest.TestCase):
    """Spec 10.4: experiment-level append-only ledger, one line per conductor session or adapter authoring."""

    def cli(self, ledger, *extra):
        argv = ['record_evaluation_cost.py', '--ledger', str(ledger)] + list(extra)
        err = io.StringIO()
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                return record_evaluation_cost.main(), err.getvalue()
            except SystemExit as exc:
                return exc.code, err.getvalue()

    def lines(self, ledger):
        return [json.loads(l) for l in ledger.read_text().splitlines()]

    def test_default_ledger_is_experiment_wide(self):
        self.assertEqual(record_evaluation_cost.default_ledger(),
                         record_evaluation_cost.ROOT / 'docs/results/evaluation-cost.jsonl')

    def test_lines_are_appended_with_role_session_model_tokens_source_runs_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'evaluation-cost.jsonl'
            code, err = self.cli(ledger, '--role', 'conductor', '--session', 'agent-1', '--model', 'claude-opus-5-5',
                                 '--input-tokens', '100', '--cache-creation-input-tokens', '0', '--cache-read-input-tokens',
                                 '900', '--output-tokens', '50', '--source', 'agent completion notice usage',
                                 '--run-id', 'claude-arm1-core01', '--run-id', 'claude-arm2-core01', '--includes-judging-adapter')
            self.assertEqual(code, 0, err)
            before = ledger.read_bytes()
            code, err = self.cli(ledger, '--role', 'judging-adapter', '--session', 'agent-2', '--item', 'pilot-01 adapter',
                                 '--model', 'claude-opus-5-5', '--total-tokens', '40', '--source', 'completion notice total only',
                                 '--run-id', 'claude-pilot-01', '--cost-usd', '0.2')
            self.assertEqual(code, 0, err)
            self.assertTrue(ledger.read_bytes().startswith(before))          # appended, earlier line untouched
            first, second = self.lines(ledger)
            self.assertEqual((first['role'], first['session'], first['model'], first['total_tokens'], first['run_ids'],
                              first['includes_judging_adapter'], first['usage_split_observed']),
                             ('conductor', 'agent-1', 'claude-opus-5-5', 1050, ['claude-arm1-core01', 'claude-arm2-core01'],
                              True, True))
            self.assertEqual((second['usage'], second['total_tokens'], second['cost_usd']), (None, 40, 0.2))
            for line in (first, second):
                self.assertTrue(line['recorded_at'] and line['tokens_source'])
                self.assertFalse(line['counted_in_run_cap'] or line['counted_in_arm_cost'])

    def test_bad_input_and_a_second_line_for_the_same_session_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'evaluation-cost.jsonl'
            ok = ['--role', 'conductor', '--session', 's', '--model', 'm', '--total-tokens', '1', '--source', 'x', '--run-id', 'r1']
            self.assertEqual(self.cli(ledger, *ok)[0], 0)
            for bad in (ok,                                                      # same role + session again
                        ['--role', 'conductor', '--session', 't', '--model', 'm', '--total-tokens', '1', '--source', 'x'],
                        ['--role', 'conductor', '--session', 't', '--model', 'm', '--input-tokens', '1', '--source', 'x', '--run-id', 'r'],
                        ['--role', 'pm', '--session', 't', '--model', 'm', '--total-tokens', '1', '--source', 'x', '--run-id', 'r'],
                        ['--role', 'conductor', '--session', 't', '--model', 'm', '--total-tokens', '-1', '--source', 'x', '--run-id', 'r'],
                        ['--role', 'conductor', '--session', ' ', '--model', 'm', '--total-tokens', '1', '--source', 'x', '--run-id', 'r'],
                        ['--role', 'conductor', '--session', 't', '--model', 'm', '--total-tokens', '1', '--source', 'x', '--run-id', 'a/b'],
                        ['--role', 'conductor', '--session', 't', '--model', 'm', '--total-tokens', '1', '--source', 'x', '--run-id', 'r',
                         '--run-id', 'r'],
                        ['--role', 'judging-adapter', '--session', 't', '--item', 'i', '--model', 'm', '--total-tokens', '1',
                         '--source', 'x', '--run-id', 'r', '--includes-judging-adapter'],
                        ['--role', 'conductor', '--session', 't', '--model', 'm', '--total-tokens', '1', '--source', 'x', '--run-id', 'r',
                         '--cost-usd', 'nan'],
                        ['--role', 'conductor', '--session', 't', '--model', 'm', '--total-tokens', '1', '--source', 'x', '--run-id', 'r',
                         '--cost-usd', '-0.5']):
                self.assertNotEqual(self.cli(ledger, *bad)[0], 0, bad)
            self.assertEqual(len(self.lines(ledger)), 1)
            with ledger.open('a') as stream:                                     # a damaged ledger is inspected, not appended to
                stream.write('not json\n')
            ok[3] = 'u'
            self.assertNotEqual(self.cli(ledger, *ok)[0], 0)

    def test_one_session_records_one_adapter_line_per_job_and_never_the_same_job_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'evaluation-cost.jsonl'
            adapter = ['--role', 'judging-adapter', '--session', 'agent-7', '--model', 'claude-fable-5-2',
                       '--total-tokens', '10', '--source', 'completion notice', '--run-id', 'claude-arm1-core01']
            self.assertEqual(self.cli(ledger, *adapter, '--item', 'arm1-core01 adapter')[0], 0)
            self.assertEqual(self.cli(ledger, *adapter, '--item', 'arm2-core01 adapter')[0], 0)
            before = ledger.read_bytes()
            code, err = self.cli(ledger, *adapter, '--item', 'arm1-core01 adapter')   # exact duplicate
            self.assertNotEqual(code, 0)
            self.assertIn('already recorded', err)
            for bad in (['--item', ' '], ['--item', ''], []):                     # an adapter line needs its job label
                code, err = self.cli(ledger, *adapter, *bad)
                self.assertNotEqual(code, 0, bad)
                self.assertIn('--item', err)
            with self.assertRaises(ValueError):                                  # the same rule without the CLI
                record_evaluation_cost.entry('judging-adapter', 'agent-8', 'm', None, 1, 'x', ['claude-arm1-core01'])
            # one conductor session over two pilots: one line naming both runs, marked unallocated (never split)
            both = record_evaluation_cost.entry('conductor', 'agent-9', 'claude-fable-5-2', None, 418000, 'completion notice',
                                                ['claude-pilot-02', 'claude-pilot-03'])
            self.assertEqual((both['run_ids'], both['per_run_split']), (['claude-pilot-02', 'claude-pilot-03'], 'unallocated'))
            self.assertEqual(record_evaluation_cost.entry('conductor', 'agent-9', 'm', None, 1, 'x', ['claude-pilot-03'])['per_run_split'],
                             'single run')
            conductor = ['--role', 'conductor', '--session', 'agent-7', '--model', 'claude-fable-5-2', '--total-tokens', '5',
                         '--source', 'completion notice', '--run-id', 'claude-arm1-core01']
            self.assertNotEqual(self.cli(ledger, *conductor, '--item', 'x')[0], 0)  # conductor: one per session, no item
            self.assertEqual(ledger.read_bytes(), before)
            self.assertEqual(self.cli(ledger, *conductor)[0], 0)
            self.assertNotEqual(self.cli(ledger, *conductor)[0], 0)
            self.assertEqual([(l['role'], l['item']) for l in self.lines(ledger)],
                             [('judging-adapter', 'arm1-core01 adapter'), ('judging-adapter', 'arm2-core01 adapter'),
                              ('conductor', None)])


def capture_copy(run, folder, name, complete=True):
    """The record folder's copy of a turn's result.json and, for a capture that completed, its marker."""
    (folder / 'turns' / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(run / 'turns' / name / 'result.json', folder / 'turns' / name / 'result.json')
    if complete:
        mark_captured(folder / 'turns' / name)


def mark_captured(copy):
    """The marker capture_turn.py writes last: every file of the turn folder with its sha256."""
    write(copy / capture_turn.CAPTURED, {'turn': copy.name, 'files': {p.name: finish_run.sha(p) for p in sorted(copy.iterdir())
                                                                       if p.is_file() and p.name != capture_turn.CAPTURED}})


def finish_fixture(tmp, arm=1, run_id='claude-arm1-core01', pilot=False, results=None, tools=None, models=None,
                   usage_complete=True, selftest=True, readiness=True, record_status='RUNNING', censored=None, turns=1,
                   proxy_model=None, captured=True):
    """A run directory, its frozen readiness and its record folder with verdict.json and evidence files."""
    launch = finish_run.launch
    root = Path(tmp) / 'claude'
    run = root / '.runs' / run_id
    base = list(launch.ALLOWED_TOOLS_ARM3 if arm == 3 else launch.ALLOWED_TOOLS_ARM12)
    ready_path = root / 'docs/results/readiness.json'
    write(ready_path, {'evidence': {'smoke_runs': {'s%d' % a: {'arm': a, 'observed_tools': sorted(
        launch.ALLOWED_TOOLS_ARM3 if a == 3 else launch.ALLOWED_TOOLS_ARM12)} for a in (1, 2, 3)}}})
    observed = models or ({'implementer': ['claude-opus-5-5']} if arm != 3 else
                          {'pm': ['claude-fable-5-2'], 'worker': ['claude-opus-5-5'], 'reviewer': ['claude-fable-5-2']})
    write(run / 'manifest.json', {'run_id': run_id, 'arm': arm, 'core': 1, 'pilot': pilot, 'harness': '/h' if arm == 3 else None,
                                  'selftest': {'passed': selftest}, 'observed_tools': tools if tools is not None else base,
                                  'actual_role_models': observed, 'readiness': str(ready_path) if readiness else None})
    events = []
    for role, models_ in observed.items():
        events += [dict(type='message', id='r-%s-%s' % (role, m), model=m, role=role, turn='0001', agent_type=None,
                        usage=dict(USAGE)) for m in models_]
    for n in range(1, turns + 1):
        t = run / 'turns' / ('%04d' % n)
        write(t / 'invocation.json', {'turn_kind': 'initial' if n == 1 else 'feedback', 'started_at': launch.utc(),
                                      'hook_selftest_before_call': {'passed': True} if arm == 3 else None})
        write(t / 'result.json', {'status': 'ok', 'usage_complete': usage_complete,
                                  'cost': {'pinned_session_usd': 0.1, 'reviewer_applicable': False}})
        (t / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in (events if n == 1 else [])))
    if proxy_model:
        c = run / 'consults/0001'
        write(c / 'usage.json', {'usage_complete': True, 'cost_usd': 0.0, 'exit_code': 0})
        (c / 'events.jsonl').write_text(json.dumps(dict(type='message', id='px', model=proxy_model, role='user-proxy',
                                                        turn='p', usage=dict(USAGE))) + '\n')
    if censored:
        write(run / 'censored.json', censored)
    folder = root / ('docs/pilots/%s' % run_id if pilot else 'docs/experiments/%d/core1' % arm)
    if not pilot:
        write(folder / 'run.json', {'run_id': run_id, 'status': record_status, 'isolation_verified': True})
    for t in sorted((run / 'turns').glob('*')) if captured else []:             # what capture_turn.py leaves
        capture_copy(run, folder, t.name)
    results = results or {}
    criteria = {c: {'result': results.get(c, 'PASS'), 'evidence': ['acceptance-http.json'], 'note': 'observed'}
                for c in finish_run.CRITERIA}
    write(folder / 'verdict.json', {'run_id': run_id, 'judged_by': 'main session', 'judged_at': launch.utc(),
                                    'criteria': criteria})
    write(folder / 'acceptance-http.json', {'cases': {}})
    return root, run, folder


class FinishRunTest(unittest.TestCase):
    """Spec 10.2: terminal status from the verdict table; the four evidence fields from fixed sources only."""

    def finish(self, root, run_id='claude-arm1-core01', **kw):
        return finish_run.finish(run_id, root / '.runs', root, **kw)

    def record(self, folder):
        return json.loads((folder / 'run.json').read_text())

    def test_status_comes_from_the_verdict_table_never_from_an_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, folder = finish_fixture(tmp)
            _, status, _, fields = self.finish(root)
            self.assertEqual(status, 'PASSED')
            self.assertEqual(fields, {'all_acceptance_passed': True, 'usage_complete': True,
                                      'all_role_models_verified': True, 'isolation_verified': True})
            record = self.record(folder)
            self.assertEqual(record['verdict']['judged_by'], 'main session')
            self.assertEqual(record['verdict']['evidence']['acceptance-http.json'],
                             hashlib.sha256((folder / 'acceptance-http.json').read_bytes()).hexdigest())
            self.assertEqual(record['verdict']['sha256'], hashlib.sha256((folder / 'verdict.json').read_bytes()).hexdigest())
        for other in ('FAIL', 'PARTIAL', 'NOT_RUN'):                             # anything but PASS is not a pass
            with tempfile.TemporaryDirectory() as tmp:
                root, _, folder = finish_fixture(tmp, results={'C07': other})
                _, status, _, fields = self.finish(root)
                self.assertEqual((status, fields['all_acceptance_passed']), ('FAILED', False), other)
        with patch.object(sys, 'argv', ['finish_run.py', '--run-id', 'x', '--verdict', 'PASSED']), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            finish_run.main()

    def test_each_evidence_field_is_false_when_its_source_is_absent_or_false(self):
        cases = {'all_acceptance_passed': dict(),
                 'usage_complete': dict(usage_complete=False),
                 'all_role_models_verified': dict(models={'implementer': ['claude-opus-5']}),
                 'isolation_verified': dict(tools=list(finish_run.launch.ALLOWED_TOOLS_ARM12) + ['Task'])}
        for field, kw in cases.items():
            with self.subTest(field), tempfile.TemporaryDirectory() as tmp:
                root, _, folder = finish_fixture(tmp, **kw)
                if field == 'all_acceptance_passed':                             # one listed evidence file does not exist
                    verdict = json.loads((folder / 'verdict.json').read_text())
                    verdict['criteria']['C05']['evidence'].append('never-written.json')
                    write(folder / 'verdict.json', verdict)
                _, status, _, fields = self.finish(root)
                self.assertEqual(status, 'PASSED')                               # passed; commit_run then blocks
                self.assertIs(fields[field], False)
                self.assertEqual([k for k, v in fields.items() if not v], [field])
                record = self.record(folder)
                self.assertIs(record[field], False)                              # a hand-written true is overwritten
                if field == 'all_acceptance_passed':
                    self.assertEqual(record['verdict']['evidence_missing'], ['never-written.json'])
        for kw in (dict(selftest=False), dict(readiness=False)):                 # isolation: self-test, frozen surface
            with tempfile.TemporaryDirectory() as tmp:
                root, _, _ = finish_fixture(tmp, **kw)
                self.assertIs(self.finish(root)[3]['isolation_verified'], False, kw)
        with tempfile.TemporaryDirectory() as tmp:                              # a hook self-test failed before a turn
            root, run, folder = finish_fixture(tmp, arm=3, run_id='claude-arm3-core01', proxy_model='claude-opus-5-5')
            finish_run.launch.record_precall_selftest_failure(run, {'passed': False}, 'feedback', 'feedback')
            fields = self.finish(root, 'claude-arm3-core01')[3]
            self.assertIs(fields['isolation_verified'], False)
            checks = self.record(folder)['evidence_derivation']['isolation_verified']['checks']
            self.assertEqual((checks['call_selftests_passed'], checks['no_selftest_failed_before_a_turn']), (True, False))
        with tempfile.TemporaryDirectory() as tmp:                              # an unreadable failure record counts too
            root, run, _ = finish_fixture(tmp, arm=3, run_id='claude-arm3-core01', proxy_model='claude-opus-5-5')
            (run / finish_run.launch.PRECALL_SELFTEST_FAILURES).mkdir()
            (run / finish_run.launch.PRECALL_SELFTEST_FAILURES / 'x.json').write_text('{')
            self.assertIs(self.finish(root, 'claude-arm3-core01')[3]['isolation_verified'], False)
        with tempfile.TemporaryDirectory() as tmp:                              # arm 3 role that was never observed
            root, _, _ = finish_fixture(tmp, arm=3, run_id='claude-arm3-core01',
                                        models={'pm': ['claude-fable-5-2'], 'worker': ['claude-opus-5-5']})
            self.assertIs(self.finish(root, 'claude-arm3-core01')[3]['all_role_models_verified'], False)
        with tempfile.TemporaryDirectory() as tmp:                              # proxy on another model
            root, _, _ = finish_fixture(tmp, arm=3, run_id='claude-arm3-core01', proxy_model='claude-sonnet-4')
            self.assertIs(self.finish(root, 'claude-arm3-core01')[3]['all_role_models_verified'], False)

    def test_result_summary_reports_correctness_load_figures_tokens_and_time_without_changing_the_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, folder = Path(tmp) / 'run', Path(tmp) / 'rec'
            t = run / 'turns/0001'
            write(t / 'invocation.json', {'turn_kind': 'initial', 'started_at': '2026-11-18T00:00:00+00:00'})
            write(t / 'result.json', {'status': 'ok', 'usage_complete': True, 'finished_at': '2026-11-18T01:00:00+00:00',
                                      'cost': {'pinned_session_usd': 1.0, 'reviewer_applicable': False}})
            (t / 'events.jsonl').write_text(json.dumps({'type': 'message', 'id': 'r1', 'model': 'm', 'role': 'implementer', 'turn': '0001',
                'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 5, 'cache_read_input_tokens': 80, 'output_tokens': 5}}) + '\n')
            load = folder / 'judging/v3/load'
            write(load / 'summary.json', {'phases': [{'phase': 'normal', 'passed': False, 'k6_exit_code': 99,
                                                      'pending_samples': [{'count': 3}, {'count': 0}], 'pending_converged': True},
                                                     {'phase': 'soak', 'passed': True}],
                                          'c20_passed': False, 'phase_gates_passed': False, 'sustainable_rps': 25})
            write(load / 'normal/k6-summary.json', {'metrics': {'payment_latency': {'p(95)': 1165.0, 'p(99)': 1886.7},
                                                                'dropped_iterations': {'count': 9}, 'committed_response': {'value': 0.98},
                                                                'iterations': {'count': 14991}}})
            write(load / 'soak/k6-summary.json', {'metrics': {'http_req_duration': {'p(95)': 97.0, 'p(99)': 182.6}}})
            results = {('C%02d' % n): 'PASS' for n in range(1, 21)}
            results.update(C07='FAIL', C13='NOT_RUN', C20='FAIL')
            write(folder / 'verdict.json', {'criteria': {'C20': {'evidence': ['judging/v3/load/summary.json']}}})
            verdict = {'results': results, 'passed': False}
            summary = finish_run.result_summary(run, folder, verdict, None)
            self.assertEqual(summary['correctness'], {'criteria': 'C01..C19', 'passed': 17, 'of': 19, 'failed': {'C07': 'FAIL'},
                                                      'not_run': ['C13']})
            load_ = summary['c20']['load']
            self.assertEqual((summary['c20']['result'], load_['sustainable_rps'], load_['c20_passed']), ('FAIL', 25, False))
            normal, soak = load_['phases']
            self.assertEqual((normal['p95_ms'], normal['p99_ms'], normal['dropped_iterations'], normal['committed_response_ratio'],
                              normal['pending_last'], normal['requests']), (1165.0, 1886.7, 9, 0.98, 0, 14991))
            self.assertEqual((soak['latency_metric'], soak['dropped_iterations'], soak['committed_response_ratio']),
                             ('http_req_duration', None, None))                  # absent figures stay None, never 0
            self.assertEqual(summary['tokens']['total_tokens'], 100)
            self.assertEqual((summary['tokens']['cache_read_input_tokens'], summary['tokens']['tokens_without_cache_reads']), (80, 20))
            self.assertEqual(summary['tokens']['by_role']['implementer']['tokens_without_cache_reads'], 20)
            self.assertEqual(summary['elapsed']['seconds'], 3600.0)
            # the C20 evidence cites no load summary, or it has another shape: the reason is kept, nothing invented
            write(folder / 'verdict.json', {'criteria': {'C20': {'evidence': ['judging/v3/load-check.stdout']}}})
            self.assertEqual(finish_run.result_summary(run, folder, verdict, None)['c20']['load']['available'], False)
            write(folder / 'verdict.json', {'criteria': {'C20': {'evidence': ['judging/v3/load/summary.json']}}})
            write(load / 'summary.json', {'phases': 'x'})
            self.assertIn('another shape', finish_run.result_summary(run, folder, verdict, None)['c20']['load']['reason'])

    def test_arm3_finish_requires_the_user_proxy_but_the_pilot_check_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:                              # no proxy request observed
            root, run, _ = finish_fixture(tmp, arm=3, run_id='claude-arm3-core01')
            _, _, _, fields = self.finish(root, 'claude-arm3-core01')
            self.assertIs(fields['all_role_models_verified'], False)
            checks = self.record(root / 'docs/experiments/3/core1')['evidence_derivation']['all_role_models_verified']['checks']
            self.assertEqual(checks, {'pm': True, 'worker': True, 'reviewer': True, 'user-proxy': False})
            manifest = json.loads((run / 'manifest.json').read_text())
            fr = finish_run.freeze_readiness                                      # the freeze's pilot role set is unchanged
            self.assertEqual(fr.PILOT_ROLES, ('pm', 'worker', 'reviewer'))
            self.assertTrue(all(fr.run_role_check(run, manifest, fr.PILOT_ROLES).values()))
        with tempfile.TemporaryDirectory() as tmp:                              # control: proxy on the agreed model
            root, _, _ = finish_fixture(tmp, arm=3, run_id='claude-arm3-core01', proxy_model='claude-opus-5-5')
            self.assertIs(self.finish(root, 'claude-arm3-core01')[3]['all_role_models_verified'], True)

    def test_a_table_with_an_unsupported_criterion_cannot_be_finished(self):
        def no_evidence(c, result):
            return c, lambda v: v['criteria'][c].update(result=result, evidence=[])
        cases = {'PASS without evidence': no_evidence('C03', 'PASS'),
                 'FAIL without evidence': no_evidence('C04', 'FAIL'),
                 'PARTIAL whose only evidence is absent': ('C05', lambda v: v['criteria']['C05'].update(
                     result='PARTIAL', evidence=['never-written.json'])),
                 'NOT_RUN without a note': ('C20', lambda v: v['criteria'].update(
                     C20={'result': 'NOT_RUN', 'evidence': ['acceptance-http.json']})),
                 'NOT_RUN with a blank note': ('C20', lambda v: v['criteria']['C20'].update(result='NOT_RUN', note='  '))}
        for name, (criterion, edit) in cases.items():
            for infra in (False, True):                                        # whatever status would result
                with self.subTest(name, infra=infra), tempfile.TemporaryDirectory() as tmp:
                    root, _, folder = finish_fixture(tmp)
                    (folder / 'docker.log').write_text('x')
                    verdict = json.loads((folder / 'verdict.json').read_text())
                    edit(verdict)
                    write(folder / 'verdict.json', verdict)
                    kw = dict(infra_reason='docker died', infra_evidence=['docker.log']) if infra else {}
                    with self.assertRaises(ValueError) as ctx:
                        self.finish(root, **kw)
                    self.assertIn(criterion, str(ctx.exception))                # the message names the criterion
                    self.assertEqual(self.record(folder)['status'], 'RUNNING')
        with tempfile.TemporaryDirectory() as tmp:                              # NOT_RUN with a note needs no file
            root, _, folder = finish_fixture(tmp)
            verdict = json.loads((folder / 'verdict.json').read_text())
            verdict['criteria']['C20'] = {'result': 'NOT_RUN', 'evidence': [], 'note': 'load check could not run'}
            write(folder / 'verdict.json', verdict)
            self.assertEqual(self.finish(root)[1], 'FAILED')

    def test_evidence_fields_use_the_freeze_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _ = finish_fixture(tmp, arm=3, run_id='claude-arm3-core01', proxy_model='claude-opus-5-5')
            fr = finish_run.freeze_readiness
            with patch.object(fr, 'run_role_check', wraps=fr.run_role_check) as roles, \
                 patch.object(fr, 'run_isolation_check', wraps=fr.run_isolation_check) as isolation:
                fields = self.finish(root, 'claude-arm3-core01')[3]
            self.assertTrue(all(fields.values()), fields)
            roles.assert_called_once()
            isolation.assert_called_once()

    def test_malformed_or_missing_verdict_is_refused_and_nothing_is_written(self):
        edits = {'other run': lambda v: v.update(run_id='claude-arm2-core01'),
                 'no judge': lambda v: v.update(judged_by=' '),
                 'missing C20': lambda v: v['criteria'].pop('C20'),
                 'unknown result': lambda v: v['criteria']['C01'].update(result='OK'),
                 'escaping path': lambda v: v['criteria']['C01'].update(evidence=['../../../x.json']),
                 'absolute path': lambda v: v['criteria']['C01'].update(evidence=['/etc/hostname']),
                 'example file': lambda v: v.update(_example='EXAMPLE ONLY')}
        for name, edit in edits.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root, _, folder = finish_fixture(tmp)
                verdict = json.loads((folder / 'verdict.json').read_text())
                edit(verdict)
                write(folder / 'verdict.json', verdict)
                with self.assertRaises(ValueError):
                    self.finish(root)
                self.assertEqual(self.record(folder)['status'], 'RUNNING')
        with tempfile.TemporaryDirectory() as tmp:
            root, _, folder = finish_fixture(tmp)
            (folder / 'verdict.json').unlink()
            with self.assertRaises(ValueError):
                self.finish(root)
        example = json.loads((finish_run.ROOT / 'docs/results/verdict.example.json').read_text())
        self.assertEqual(sorted(example['criteria']), list(finish_run.CRITERIA))   # the shipped example has the shape
        self.assertTrue(all(c['result'] in finish_run.RESULTS for c in example['criteria'].values()))

    def test_finish_once_and_supersede_keeps_the_previous_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, folder = finish_fixture(tmp)
            self.finish(root)
            first = (folder / 'run.json').read_bytes()
            with self.assertRaises(ValueError):
                self.finish(root)
            with self.assertRaises(ValueError):
                self.finish(root, supersede_reason=' ')
            verdict = json.loads((folder / 'verdict.json').read_text())
            verdict['criteria']['C02']['result'] = 'FAIL'
            write(folder / 'verdict.json', verdict)
            _, status, _, _ = self.finish(root, supersede_reason='C02 re-read: idempotency violated')
            self.assertEqual(status, 'FAILED')
            kept = list(folder.glob('run.superseded-*.json'))
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].read_bytes(), first)
            self.assertEqual(self.record(folder)['supersedes']['reason'], 'C02 re-read: idempotency violated')
            for argv in (['--supersede'], ['--reason', 'r']):
                with patch.object(sys, 'argv', ['finish_run.py', '--run-id', 'claude-arm1-core01', '--runs',
                                                str(root / '.runs')] + argv), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    finish_run.main()
        with tempfile.TemporaryDirectory() as tmp:                              # nothing to supersede yet
            root, _, _ = finish_fixture(tmp)
            with self.assertRaises(ValueError):
                self.finish(root, supersede_reason='r')

    def test_finish_comes_after_every_finalized_turn_is_captured(self):
        with tempfile.TemporaryDirectory() as tmp:                              # turn 0002 not captured yet
            root, run, folder = finish_fixture(tmp, turns=2, captured=False)
            capture_copy(run, folder, '0001')
            before = (folder / 'run.json').read_bytes()
            with self.assertRaises(ValueError) as ctx:
                self.finish(root)
            self.assertIn('0002', str(ctx.exception))
            self.assertNotIn('0001', str(ctx.exception))
            self.assertEqual((folder / 'run.json').read_bytes(), before)
            capture_copy(run, folder, '0002', complete=False)                     # capture stopped part-way
            with self.assertRaises(ValueError) as ctx:
                self.finish(root)
            self.assertIn('0002', str(ctx.exception))
            self.assertEqual((folder / 'run.json').read_bytes(), before)
            (folder / 'turns/0002/result.json').write_text('{"status": "ok"}')   # captured from another result
            mark_captured(folder / 'turns/0002')
            with self.assertRaises(ValueError):
                self.finish(root)
            shutil.copy2(run / 'turns/0002/result.json', folder / 'turns/0002/result.json')
            (folder / 'turns/0002/analysis.md').write_text('analysis')
            mark_captured(folder / 'turns/0002')
            for change in ('delete', 'edit'):                                     # a listed file gone or changed since
                with self.subTest(change=change):
                    if change == 'delete':
                        (folder / 'turns/0002/analysis.md').unlink()
                    else:
                        (folder / 'turns/0002/analysis.md').write_text('edited')
                    with self.assertRaises(ValueError) as ctx:
                        self.finish(root)
                    self.assertIn('0002', str(ctx.exception))
                    (folder / 'turns/0002/analysis.md').write_text('analysis')
            self.assertEqual(self.finish(root)[1], 'PASSED')
            self.assertEqual(self.record(folder)['turns_not_capturable'], [])
        with tempfile.TemporaryDirectory() as tmp:                              # a cut-short turn cannot be captured
            root, run, folder = finish_fixture(tmp, turns=2)
            (run / 'turns/0003').mkdir()
            write(run / 'turns/0003/invocation.json', {'turn_kind': 'feedback', 'started_at': finish_run.launch.utc()})
            _, _, _, fields = self.finish(root)
            self.assertEqual(self.record(folder)['turns_not_capturable'], ['0003'])
            self.assertIs(fields['usage_complete'], False)                      # and the ledger stays incomplete

    def test_runs_that_never_sent_a_turn_are_refused(self):
        for kw in (dict(record_status='NOT_STARTED'), dict(record_status='SETUP_FAILED'), dict(turns=0)):
            with self.subTest(**kw), tempfile.TemporaryDirectory() as tmp:
                root, _, folder = finish_fixture(tmp, **kw)
                with self.assertRaises(ValueError):
                    self.finish(root)
                self.assertEqual(self.record(folder)['status'], kw.get('record_status', 'RUNNING'))

    def test_invalid_infra_needs_reason_and_evidence_and_never_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, folder = finish_fixture(tmp, results={'C13': 'FAIL'})
            (folder / 'docker-error.txt').write_text('daemon died')
            for kw in (dict(infra_reason='docker daemon died'), dict(infra_reason=' ', infra_evidence=['docker-error.txt']),
                       dict(infra_reason='x', infra_evidence=['absent.txt'])):
                with self.assertRaises(ValueError):
                    self.finish(root, **kw)
            _, status, _, fields = self.finish(root, infra_reason='docker daemon died', infra_evidence=['docker-error.txt'])
            self.assertEqual(status, 'INVALID_INFRA')
            self.assertIs(fields['all_acceptance_passed'], False)
            self.assertEqual(self.record(folder)['invalid_infra']['evidence']['docker-error.txt'],
                             hashlib.sha256(b'daemon died').hexdigest())
        with tempfile.TemporaryDirectory() as tmp:                              # a table that passes everything is no infra failure
            root, _, folder = finish_fixture(tmp)
            (folder / 'e.txt').write_text('e')
            with self.assertRaises(ValueError):
                self.finish(root, infra_reason='x', infra_evidence=['e.txt'])

    def test_pass_or_fail_comes_from_the_verdict_and_the_cap_record_is_annotation(self):
        after = lambda call, status='CENSORED_BUDGET': {'status': status, 'first': dict(
            {'when': 'after call', 'overshoot': {'token_limit': 500}, 'used': {'token_limit': 20000500}, 'limits': {}},
            **({'call': call} if call else {}))}
        before_call = lambda status, call: {'status': status, 'first': {'when': 'before call', 'call': call,
                                                                       'overshoot': {'time_limit_seconds': 3.0}}}
        cases = {'turn overshoot': (after('turn'), ('CENSORED_BUDGET', 'after call', 'turn', {'token_limit': 500})),
                 'proxy overshoot': (after('user-proxy', 'CENSORED_TIME'),
                                     ('CENSORED_TIME', 'after call', 'user-proxy', {'token_limit': 500})),
                 'record without call kind': (after(None), ('CENSORED_BUDGET', 'after call', None, {'token_limit': 500})),
                 'blocked before a turn': (before_call('CENSORED_TIME', 'turn'), ('CENSORED_TIME', 'before call', 'turn', None)),
                 'blocked before a proxy call': (before_call('CENSORED_BUDGET', 'user-proxy'),
                                                 ('CENSORED_BUDGET', 'before call', 'user-proxy', None)),
                 'feedback cap': (before_call('CENSORED_TURNS', 'turn'), ('CENSORED_TURNS', 'before call', 'turn', None))}
        for name, (censored, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root, run, folder = finish_fixture(tmp, censored=censored)
                kept = (run / 'censored.json').read_bytes()
                _, status, cap, _ = self.finish(root)
                self.assertEqual(status, 'PASSED')                               # all 20 PASS: passed whatever the cap
                record = self.record(folder)
                self.assertIs(record['cap_reached'], True)
                self.assertEqual((record['cap_status'], record['cap_when'], record['cap_call'], record['cap_overshoot']),
                                 expected)
                self.assertEqual(record['cap'], cap)
                self.assertEqual((run / 'censored.json').read_bytes(), kept)     # never modified
                with self.assertRaises(RuntimeError):                            # still no further paid call
                    finish_run.launch.limit_gate(run, next_is_feedback=False)
            with self.subTest(name + ' / not passed'), tempfile.TemporaryDirectory() as tmp:
                root, _, folder = finish_fixture(tmp, censored=censored, results={'C01': 'FAIL'})
                self.assertEqual(self.finish(root)[1], censored['status'])     # not passed and capped: CENSORED_*
                self.assertIs(self.record(folder)['cap_reached'], True)
        with tempfile.TemporaryDirectory() as tmp:                              # no cap record
            root, _, folder = finish_fixture(tmp)
            self.assertEqual(self.finish(root)[1:3], ('PASSED', None))
            record = self.record(folder)
            self.assertEqual((record['cap_reached'], record['cap_status'], record['cap_overshoot']), (False, None, None))
        with tempfile.TemporaryDirectory() as tmp:                              # a cap record with no known status
            root, _, folder = finish_fixture(tmp, censored={'status': 'DONE', 'first': {}}, results={'C01': 'FAIL'})
            with self.assertRaises(ValueError):
                self.finish(root)
            self.assertEqual(self.record(folder)['status'], 'RUNNING')

    def test_pilot_records_go_to_the_pilot_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, folder = finish_fixture(tmp, arm=3, run_id='claude-pilot-01', pilot=True)
            self.assertEqual(folder, root / 'docs/pilots/claude-pilot-01')
            path, status, _, _ = self.finish(root, 'claude-pilot-01')
            self.assertEqual((path, status), (folder / 'run.json', 'PASSED'))
            record = self.record(folder)
            self.assertEqual((record['run_id'], record['pilot'], record['scope']), ('claude-pilot-01', True, 'PILOT_EXCLUDED'))
            self.assertFalse((root / 'docs/experiments').exists())
            write(folder / 'run.json', dict(record, run_id='claude-pilot-02'))   # folder of another run: refused
            with self.assertRaises(ValueError):
                self.finish(root, 'claude-pilot-01', supersede_reason='r')


class CloseoutRecordsTest(unittest.TestCase):
    """Spec 10.3 for capture_turn and 10.2/U10 for the user proxy."""

    def test_capture_picks_the_pilot_folder_and_refuses_another_runs_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp)
            root = shared / 'claude'
            shutil.copytree(capture_turn.ROOT.parent / 'recording', shared / 'recording',
                            ignore=shutil.ignore_patterns('__pycache__'))
            session = '11111111-2222-4333-8444-555555555555'
            run = root / '.runs/claude-pilot-01'
            write(run / 'manifest.json', dict(run_id='claude-pilot-01', pilot=True, arm=3, core=1, session_id=session,
                                              requested_model='claude-fable-5-2'))
            raw = run / 'turns/0001'
            write(raw / 'result.json', dict(status='ok', session_id=session, event_count=1, usage_complete=True))
            write(raw / 'invocation.json', {'turn_kind': 'initial', 'started_at': '2026-11-18T00:00:00+00:00'})
            for name in ('prompt.txt', 'stderr.txt'):
                (raw / name).write_text('x')
            (raw / 'stdout.jsonl').write_text('')
            (raw / 'events.jsonl').write_text(json.dumps(dict(type='message', id='r1', model='claude-fable-5-2', usage=USAGE,
                                                              role='pm')) + '\n')
            analysis = shared / 'analysis.md'
            analysis.write_text('analysis')
            with patch.object(capture_turn, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                capture_turn.capture('claude-pilot-01', 1, analysis, 'ITERATING')
                folder = root / 'docs/pilots/claude-pilot-01'
                self.assertTrue((folder / 'turns/0001/usage.jsonl').is_file())
                self.assertEqual(json.loads((folder / 'run.json').read_text())['run_id'], 'claude-pilot-01')
                self.assertTrue((folder / 'events.jsonl').is_file())             # journal initialized for the pilot
                self.assertFalse((root / 'docs/experiments').exists())
                # a measured-run manifest pointing at a folder that belongs to another run is refused
                write(run / 'manifest.json', dict(run_id='claude-pilot-01', pilot=False, arm=3, core=1, session_id=session))
                write(root / 'docs/experiments/3/core1/run.json', {'run_id': 'claude-arm3-core01'})
                with self.assertRaises(ValueError):
                    capture_turn.capture('claude-pilot-01', 1, analysis, 'ITERATING')
                self.assertFalse((root / 'docs/experiments/3/core1/turns').exists())

    def test_proxy_runs_on_the_runs_own_image_and_names_its_call_kind_in_the_cap_record(self):
        t = ConsultContractTest()
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = t.setup_run(tmp)
            seen = []
            inner = t.fake()
            def fake(cmd, **kw):
                if cmd[:2] == ['docker', 'create']:
                    seen.append(cmd)
                return inner(cmd, **kw)
            code, _, out = t.call(argv + ['--model', 'claude-opus-5-5'], fake)
            self.assertEqual(code, 0, out)
            self.assertIn('sha256:run-image', seen[0])                           # manifest image, not readiness digest
            self.assertNotIn('sha256:1', seen[0])
            usage = dict(input_tokens=freeze_readiness.launch.LIMITS['token_limit'] + 1, cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=0)
            (run_dir / 'consults/0001/events.jsonl').write_text(json.dumps(dict(type='message', id='big', model='m',
                                                                               role='user-proxy', turn='p', usage=usage)) + '\n')
            consult_contract.launch.record_overshoot(run_dir, 'user-proxy')
            self.assertEqual(json.loads((run_dir / 'censored.json').read_text())['first']['call'], 'user-proxy')
        with tempfile.TemporaryDirectory() as tmp:
            argv, run_dir = t.setup_run(tmp)
            write(run_dir / 'manifest.json', {'arm': 3, 'run_id': 'claude-arm3-core01'})
            code, run, out = t.call(argv + ['--model', 'claude-opus-5-5'], t.fake())
            self.assertNotEqual(code, 0)
            self.assertIn('pins no image', out)
            run.assert_not_called()


class LoadCheckTest(unittest.TestCase):
    def test_only_claude_owned_projects(self):
        done = subprocess.run([sys.executable, str(HERE / 'load_check.py'), '--project', 'payment-codex-c01-a1',
                               '--output', '/nonexistent', '--pending-sql-file', '/dev/null', '--audit-command-json', '[]'],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn('payment-claude-*', done.stderr)


class ExportSetupEvidenceTest(unittest.TestCase):
    TOKEN = 'sk-ant-oat01-EXPORTTEST-0123456789'

    def run_dir(self, runs, run_id, status='ok', leak=False, token_in=None):
        run = runs / run_id
        write(run / 'manifest.json', {'run_id': run_id, 'selftest': {'passed': True, 'gate_l1_write_exit': '2'}})
        turn = run / 'turns/0001'
        write(turn / 'invocation.json', {'hook_selftest_before_call': {'passed': True}})
        write(turn / 'result.json', {'status': status})
        (turn / 'stdout.jsonl').write_text('{"type":"result"}\n')
        if leak:
            (turn / 'SECRET-LEAK.txt').write_text('x')
        if token_in:
            (run / token_in).write_text('leak ' + self.TOKEN)
        return run

    def test_exports_finalised_leak_free_runs_only_and_never_overwrites(self):
        import export_setup_evidence as exporter
        with tempfile.TemporaryDirectory() as tmp:
            runs, dest = Path(tmp) / 'runs', Path(tmp) / 'v3'
            self.run_dir(runs, 'smoke-a3')
            written = exporter.export(runs, ['smoke-a3'], dest, self.TOKEN)
            self.assertEqual(sorted(written), ['smoke-a3-manifest.json', 'smoke-a3-turn-0001-invocation.json',
                                               'smoke-a3-turn-0001-result.json', 'smoke-a3-turn-0001-stdout.jsonl'])
            for name, digest in written.items():
                self.assertEqual(hashlib.sha256((dest / name).read_bytes()).hexdigest(), digest)
            with self.assertRaises(ValueError):                       # already exported: never overwritten
                exporter.export(runs, ['smoke-a3'], dest, self.TOKEN)
            # credential patterns of the harness secret guard: refused by file and pattern name, never echoing the value
            patterns = {'sk- key': 'sk-ant-api03-' + 'A' * 24, 'GitHub token': 'ghp_' + 'b' * 30,
                        'AWS access key (AKIA)': 'AKIA' + 'C' * 16, 'private key PEM block': '-----BEGIN RSA PRIVATE KEY-----',
                        'Authorization: Bearer': 'Authorization: Bearer abcdefgh12345678',
                        'credential assignment': 'PASSWORD=hunter2hunter2', 'JWT': 'eyJ' + 'a' * 12 + '.' + 'b' * 12 + '.' + 'c' * 12}
            for number, (label, value) in enumerate(patterns.items()):
                with self.subTest(label):
                    run_id = 'p-%d' % number
                    run = self.run_dir(runs, run_id)
                    (run / 'turns/0001/stdout.jsonl').write_text('{"text": "%s"}\n' % value)
                    with self.assertRaises(ValueError) as ctx:
                        exporter.export(runs, [run_id], Path(tmp) / ('out-' + run_id), None)
                    self.assertIn(label, str(ctx.exception))
                    self.assertIn('stdout.jsonl', str(ctx.exception))
                    self.assertNotIn(value, str(ctx.exception))
                    self.assertFalse((Path(tmp) / ('out-' + run_id)).exists())
            refused = {'collected': dict(status='collected'), 'failed': dict(status='failed'), 'leak': dict(leak=True),
                       'token in stream': dict(token_in='turns/0001/stdout.jsonl'), 'token in manifest': dict(token_in='manifest.json')}
            for name, options in refused.items():
                with self.subTest(name):
                    run_id = 'r-' + name.replace(' ', '-')
                    self.run_dir(runs, run_id, **options)
                    with self.assertRaises(ValueError):
                        exporter.export(runs, ['smoke-a3', run_id], Path(tmp) / ('out-' + run_id), self.TOKEN)
                    self.assertFalse((Path(tmp) / ('out-' + run_id)).exists())   # nothing written, not even the good run
            (runs / 'empty').mkdir()
            write(runs / 'empty/manifest.json', {})
            with self.assertRaises(ValueError):                       # a run without a turn proves nothing
                exporter.export(runs, ['empty'], Path(tmp) / 'out-empty', self.TOKEN)


class RefreshLimitsTest(unittest.TestCase):
    def root(self, tmp):
        root = Path(tmp) / 'claude'
        write(root / 'docs/results/run-manifest.example.json', {'token_limit': 150000000, 'time_limit_seconds': 28800,
                                                                 'feedback_limit': 30, 'provider': 'claude-code'})
        for arm in (1, 2, 3):
            for core in range(1, 11):
                write(root / 'docs/experiments' / str(arm) / ('core%d' % core) / 'run.json',
                      {'run_id': 'claude-arm%d-core%02d' % (arm, core), 'status': 'NOT_STARTED', 'token_limit': 20000000,
                       'time_limit_seconds': 28800, 'feedback_limit': 30, 'provider': 'claude-code', 'note': 'kept'})
        write(root / 'docs/results/cycles.json', [{'cycle': c, 'status': 'NOT_STARTED'} for c in range(1, 11)])
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty', '-m', 's'], check=True)
        return root

    def test_only_the_cap_fields_of_not_started_records_are_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            before = {p: json.loads(p.read_text()) for p in root.glob('docs/experiments/*/*/run.json')}
            changes = prepare.refresh_limits(root)
            self.assertEqual(len(changes), 30)
            self.assertEqual({(k, b, a) for _, k, b, a in changes}, {('token_limit', 20000000, 150000000)})
            for path, old in before.items():
                new = json.loads(path.read_text())
                self.assertEqual({k for k in set(old) | set(new) if old.get(k) != new.get(k)}, {'token_limit'})
            self.assertEqual(prepare.refresh_limits(root), [])                     # nothing left to change
            # a record not in NOT_STARTED is never touched (reachable only if the start check is passed)
            rec = root / 'docs/experiments/2/core3/run.json'
            write(rec, dict(json.loads(rec.read_text()), status='RUNNING', token_limit=20000000))
            with patch.object(freeze_readiness, 'experiment_started', return_value=[]):
                self.assertEqual(prepare.refresh_limits(root), [])
            self.assertEqual(json.loads(rec.read_text())['token_limit'], 20000000)

    def test_refused_once_a_measured_run_has_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            (root / '.runs/claude-arm1-core01').mkdir(parents=True)
            before = {p: p.read_text() for p in root.glob('docs/experiments/*/*/run.json')}
            with self.assertRaisesRegex(ValueError, 'measured run has started'):
                prepare.refresh_limits(root)
            self.assertEqual({p: p.read_text() for p in before}, before)


class FirstTurnTimeoutTest(unittest.TestCase):
    def test_start_cycle_first_turns_default_to_the_run_cap(self):
        # decision 2026-11-18: no per-turn limit; the runner reduces the timeout to the run time left
        source = (HERE / 'start_cycle.py').read_text()
        self.assertIn("default=freeze_readiness.LIMITS['time_limit_seconds']", source)
        self.assertEqual(freeze_readiness.LIMITS['time_limit_seconds'], 28800)


if __name__ == '__main__':
    unittest.main()
