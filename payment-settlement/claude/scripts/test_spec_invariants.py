"""Spec-derived invariant tests for the Claude-arm scripts (no docker, no model call, no git writes).

Source of every expectation: docs/plans/2026-11-17/claude-only-run/requirement-spec.md, sections 1, 2 and 5.
Each test names the spec sentence it checks. Expected values come from the spec, not from the implementation.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import commit_run
import consult_contract
import freeze_readiness
import preflight
import prepare
import start_cycle

launch = consult_contract.launch

OPUS = 'claude-opus-5-5'
FABLE = 'claude-fable-5-2'
PROTOCOL = 'payment-settlement-v3-claude'
SPEC_LIMITS = {'token_limit': 20_000_000, 'time_limit_seconds': 8 * 3600, 'feedback_limit': 30}
TOKEN = 'sk-ant-oat01-SPECINVARIANT-scripts-0123456789'
U1 = {'input_tokens': 10, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 30, 'output_tokens': 40}
FLAGS = ('isolation_verified', 'usage_collector_verified', 'harness_adapter_verified', 'acceptance_suite_verified',
         'pilot_passed', 'oauth_token_source_verified', 'role_models_verified', 'cache_observation_verified',
         'hook_enforcement_verified')


def ready(**extra):
    data = dict(protocol=PROTOCOL, provider='claude-code', auth='subscription-oauth-token', reasoning_effort='high',
                cache_policy='observed-cache', billed_cost=None, baseline_sha='abc', contract_sha256='def',
                image_digests={'runner': 'sha256:1'}, actual_models={'1': OPUS, '2': OPUS, '3': FABLE},
                user_proxy_model=OPUS,                                   # spec 2: 대리 사용자는 Opus 5.5 (frozen)
                **SPEC_LIMITS, **{f: True for f in FLAGS})
    data.update(extra)
    return data


def ledger(arm, **extra):
    data = dict(ready(), arm=arm, actual_model=OPUS if arm != 3 else FABLE)
    if arm == 3:
        data['requested_worker_model'] = OPUS
    data.update(extra)
    return data


def current_contract_sha():
    return hashlib.sha256(json.dumps(freeze_readiness.inventory(), sort_keys=True).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


class LimitsAndProtocol(unittest.TestCase):
    def test_limits_are_the_spec_caps_and_identical_for_every_arm(self):
        # spec 2: 상한: run당 input+output 2,000만 토큰 · 8시간 · 피드백 30회 / 세 조건의 ... 상한은 같다
        self.assertEqual(dict(preflight.LIMITS), SPEC_LIMITS)
        self.assertEqual(dict(freeze_readiness.LIMITS), SPEC_LIMITS)
        for arm in (1, 2, 3):
            self.assertEqual(preflight.blockers(ledger(arm)), [], 'arm %d' % arm)
            for key, value in SPEC_LIMITS.items():
                for wrong in (value - 1, value + 1, value * 10, None):
                    self.assertTrue(preflight.blockers(ledger(arm, **{key: wrong})), '%s=%r accepted for arm %d' % (key, wrong, arm))

    def test_protocol_is_v3_claude(self):
        # spec 1-A: 새 프로토콜 버전(payment-settlement-v3-claude)으로 동결
        self.assertEqual(preflight.PROTOCOL, PROTOCOL)
        self.assertEqual(freeze_readiness.PROTOCOL, PROTOCOL)
        for other in ('payment-settlement-v1', 'payment-settlement-v2', 'payment-settlement-v3', None):
            self.assertTrue(preflight.blockers(ledger(1, protocol=other)))

    def test_preflight_flags_must_be_literally_true(self):
        # spec 5: preflight.py 전 항목 true(가짜 true 금지)
        for arm in (1, 2, 3):
            for flag in FLAGS:
                if arm != 3 and flag == 'hook_enforcement_verified':
                    continue
                for fake in ('true', 'True', 1, 'yes', 'pending', [True], None):
                    self.assertTrue(preflight.blockers(ledger(arm, **{flag: fake})), '%s=%r passed arm %d' % (flag, fake, arm))

    def test_effort_high_is_part_of_the_gate(self):
        # spec 2: 구현 모델(Opus 5.5, effort high)
        for arm in (1, 2, 3):
            self.assertTrue(preflight.blockers(ledger(arm, reasoning_effort='medium')))


class ModelsPerSpec(unittest.TestCase):
    def test_requested_models_per_arm(self):
        # spec 2: 구현 모델 Opus 5.5 · 하네스 조건만 PM(Fable 5.2) / §0: 구현자·워커 claude-opus-5-5, PM claude-fable-5-2
        self.assertEqual(launch.MODELS[1], (OPUS, None))
        self.assertEqual(launch.MODELS[2], (OPUS, None))
        self.assertEqual(launch.MODELS[3], (FABLE, OPUS))
        self.assertEqual(launch.REVIEWER_MODEL, FABLE)          # spec 2: 두 번째 리뷰어 Fable 5.2
        self.assertEqual(prepare.MODELS[1]['requested_model'], OPUS)
        self.assertEqual(prepare.MODELS[2]['requested_model'], OPUS)
        self.assertEqual(prepare.MODELS[3]['requested_model'], FABLE)
        self.assertEqual(prepare.MODELS[3]['requested_worker_model'], OPUS)
        for arm in (1, 2):
            self.assertNotIn('requested_worker_model', prepare.MODELS[arm])

    def smoke(self, runs, run_id, arm, requested, observed):
        write(runs / run_id / 'manifest.json', dict(
            arm=arm, requested_model=requested.get('main'), requested_worker_model=requested.get('worker'),
            requested_reviewer_model=requested.get('reviewer'), actual_role_models=observed,
            selftest=dict(passed=True, gate_l1_write_exit='2', gate_docs_write_exit='0', git_guard_push='ask',
                          harness_hash_match='1', reviewer_on_path='1')))
        write(runs / run_id / 'turns/0001/result.json', dict(
            status='ok', session_id_confirmed=True, usage_complete=True,
            config_volume=dict(credentials_file_in_volume=False), usage_cross_check=dict(reported=U1)))

    def test_role_models_verified_only_when_observed_ids_are_opus_and_fable(self):
        # spec 5 인증 스모크: 실제 모델 ID가 Opus 5.5·Fable 5.2로 기록되는지 확인
        arm3_req = {'main': FABLE, 'worker': OPUS, 'reviewer': FABLE}
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            self.smoke(runs, 'a1', 1, {'main': OPUS}, {'implementer': [OPUS + '-20261101']})
            self.smoke(runs, 'a3', 3, arm3_req, {'pm': [FABLE], 'worker': [OPUS], 'reviewer': [FABLE]})
            flags, actual, _ = freeze_readiness.smoke_evidence(runs, ['a1', 'a3'])
            self.assertTrue(flags['role_models_verified'])
            self.assertTrue(actual['1'].startswith(OPUS) and actual['3'] == FABLE)
            wrong = {
                'arm1 served Fable': ('b1', 1, {'main': OPUS}, {'implementer': [FABLE]}),
                'arm3 PM served Opus': ('b3', 3, arm3_req, {'pm': [OPUS], 'worker': [OPUS], 'reviewer': [FABLE]}),
                'arm3 worker served Fable': ('c3', 3, arm3_req, {'pm': [FABLE], 'worker': [FABLE], 'reviewer': [FABLE]}),
                'arm3 reviewer served Opus': ('d3', 3, arm3_req, {'pm': [FABLE], 'worker': [OPUS], 'reviewer': [OPUS]}),
                # requested and observed agree, but neither is the agreed model: still not the spec's model control
                'arm1 requested and served another model': ('e1', 1, {'main': 'claude-sonnet-5'}, {'implementer': ['claude-sonnet-5']}),
                'arm3 PM requested and served Opus': ('f3', 3, dict(arm3_req, main=OPUS), {'pm': [OPUS], 'worker': [OPUS], 'reviewer': [FABLE]}),
            }
            for name, (run_id, arm, req, obs) in wrong.items():
                with self.subTest(name):
                    self.smoke(runs, run_id, arm, req, obs)
                    pair = [run_id, 'a3'] if arm != 3 else ['a1', run_id]
                    self.assertFalse(freeze_readiness.smoke_evidence(runs, pair)[0]['role_models_verified'], name)

    def test_no_flag_is_true_without_evidence(self):
        # spec 5: preflight.py 전 항목 true(가짜 true 금지)
        flags, actual, _ = freeze_readiness.smoke_evidence(Path('/nonexistent'), [])
        self.assertFalse(any(flags.values()))
        self.assertEqual(actual, {})
        with tempfile.TemporaryDirectory() as tmp:
            attest = Path(tmp) / 'a.json'
            for flag in ('pilot_passed', 'acceptance_suite_verified', 'harness_adapter_verified'):
                attest.write_text(json.dumps({flag: {'value': True, 'evidence': []}}))
                with self.assertRaises(ValueError):
                    freeze_readiness.attested(attest)
            for flag in ('isolation_verified', 'usage_collector_verified', 'role_models_verified', 'oauth_token_source_verified'):
                attest.write_text(json.dumps({flag: {'value': True, 'evidence': ['README.md']}}))
                with self.assertRaises(ValueError):
                    freeze_readiness.attested(attest)


class TerminalStatuses(unittest.TestCase):
    def test_failed_and_censored_runs_are_terminal_and_committable(self):
        # spec 2: 먼저 닿으면 CENSORED_*로 끝내고 보존한다. 실패 run을 결과에서 지우지 않는다.
        self.assertIn('FAILED', commit_run.TERMINAL)
        censored = {s for s in commit_run.TERMINAL if s.startswith('CENSORED_')}
        self.assertGreaterEqual(len(censored), 3, 'one censoring status per cap (tokens, time, feedback): %s' % censored)


class RunIsolationAndOneToOne(unittest.TestCase):
    def test_every_run_has_its_own_db_project_and_ports(self):
        # spec 2: run마다 새 컨테이너·새 세션·새 DB
        plans = [start_cycle.run_plan(cycle, arm) for cycle in range(1, 11) for arm in (1, 2, 3)]
        self.assertEqual(len({p[0] for p in plans}), 30)
        self.assertEqual(len({p[1] for p in plans}), 30)
        ports = [v for p in plans for v in p[2].values()]
        self.assertEqual(len(ports), len(set(ports)), 'port collision between runs')
        for _, _, p in plans:
            for v in p.values():
                self.assertTrue(1024 < int(v) < 65536)

    def test_schedule_and_cycles_map_one_to_one_onto_30_runs(self):
        # spec 5: 30행 결과 표가 run 기록과 1:1로 대조된다(누락·중복 0) / spec 1: 세 조건 × 10회 = 30회
        root = HERE.parent
        schedule = json.loads((root / 'docs/results/schedule.json').read_text())
        cycles = json.loads((root / 'docs/results/cycles.json').read_text())
        pairs = [(r['arm'], r['core']) for r in schedule]
        self.assertEqual(sorted(pairs), sorted(itertools.product((1, 2, 3), range(1, 11))))
        ids = [r['run_id'] for r in schedule]
        self.assertEqual(len(ids), len(set(ids)))
        for r in schedule:
            self.assertEqual(r['run_id'], start_cycle.run_plan(r['core'], r['arm'])[0])
            ledger_file = root / 'docs/experiments' / str(r['arm']) / ('core%d' % r['core']) / 'run.json'
            self.assertTrue(ledger_file.is_file(), ledger_file)
            self.assertEqual(json.loads(ledger_file.read_text())['run_id'], r['run_id'])
        self.assertEqual([c['cycle'] for c in cycles], list(range(1, 11)))
        cycle_ids = [rid for c in cycles for rid in c['runs']]
        self.assertEqual(sorted(cycle_ids), sorted(ids))
        for c in cycles:
            self.assertEqual(sorted(c['runs']), sorted(start_cycle.run_plan(c['cycle'], a)[0] for a in (1, 2, 3)))


class StartCycleLaunch(unittest.TestCase):
    def test_cycle_launches_three_arms_with_identical_caps_and_no_credential_value(self):
        # spec 2: 세 조건의 ... 자원 한도·상한은 같다 / 하네스 조건만 하네스 / 자격증명 값은 ... 남기지 않는다
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'claude'
            for arm in (1, 2, 3):
                write(root / 'docs/experiments' / str(arm) / 'core1' / 'run.json',
                      dict(ledger(arm), run_id='claude-arm%d-core01' % arm, core=1, status='NOT_STARTED'))
            write(root / 'docs/results/cycles.json', [dict(cycle=c, status='NOT_STARTED') for c in range(1, 11)])
            readiness = Path(tmp) / 'readiness.json'
            # the frozen inputs are unchanged since the freeze (spec 2: 동결 후 바꾸지 않는다), so the freeze hash is current
            write(readiness, ready(contract_sha256=current_contract_sha()))
            env = Path(tmp) / 'claude.env'
            env.write_text('CLAUDE_CODE_OAUTH_TOKEN=' + TOKEN + '\n')
            os.chmod(env, 0o600)
            calls = []

            def fake_run(cmd, **kw):
                calls.append([str(c) for c in cmd])
                return SimpleNamespace(returncode=0)
            argv = ['start_cycle.py', '--cycle', '1', '--readiness', str(readiness), '--env-file', str(env)]
            with patch.object(start_cycle, 'ROOT', root), patch.object(sys, 'argv', argv), \
                 patch.object(start_cycle.subprocess, 'run', side_effect=fake_run), \
                 patch.object(start_cycle.lab, 'verify'), patch.object(start_cycle.lab, 'prompt', side_effect=lambda arm: 'p%d' % arm), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(start_cycle.main(), 0)
            inits = [c for c in calls if 'init' in c]
            turns = [c for c in calls if 'turn' in c]
            self.assertEqual(len(inits), 3)
            self.assertEqual(len(turns), 3)

            def opt(cmd, flag):
                return cmd[cmd.index(flag) + 1] if flag in cmd else None
            self.assertEqual({opt(c, '--arm') for c in inits}, {'1', '2', '3'})
            for c in inits:
                self.assertEqual(opt(c, '--harness') is not None, opt(c, '--arm') == '3')
            self.assertEqual(len({opt(c, '--image') for c in inits}), 1)
            for flag in ('--timeout-seconds', '--cache-policy', '--max-turns', '--max-budget-usd'):
                self.assertEqual(len({opt(c, flag) for c in turns}), 1, flag)
            for c in calls:
                self.assertNotIn(TOKEN, ' '.join(c))
            for path in root.rglob('*'):
                if path.is_file():
                    self.assertNotIn(TOKEN, path.read_text(), path)

    def test_cycle_refuses_when_frozen_inputs_no_longer_match_the_freeze(self):
        # spec 2: 판정 기준과 요구사항은 동결 후 바꾸지 않는다. 바꾸면 프로토콜 버전을 올리고 전 조건 재실행이다.
        # A readiness whose contract_sha256 does not describe the current frozen inputs must not start a cycle.
        with tempfile.TemporaryDirectory() as tmp:
            readiness = Path(tmp) / 'readiness.json'
            write(readiness, ready(contract_sha256='0' * 64))
            argv = ['start_cycle.py', '--cycle', '1', '--readiness', str(readiness), '--env-file', '/nonexistent', '--dry-run']
            with patch.object(sys, 'argv', argv), patch.object(start_cycle.subprocess, 'run') as run, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = start_cycle.main()
                except SystemExit as exc:
                    code = exc.code
                run.assert_not_called()
            self.assertNotEqual(code, 0, 'cycle gate accepted a freeze hash that does not match the current contract')


class FrozenContract(unittest.TestCase):
    def test_freeze_covers_requirements_acceptance_and_evaluator(self):
        # spec 2: 판정 기준과 요구사항은 동결 후 바꾸지 않는다 / spec 3: 계약·판정 기준소스
        keys = set(freeze_readiness.inventory())
        for rel in ('docs/requirements.md', 'docs/acceptance.md', 'docs/answer-policy.md', 'docs/resources.md',
                    'claude/prompts/feedback.md'):
            self.assertIn(rel, keys)
        self.assertTrue(any(k.startswith('evaluation/') for k in keys))
        self.assertTrue(any(k.startswith('infra/') for k in keys))

    def test_changing_acceptance_changes_the_frozen_inventory(self):
        # spec 2: 바꾸면 프로토콜 버전을 올리고 — a change must be detectable from the freeze record
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp)
            (shared / 'docs').mkdir()
            (shared / 'docs/acceptance.md').write_text('C01 v1')
            with patch.object(freeze_readiness, 'SHARED', shared):
                before = freeze_readiness.inventory()
                (shared / 'docs/acceptance.md').write_text('C01 v2')
                after = freeze_readiness.inventory()
            self.assertNotEqual(before, after)


class UserProxyAccounting(unittest.TestCase):
    """Spec 2: 진행자·판정 어댑터·대리 사용자의 토큰은 역할별로 분리 기록한다 / 자격증명 값은 ... 남기지 않는다."""

    def run_proxy(self, tmp, copy_ok=True, leak=False, model=OPUS, arm=3, censored=False, turns=()):
        """turns: kinds of turns already delivered in the run (each 'ok'), as the runner records them."""
        env = Path(tmp) / 'claude.env'
        env.write_text('CLAUDE_CODE_OAUTH_TOKEN=' + TOKEN + '\n')
        os.chmod(env, 0o600)
        questions = Path(tmp) / 'q.md'
        questions.write_text('질문')
        readiness = Path(tmp) / 'readiness.json'
        write(readiness, ready())
        runs = Path(tmp) / 'runs'
        run = runs / 'claude-arm3-core01'
        (run / 'turns').mkdir(parents=True)
        write(run / 'manifest.json', dict(run_id='claude-arm3-core01', arm=arm, session_id='11111111-2222-4333-8444-555555555555',
                                          requested_model=FABLE, requested_worker_model=OPUS, requested_reviewer_model=FABLE))
        if censored:
            write(run / 'censored.json', {'status': 'CENSORED_BUDGET'})
        for i, kind in enumerate(turns, 1):
            d = run / 'turns' / ('%04d' % i)
            write(d / 'invocation.json', {'started_at': datetime.now(timezone.utc).isoformat(), 'turn_kind': kind, 'counts_as': kind})
            write(d / 'result.json', {'status': 'ok', 'usage_complete': True})
            (d / 'events.jsonl').write_text('')
        out = run / 'consults' / '0001'
        state = {'calls': []}

        def fake_run(cmd, **kw):
            state['calls'].append(list(cmd))
            if cmd[:2] == ['docker', 'create']:
                state['session'] = cmd[cmd.index('--session-id') + 1]
                state['create'] = cmd
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'start']:
                kw['stdout'].write(json.dumps({'type': 'result', 'result': 'APPROVED' + (' ' + TOKEN if leak else ''), 'session_id': state['session'],
                                               'usage': U1}).encode() + b'\n')
                return SimpleNamespace(returncode=0)
            if cmd[:2] == ['docker', 'cp']:
                if not copy_ok:
                    return SimpleNamespace(returncode=1, stdout=b'', stderr=b'copy failed')
                target = Path(cmd[-1]) / '-tmp'
                target.mkdir(parents=True)
                lines = [{'type': 'assistant', 'requestId': 'req-proxy', 'sessionId': state['session'],
                          'message': {'model': OPUS, 'usage': U1}},
                         {'type': 'user', 'text': 'echo ' + (TOKEN if leak else 'nothing')}]
                (target / (state['session'] + '.jsonl')).write_text(''.join(json.dumps(l) + '\n' for l in lines))
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        argv = ['consult_contract.py', '--run-id', 'claude-arm3-core01', '--questions', str(questions), '--runs', str(runs),
                '--model', model, '--env-file', str(env), '--readiness', str(readiness)]
        console = io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(consult_contract.subprocess, 'run', side_effect=fake_run), \
             contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            try:
                code = consult_contract.main()
            except SystemExit as exc:
                code = exc.code
        self.run_dir = run
        return code, out, state, console.getvalue()

    def test_user_proxy_usage_is_recorded_under_its_own_role_and_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, state, console = self.run_proxy(tmp)
            self.assertEqual(code, 0, console)
            usage = json.loads((out / 'usage.json').read_text())
            self.assertEqual(usage['role'], 'user-proxy')
            self.assertEqual(usage['usage'], U1)
            self.assertIs(usage['usage_complete'], True)
            events = [json.loads(l) for l in (out / 'events.jsonl').read_text().splitlines()]
            self.assertEqual({e['role'] for e in events}, {'user-proxy'})
            self.assertNotIn('--mount', state['create'])                 # never writes into a run's state
            self.assertNotIn('--resume', state['create'])                # its own fresh session

    def test_credential_leaked_by_the_user_proxy_is_scrubbed_and_fails_the_call(self):
        # spec 2: 자격증명 값은 저장소·로그·기록 어디에도 남기지 않는다
        # (test corrected 2026-11-17: the first draft expected exit 0 with a leaked token; the spec does not ask a
        #  leaking call to succeed, and failing closed is consistent with it, so only "no value persisted" is asserted)
        with tempfile.TemporaryDirectory() as tmp:
            code, out, state, console = self.run_proxy(tmp, leak=True)
            self.assertNotEqual(code, 0)
            for path in self.run_dir.rglob('*'):
                self.assertNotIn(TOKEN, path.name)
                if path.is_file():
                    self.assertNotIn(TOKEN.encode(), path.read_bytes(), path)
            self.assertNotIn(TOKEN, console)

    def test_unobserved_user_proxy_usage_is_not_zero_filled(self):
        # spec 2: 관측하지 못한 사용량은 0으로 채우지 않고 usage_complete=false로 남긴다
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _, _ = self.run_proxy(tmp, copy_ok=False)
            usage = json.loads((out / 'usage.json').read_text())
            self.assertIs(usage['usage_complete'], False)
            self.assertNotEqual(usage['usage'], {k: 0 for k in launch.USAGE_KEYS},
                                'unobserved proxy usage recorded as zeros')


class StageGatesAndProxyModel(unittest.TestCase):
    def test_arm3_gate_requires_the_agreed_user_proxy_model(self):
        # spec 2: 대리 사용자는 Opus 5.5다 / §0 추가 결정: 대리 사용자 모델 → Opus 5.5 (동결 대상)
        self.assertEqual(preflight.blockers(ledger(3)), [])
        for other in (FABLE, 'claude-sonnet-5', None, ''):
            self.assertTrue(preflight.blockers(ledger(3, user_proxy_model=other)), repr(other))

    def test_pilot_gate_waives_only_pilot_passed_and_cycle_gate_requires_it(self):
        # §0 추가 결정: 파일럿 시작 게이트는 파일럿 통과를 요구하지 않고 본 사이클 게이트가 요구하도록 나눈다
        for arm in (1, 2, 3):
            not_piloted = ledger(arm, pilot_passed=False)
            self.assertEqual(preflight.blockers(not_piloted, 'pilot'), [], 'arm %d' % arm)
            self.assertTrue(preflight.blockers(not_piloted, 'cycle'))
            self.assertTrue(preflight.blockers(not_piloted))                 # default is the strict cycle gate
            for flag in FLAGS:
                if flag == 'pilot_passed' or (arm != 3 and flag == 'hook_enforcement_verified'):
                    continue
                self.assertTrue(preflight.blockers(ledger(arm, pilot_passed=False, **{flag: False}), 'pilot'),
                                'pilot gate waived %s for arm %d' % (flag, arm))
        failures = start_cycle.gate(ready(pilot_passed=False), 1)              # measured cycles use the cycle gate
        self.assertTrue(any('pilot_passed' in f for f in failures))


def smoke_run(runs, run_id, arm, tools, observed=None):
    write(runs / run_id / 'manifest.json', dict(
        arm=arm, requested_model=FABLE if arm == 3 else OPUS, requested_worker_model=OPUS if arm == 3 else None,
        requested_reviewer_model=FABLE if arm == 3 else None, observed_tools=sorted(tools),
        actual_role_models=observed or ({'pm': [FABLE]} if arm == 3 else {'implementer': [OPUS]}),
        selftest=dict(passed=True, gate_l1_write_exit='2', gate_docs_write_exit='0', git_guard_push='ask',
                      harness_hash_match='1', reviewer_on_path='1')))
    write(runs / run_id / 'turns/0001/result.json', dict(
        status='ok', session_id_confirmed=True, usage_complete=True,
        config_volume=dict(credentials_file_in_volume=False), usage_cross_check=dict(reported=U1)))


BASE_TOOLS = {'Bash', 'Read', 'Edit', 'Write', 'Glob', 'Grep', 'WebSearch', 'WebFetch'}


class SameToolsEvidence(unittest.TestCase):
    """Spec 2: 도구(웹 도구 포함)는 같다 ... 하네스 조건만 PM·서브에이전트·하네스 훅을 갖는다 — on the observed tool surfaces."""

    def isolation(self, t1, t2, t3):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            smoke_run(runs, 's1', 1, t1)
            smoke_run(runs, 's2', 2, t2)
            smoke_run(runs, 's3', 3, t3)
            return freeze_readiness.smoke_evidence(runs, ['s1', 's2', 's3'])[0]['isolation_verified']

    def test_same_surface_plus_subagents_in_arm3_only(self):
        self.assertTrue(self.isolation(BASE_TOOLS, BASE_TOOLS, BASE_TOOLS | {'Task'}))
        cases = {
            'arm 3 with an extra non-subagent tool': (BASE_TOOLS, BASE_TOOLS, BASE_TOOLS | {'Task', 'Skill'}),
            'arm 3 without subagents': (BASE_TOOLS, BASE_TOOLS, BASE_TOOLS),
            'arm 2 without a web tool': (BASE_TOOLS, BASE_TOOLS - {'WebFetch'}, BASE_TOOLS | {'Task'}),
            'arm 3 without a web tool': (BASE_TOOLS, BASE_TOOLS, (BASE_TOOLS - {'WebSearch'}) | {'Task'}),
            'arm 1 with subagents': (BASE_TOOLS | {'Task'}, BASE_TOOLS, BASE_TOOLS | {'Task'}),
        }
        for name, tools in cases.items():
            with self.subTest(name):
                self.assertFalse(self.isolation(*tools), name)


class WorkerModels(unittest.TestCase):
    """Spec 2: 하네스 조건의 구현·테스트·리뷰 워커는 Opus 5.5이고, 탐색 워커는 하네스 정의(Sonnet)를 따른다."""

    def worker_check(self, events):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'r'
            (run / 'turns/0001').mkdir(parents=True)
            (run / 'turns/0001/events.jsonl').write_text(''.join(json.dumps(dict(
                id='w%d' % i, role='worker', model=m, usage=U1, turn='0001', agent_type=t)) + '\n' for i, (m, t) in enumerate(events)))
            observed = {'pm': [FABLE], 'worker': sorted({m for m, _ in events})}
            return freeze_readiness.role_check(3, observed, ('pm', 'worker'), run)['worker']

    def test_worker_models_per_agent_type(self):
        sonnet = 'claude-sonnet-5'
        self.assertTrue(self.worker_check([(OPUS, 'general-purpose'), (OPUS, None), (sonnet, 'explorer')]))
        self.assertTrue(self.worker_check([(OPUS, None)]))
        for name, events in {'explorer on Opus': [(OPUS, None), (OPUS, 'explorer')],
                             'implementation worker on Sonnet': [(sonnet, None)],
                             'test/review worker on Sonnet': [(OPUS, None), (sonnet, 'general-purpose')],
                             'worker on Fable': [(FABLE, None)]}.items():
            with self.subTest(name):
                self.assertFalse(self.worker_check(events), name)


class UserProxyGates(UserProxyAccounting):
    """Spec 2: 대리 사용자는 Opus 5.5다 / 대리 사용자의 토큰은 역할별로 분리 기록한다 / 먼저 닿으면 CENSORED_*로 끝낸다."""
    test_user_proxy_usage_is_recorded_under_its_own_role_and_session = None
    test_credential_leaked_by_the_user_proxy_is_scrubbed_and_fails_the_call = None
    test_unobserved_user_proxy_usage_is_not_zero_filled = None

    def test_user_proxy_must_be_opus_5_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, state, _ = self.run_proxy(tmp, model=FABLE)
            self.assertNotEqual(code, 0)
            self.assertFalse(state['calls'])

    def test_user_proxy_tokens_enter_the_run_ledger_as_their_own_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _, console = self.run_proxy(tmp)
            self.assertEqual(code, 0, console)
            ledger = json.loads((self.run_dir / 'ledger.json').read_text())
            self.assertEqual(set(ledger['roles']), {'user-proxy'})
            self.assertEqual(ledger['roles']['user-proxy']['usage'], U1)

    def test_censored_run_gets_no_user_proxy_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, state, _ = self.run_proxy(tmp, censored=True)
            self.assertNotEqual(code, 0)
            self.assertFalse([c for c in state['calls'] if c[:2] == ['docker', 'create']])


class UserProxyAndFeedbackCap(UserProxyAccounting):
    """§0 추가 결정(2026-11-17): 평가자가 판정 결과를 전하는 후속 턴만 30회에 넣는다. 하네스 조건의 질의응답 턴
    (대리 사용자의 답을 PM에게 전하는 턴)은 상한에 넣지 않고 횟수를 따로 기록한다 (experiment-plan.md:25,51)."""
    test_user_proxy_usage_is_recorded_under_its_own_role_and_session = None
    test_credential_leaked_by_the_user_proxy_is_scrubbed_and_fails_the_call = None
    test_unobserved_user_proxy_usage_is_not_zero_filled = None

    def test_consultation_after_thirty_feedbacks_is_not_refused_by_the_feedback_cap(self):
        # the cap stops the 31st evaluator feedback; answering the PM's questions stays outside it
        with tempfile.TemporaryDirectory() as tmp:
            code, _, state, console = self.run_proxy(tmp, turns=['initial'] + ['feedback'] * 30)
            self.assertEqual(code, 0, console)
            self.assertTrue([c for c in state['calls'] if c[:2] == ['docker', 'create']])
            self.assertFalse((self.run_dir / 'censored.json').exists())

    def test_consultation_is_not_counted_as_a_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _, console = self.run_proxy(tmp, turns=['initial'] + ['feedback'] * 29)
            self.assertEqual(code, 0, console)
            state = launch.limit_state(self.run_dir)
            self.assertEqual(state['used']['feedback_limit'], 29)
            ledger = json.loads((self.run_dir / 'ledger.json').read_text())
            self.assertEqual(ledger['turn_kinds']['delivered']['feedback'], 29)
            launch.limit_gate(self.run_dir, next_is_feedback=True, write=False)   # the 30th feedback is still allowed (no raise)

    def test_user_proxy_exists_only_in_the_harness_arm(self):
        # 하네스 조건의 질의응답 턴 / 대리 사용자는 하네스 조건의 인터뷰 상대 — arms 1 and 2 get no proxy call
        for arm in (1, 2):
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as tmp:
                code, _, state, _ = self.run_proxy(tmp, arm=arm)
                self.assertNotEqual(code, 0)
                self.assertFalse([c for c in state['calls'] if c[:2] == ['docker', 'create']])


if __name__ == '__main__':
    unittest.main()
