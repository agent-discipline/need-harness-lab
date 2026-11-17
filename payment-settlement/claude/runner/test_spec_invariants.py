"""Spec-derived invariant tests for runner/launch.py (no docker, no model call, no network).

Source of every expectation: docs/plans/2026-11-17/claude-only-run/requirement-spec.md, sections 0, 2, 5 and 10
(with docs/answer-policy.md and claude/docs/runbook.md for the interface names).
Each test names the spec sentence it checks. Expected values come from the spec, not from the implementation.
"""
import atexit
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('launch_spec_invariants', HERE / 'launch.py')
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)

# Spec values (section 0 Q/A and section 2).
OPUS = 'claude-opus-5-5'
FABLE = 'claude-fable-5-2'
WEB = ('WebSearch', 'WebFetch')
SUBAGENT_SURFACE = ('Agent', 'Task')
TOKEN = 'sk-ant-oat01-SPECINVARIANT-0123456789abcdef'
KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
U1 = {'input_tokens': 10, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 30, 'output_tokens': 40}
U2 = {'input_tokens': 1, 'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 3, 'output_tokens': 4}
SELFTEST_OK = {'passed': True, 'selftest_failed': '0'}


def add(*usages):
    return {k: sum(u[k] for u in usages) for k in KEYS}


def make_root(tmp):
    """A fake payment-settlement/claude tree: three starters, a harness snapshot and the runner seed files."""
    root = Path(tmp) / 'claude'
    for arm in (1, 2, 3):
        (root / str(arm) / 'core1').mkdir(parents=True)
        (root / str(arm) / 'core1' / 'Main.kt').write_text('starter')
    harness = root / 'harness-source'
    for rel in ('src', 'hooks', 'playbooks', 'templates', 'agents'):
        (harness / rel).mkdir(parents=True)
    (harness / 'src/core.md').write_text('core')
    (harness / 'HISTORY.md').write_text('history')
    (harness / 'hooks/gate-guard.sh').write_text('#!/bin/bash\n')
    (harness / 'playbooks/review.md').write_text('review')
    (harness / 'templates/log.md').write_text('log')
    (harness / 'agents/explorer.md').write_text('explorer')
    (root / 'runner').mkdir()
    (root / 'runner/harness-bootstrap-CLAUDE.md').write_text('@core.md')
    (root / 'runner/harness-settings.json').write_text('{}')
    return root


def init_run(root, run_id, arm, harness='default', seed=None, image='img', tools=None, core=1, **options):
    """launch.py init through launch.initialize. tools: the CLI tool surface the (no-cost) init self-test reports.
    options: further init options (readiness=..., pilot=True)."""
    if harness == 'default':
        harness = root / 'harness-source' if arm == 3 else None
    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, arm=arm, core=core, image=image, harness=harness, network=None,
                           **options)
    report = dict(SELFTEST_OK, cli_tools=','.join(tools)) if tools else SELFTEST_OK
    seeder = seed or patch.object(launch, 'seed_harness', return_value='listing')
    with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
         patch.object(launch, 'selftest', return_value=report), seeder, contextlib.redirect_stdout(io.StringIO()):
        launch.initialize(args)
    return json.loads((root / '.runs' / run_id / 'manifest.json').read_text())


def env_file(directory):
    path = Path(directory) / 'claude.env'
    path.write_text(launch.TOKEN_KEY + '=' + TOKEN + '\n')
    os.chmod(path, 0o600)
    return path


def assistant(rid, model, usage, session='s', sidechain=False):
    line = {'type': 'assistant', 'requestId': rid, 'sessionId': session, 'message': {'model': model}}
    if usage is not None:
        line['message']['usage'] = usage
    if sidechain:
        line['isSidechain'] = True
        line['agentId'] = 'a1'
    return line


def selftest_patch(effect=None, report=SELFTEST_OK):
    """The hook self-test is a no-cost helper container; it reports `report` (default: passed), optionally after running
    `effect(manifest)`."""
    if effect is None:
        return patch.object(launch, 'selftest', return_value=report)

    def run(manifest, *a, **k):
        effect(manifest)
        return report
    return patch.object(launch, 'selftest', side_effect=run)


def env_values(cmd):
    return dict(cmd[i + 1].split('=', 1) for i, arg in enumerate(cmd) if arg == '--env' and '=' in cmd[i + 1])


def drive_turn(root, run_id, *, resume=False, kind=None, reported=None, projects=None, projects_rc=0, stderr=b'',
               reviewer_log=None, reviewer_projects=None, side_effect=None, stdout_extra=b'', timeout=5, record=None,
               selftest=None):
    """Run launch.turn with a fake docker. projects: {relative path: [line dict or raw str]}.
    kind: the declared turn kind of a resumed turn (--kind); a resumed turn without one is the caller's choice to test.
    selftest: optional side effect of the (no-cost) hook self-test, e.g. to let wall-clock time pass during it."""
    if resume and kind is None:
        kind = 'feedback'          # default for the generic accounting tests: an evaluator feedback turn
    dest = root / '.runs' / run_id
    manifest = json.loads((dest / 'manifest.json').read_text())
    prompt = Path(root).parent / 'prompt.txt'
    prompt.write_text('build it')

    def write_tree(target, tree):
        for rel, lines in tree.items():
            path = Path(target) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join((l if isinstance(l, str) else json.dumps(l)) + '\n' for l in lines))

    def fake_run(cmd, **kw):
        if record is not None:
            record.append((list(cmd), kw.get('timeout')))
        if cmd[:2] == ['docker', 'create']:
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if cmd[:2] == ['docker', 'start']:
            result = {'type': 'result', 'subtype': 'success', 'session_id': manifest['session_id'], 'total_cost_usd': 0.1}
            if reported is not None:
                result['usage'] = reported
            kw['stdout'].write(stdout_extra + json.dumps(result).encode() + b'\n')
            kw['stderr'].write(stderr)
            if side_effect:
                side_effect(dest)
            return SimpleNamespace(returncode=0)
        if cmd[:2] == ['docker', 'inspect']:
            return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
        if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/reviewer-calls.log'):
            if reviewer_log is None:
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'Error: No such file or directory')
            Path(cmd[-1]).write_text(reviewer_log)
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if cmd[:2] == ['docker', 'cp'] and '/.fable-review/projects' in cmd[2]:
            if not reviewer_projects:
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'Error: No such file or directory')
            write_tree(cmd[-1], reviewer_projects)
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if cmd[:2] == ['docker', 'cp']:
            if projects_rc:
                return SimpleNamespace(returncode=projects_rc, stdout=b'', stderr=b'copy failed')
            write_tree(cmd[-1], projects or {})
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, env_file=env_file(Path(root).parent), prompt=prompt,
                           resume=resume, kind=kind, dry_run=False, cache_policy='observed-cache', timeout_seconds=timeout,
                           max_turns=None, max_budget_usd=None)
    error = None
    out, err = io.StringIO(), io.StringIO()
    with patch.object(launch.subprocess, 'run', side_effect=fake_run), \
         patch.object(launch, 'volume_listing', return_value={'rc': 0, 'entries': [], 'credentials_file_in_volume': False}), \
         patch.object(launch, 'session_exists', return_value=resume), selftest_patch(selftest), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            launch.turn(args)
        except Exception as exc:              # the caller asserts on it
            error = exc
    turn_dir = sorted((dest / 'turns').iterdir())[-1]
    return json.loads((turn_dir / 'result.json').read_text()), turn_dir, error, out.getvalue() + err.getvalue()


class SameConditionsAcrossArms(unittest.TestCase):
    """Spec 2: 세 조건의 구현 모델(Opus 5.5, effort high)·도구(웹 도구 포함)·자원 한도·상한은 같다."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = make_root(cls.tmp.name)
        cls.manifests = {arm: init_run(cls.root, 'arm%d' % arm, arm) for arm in (1, 2, 3)}
        cls.commands = {arm: launch.docker_command(m, Path('/outside/claude.env'), 'n%d' % arm, resume=False)
                        for arm, m in cls.manifests.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_implementation_model_is_opus_5_5_in_every_arm(self):
        # spec 2: 세 조건의 구현 모델(Opus 5.5)은 같다 (arm 3 implements through its Opus workers)
        for arm in (1, 2):
            cmd = self.commands[arm]
            self.assertEqual(self.manifests[arm]['requested_model'], OPUS)
            self.assertEqual(cmd[cmd.index('--model') + 1], OPUS)
        self.assertEqual(self.manifests[3]['requested_worker_model'], OPUS)
        self.assertEqual(env_values(self.commands[3]).get('CLAUDE_CODE_SUBAGENT_MODEL'), OPUS)

    def test_effort_is_high_for_every_model_call(self):
        # spec 2: 구현 모델(Opus 5.5, effort high) — every arm, and arm 3 workers too
        for arm, cmd in self.commands.items():
            self.assertEqual(cmd[cmd.index('--effort') + 1], 'high', 'arm %d' % arm)
            self.assertEqual(self.manifests[arm]['effort'], 'high')
        self.assertEqual(env_values(self.commands[3]).get('CLAUDE_CODE_SUBAGENT_EFFORT'), 'high')

    def test_web_tools_are_not_withheld_from_any_arm(self):
        # spec 2: 도구(웹 도구 포함)는 같다 / §0 웹 도구 "세 조건 모두 허용하고 기록"
        for arm, cmd in self.commands.items():
            if '--tools' in cmd:
                tools = cmd[cmd.index('--tools') + 1].split(',')
                for web in WEB:
                    self.assertIn(web, tools, 'arm %d tool allow-list lacks %s' % (arm, web))
            for flag in ('--disallowedTools', '--disallowed-tools'):
                if flag in cmd:
                    denied = ' '.join(cmd[cmd.index(flag) + 1:])
                    for web in WEB:
                        self.assertNotIn(web, denied)

    def test_resource_limits_identical_across_arms(self):
        # spec 2: 자원 한도는 같다
        def limits(cmd):
            out = {}
            for arg in cmd[:cmd.index(self.manifests[1]['image'])]:
                for key in ('--cpus', '--memory', '--pids-limit', '--cap-drop', '--security-opt'):
                    if arg.startswith(key + '='):
                        out[key] = arg
            return out
        base = limits(self.commands[1])
        for key in ('--cpus', '--memory', '--pids-limit'):
            self.assertIn(key, base, 'resource limit %s missing' % key)
        self.assertEqual(limits(self.commands[2]), base)
        self.assertEqual(limits(self.commands[3]), base)

    def test_call_caps_are_passed_identically_to_every_arm(self):
        # spec 2: 상한은 같다 — the same per-call caps produce the same CLI cap flags in every arm
        caps = {}
        for arm, m in self.manifests.items():
            cmd = launch.docker_command(m, Path('/e'), 'n', resume=False, max_turns=7, max_budget_usd=2.5)
            caps[arm] = (cmd[cmd.index('--max-turns') + 1], cmd[cmd.index('--max-budget-usd') + 1])
        self.assertEqual(caps[1], caps[2])
        self.assertEqual(caps[1], caps[3])

    def test_only_arm3_has_fable_pm_subagents_and_harness(self):
        # spec 2: 하네스 조건만 PM(Fable 5.2)·서브에이전트·하네스 훅을 갖는다
        self.assertEqual(self.manifests[3]['requested_model'], FABLE)
        self.assertIsNotNone(self.manifests[3]['harness'])
        self.assertTrue(self.manifests[3]['harness_seed'])
        for arm in (1, 2):
            m, cmd = self.manifests[arm], self.commands[arm]
            self.assertNotEqual(m['requested_model'], FABLE)
            self.assertIsNone(m['harness'])
            self.assertFalse(m['harness_seed'])
            self.assertFalse(m['requested_worker_model'])
            self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL', env_values(cmd))
            self.assertIn('--tools', cmd, 'arms 1/2 must restrict the tool surface (no subagent tools)')
            tools = cmd[cmd.index('--tools') + 1].split(',')
            for t in SUBAGENT_SURFACE:
                self.assertNotIn(t, tools)

    def test_harness_cannot_be_attached_to_arms_1_2_and_is_required_for_arm3(self):
        # spec 2: 하네스 조건만 ... 하네스 훅을 갖는다
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            for arm in (1, 2):
                with self.assertRaises(ValueError):
                    init_run(root, 'x%d' % arm, arm, harness=root / 'harness-source')
            with self.assertRaises(ValueError):
                init_run(root, 'x3', 3, harness=None)
            with patch.object(launch, 'seed_harness', return_value='listing') as seeded:
                init_run(root, 'ok1', 1, seed=contextlib.nullcontext())
                init_run(root, 'ok2', 2, seed=contextlib.nullcontext())
                seeded.assert_not_called()
                init_run(root, 'ok3', 3, seed=contextlib.nullcontext())
                seeded.assert_called_once()


class SelftestToolSurface(unittest.TestCase):
    """Runs the selftest shell script locally against a fake `claude` that reports a chosen tool surface."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_root(self.tmp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'claude'
        # Echoes the --tools allow-list as the init tool surface (what the CLI does), with optional drift.
        fake.write_text('#!/usr/bin/env python3\n'
                        'import json, os, sys\n'
                        'a = sys.argv[1:]\n'
                        'tools = a[a.index("--tools") + 1].split(",") if "--tools" in a else os.environ.get("FAKE_DEFAULT", "").split(",")\n'
                        'tools = [t for t in tools if t and t not in os.environ.get("FAKE_DROP", "").split(",")]\n'
                        'tools += [t for t in os.environ.get("FAKE_ADD", "").split(",") if t]\n'
                        'print(json.dumps({"type": "system", "subtype": "init", "tools": tools}))\n'
                        'sys.exit(1)\n')
        os.chmod(fake, 0o755)
        self.ws = self.base / 'ws'
        self.ws.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.ws)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def script_for(self, arm):
        manifest = init_run(self.root, 'st%d' % arm, arm)
        captured = {}

        def fake_helper(m, script, mounts, network_none=True):
            captured['script'] = script
            return 0, 'selftest_failed=0\n', ''
        with patch.object(launch, 'helper', side_effect=fake_helper), patch.object(launch, 'ROOT', self.root):
            launch.selftest(manifest)
        work = self.base / ('t%d' % arm)
        work.mkdir()
        return captured['script'].replace('/tmp', str(work)).replace('/workspace', str(self.ws))

    def run_script(self, script, **env):
        full = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], HOME=str(self.base), **env)
        done = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=full, timeout=120)
        report = {}
        for line in done.stdout.splitlines():
            k, _, v = line.partition('=')
            report.setdefault(k, []).append(v)
        return report

    def test_arms_1_2_selftest_passes_on_allowed_surface_and_fails_on_subagent_or_missing_web(self):
        # spec 2: 하네스 조건만 서브에이전트를 갖는다 + 도구(웹 도구 포함)는 같다 — checked on the observed CLI surface
        for arm in (1, 2):
            script = self.script_for(arm)
            ok = self.run_script(script)
            self.assertEqual(ok.get('selftest_failed'), ['0'], ok)
            observed = ok['cli_tools'][0].split(',')
            for web in WEB:
                self.assertIn(web, observed)
            leak = self.run_script(script, FAKE_ADD='Agent')
            self.assertEqual(leak.get('selftest_failed'), ['1'], leak)
            for missing in WEB:
                nowe = self.run_script(script, FAKE_DROP=missing)
                self.assertEqual(nowe.get('selftest_failed'), ['1'], 'arm %d without %s: %s' % (arm, missing, nowe))

    def test_web_tool_check_is_arm_independent(self):
        # spec 2: 도구(웹 도구 포함)는 같다 — arm 3 (default tool surface) is held to the same web-tool requirement
        script = self.script_for(3)
        marker = 'if [ "$HARNESS" = 0 ]; then'
        self.assertIn(marker, script)
        common = script[:script.index(marker)] + '\nreport selftest_failed "$fail"\n'
        default = 'Bash,Read,Edit,Write,Glob,Grep,Agent,WebSearch,WebFetch'
        self.assertEqual(self.run_script(common, FAKE_DEFAULT=default).get('selftest_failed'), ['0'])
        for missing in WEB:
            report = self.run_script(common, FAKE_DEFAULT=default, FAKE_DROP=missing)
            self.assertEqual(report.get('selftest_failed'), ['1'], report)


class DualReviewArm3(unittest.TestCase):
    """Spec 2: 하네스 조건의 리뷰는 Opus 워커와 Fable 5.2 두 리뷰어다(codex 자리 대체)."""

    def test_arm3_reviewers_are_opus_worker_and_fable_second_reviewer_without_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            m = init_run(root, 'r3', 3)
            self.assertEqual(m['requested_worker_model'], OPUS)
            self.assertEqual(m['requested_reviewer_model'], FABLE)
            cmd = launch.docker_command(m, Path('/outside/claude.env'), 'n', resume=False)
            env = env_values(cmd)
            self.assertEqual(env.get('LAB_REVIEWER_MODEL'), FABLE)
            self.assertNotIn('codex', ' '.join(cmd).lower())
            # the in-container second reviewer entry point really calls the Fable model at effort high
            fake_bin = Path(tmp) / 'bin'
            fake_bin.mkdir()
            (fake_bin / 'claude').write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$FAKE_OUT"\necho \'{"result":"ok"}\'\n')
            os.chmod(fake_bin / 'claude', 0o755)
            argv_file = Path(tmp) / 'argv'
            reviewer_dir = Path(tmp) / 'reviewer'
            done = subprocess.run(['bash', str(HERE / 'fable-review.sh')], input='review', capture_output=True, text=True,
                                  env={'PATH': str(fake_bin) + ':' + os.environ['PATH'], 'HOME': tmp, 'FAKE_OUT': str(argv_file),
                                       'LAB_REVIEWER_MODEL': env['LAB_REVIEWER_MODEL'],
                                       'LAB_REVIEWER_CONFIG_DIR': str(reviewer_dir)})
            self.assertEqual(done.returncode, 0, done.stderr)
            argv = argv_file.read_text().splitlines()
            self.assertEqual(argv[argv.index('--model') + 1], FABLE)
            self.assertEqual(argv[argv.index('--effort') + 1], 'high')

    def test_runner_image_ships_the_fable_reviewer_and_no_codex(self):
        # spec 2: codex 자리 대체 / §0 "코덱스는 그냥 사용하지 말자"
        dockerfile = (HERE / 'Dockerfile').read_text()
        self.assertRegex(dockerfile, r'COPY\s+fable-review\.sh\s+/usr/local/bin/fable-review')
        for line in dockerfile.splitlines():
            if line.startswith('RUN'):
                self.assertNotIn('codex', line.lower())


class RunIsolation(unittest.TestCase):
    """Spec 2: run마다 새 컨테이너·새 세션·새 DB. 다른 run의 코드·실패 이력이 넘어가지 않는다."""

    def test_every_run_has_new_session_volumes_and_a_clean_starter_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            a = init_run(root, 'a', 3)
            # run a produces code and a failure history
            ws_a = Path(a['workspace'])
            (ws_a / 'Main.kt').write_text('code written by run a')
            (ws_a / 'FAILURES.md').write_text('C07 failed')
            b = init_run(root, 'b', 3)
            for key in ('session_id', 'config_volume', 'reviewer_volume', 'workspace'):
                self.assertNotEqual(a[key], b[key], key)
            ws_b = Path(b['workspace'])
            self.assertEqual((ws_b / 'Main.kt').read_text(), 'starter')
            self.assertFalse((ws_b / 'FAILURES.md').exists())
            log = subprocess.run(['git', '-C', str(ws_b), 'log', '--format=%s'], capture_output=True, text=True, check=True)
            self.assertEqual(log.stdout.split('\n')[:-1], ['starter'])        # no history from any other run
            with self.assertRaises(ValueError):
                init_run(root, 'a', 3)                                           # an existing run is never reused

    def test_each_turn_runs_in_a_new_container(self):
        # spec 2: run마다 새 컨테이너
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'c', 1)
            names = []
            orig = launch.docker_command

            def spy(manifest, env, name, *a, **k):
                names.append(name)
                return orig(manifest, env, name, *a, **k)
            line = assistant('r1', OPUS, U1)
            with patch.object(launch, 'docker_command', side_effect=spy):
                drive_turn(root, 'c', reported=U1, projects={'-workspace/s.jsonl': [line]})
            init_run(root, 'd', 1)
            with patch.object(launch, 'docker_command', side_effect=spy):
                drive_turn(root, 'd', reported=U1, projects={'-workspace/s.jsonl': [line]})
            self.assertEqual(len(set(names)), 2)


class UsageAccounting(unittest.TestCase):
    """Spec 2: 사용량 집계는 요청 단위이고 부모·서브에이전트 중복 합산이 없다.
    관측하지 못한 사용량은 0으로 채우지 않고 usage_complete=false로 남긴다."""

    def test_one_event_per_request_even_with_repeated_lines_and_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / 'projects'
            (projects / '-workspace/s/subagents').mkdir(parents=True)
            parent = [assistant('req-pm', OPUS, U1), assistant('req-pm', OPUS, U1),           # one request, two content lines
                      assistant('req-w', OPUS, U2, sidechain=True)]                           # copy of the worker request
            worker = [assistant('req-w', OPUS, U2, sidechain=True), assistant('req-w', OPUS, U2, sidechain=True)]
            (projects / '-workspace/s.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in parent))
            (projects / '-workspace/s/subagents/agent-a1.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in worker))
            events, ids, incomplete, _ = launch.build_events(projects, set(), 'r', '0001', 3)
            self.assertEqual(sorted(e['id'] for e in events), ['req-pm', 'req-w'])
            self.assertEqual(incomplete, [])
            self.assertEqual(launch.sum_usage(events), add(U1, U2))
            roles = {e['id']: e['role'] for e in events}
            self.assertEqual(roles, {'req-pm': 'pm', 'req-w': 'worker'})

    def test_unobserved_usage_is_not_zero_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / 'projects'
            (projects / '-workspace').mkdir(parents=True)
            partial = {k: v for k, v in U2.items() if k != 'cache_read_input_tokens'}
            lines = [assistant('req-ok', OPUS, U1), assistant('req-none', OPUS, None), assistant('req-partial', OPUS, partial)]
            (projects / '-workspace/s.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in lines))
            events, ids, incomplete, _ = launch.build_events(projects, set(), 'r', '0001', 1)
            self.assertEqual([e['id'] for e in events], ['req-ok'])
            self.assertEqual(sorted(i['requestId'] for i in incomplete), ['req-none', 'req-partial'])
            for e in events:
                self.assertEqual(e['usage'], U1)

    def turn_case(self, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'u', kw.pop('arm', 1))
            result, turn_dir, error, _ = drive_turn(root, 'u', **kw)
            events = [json.loads(l) for l in (turn_dir / 'events.jsonl').read_text().splitlines()] \
                if (turn_dir / 'events.jsonl').exists() else []
            return result, events, error

    def test_complete_turn_is_marked_complete(self):
        # control for the cases below (spec 5: run마다 result.json의 usage_complete)
        result, events, error = self.turn_case(reported=U1, projects={'-workspace/s.jsonl': [assistant('r1', OPUS, U1)]})
        self.assertIsNone(error)
        self.assertIs(result['usage_complete'], True)

    def test_unobserved_usage_leaves_usage_complete_false(self):
        good = assistant('r1', OPUS, U1)
        cases = {
            'transcript copy failed': dict(reported=U1, projects_rc=1),
            'request without usage': dict(reported=U1, projects={'-workspace/s.jsonl': [good, assistant('r2', OPUS, None)]}),
            'assistant line without requestId': dict(reported=U1, projects={'-workspace/s.jsonl': [
                good, {'type': 'assistant', 'sessionId': 's', 'message': {'model': OPUS, 'usage': U2}}]}),
            'unparsable transcript line': dict(reported=U1, projects={'-workspace/s.jsonl': [good, '{"type": "assist']}),
            'stream result without usage': dict(reported=None, projects={'-workspace/s.jsonl': [good]}),
            'no transcript at all': dict(reported=U1, projects={}),
            'unparsable stream line': dict(reported=U1, projects={'-workspace/s.jsonl': [good]}, stdout_extra=b'{"broken\n'),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                result, events, _ = self.turn_case(**kw)
                self.assertIs(result['usage_complete'], False, name)
                for e in events:
                    self.assertNotEqual(e['usage'], {k: 0 for k in KEYS}, 'zero-filled event in ' + name)

    def test_parent_and_subagent_usage_are_never_summed_twice(self):
        # arm 3: the worker request appears in the parent transcript and in its own subagent transcript
        projects = {'-workspace/s.jsonl': [assistant('req-pm', FABLE, U1), assistant('req-w', OPUS, U2, sidechain=True)],
                    '-workspace/s/subagents/agent-a1.jsonl': [assistant('req-w', OPUS, U2, sidechain=True)]}
        for reported in (U1, add(U1, U2)):          # CLI total either excludes or includes the sidechain once
            result, events, error = self.turn_case(arm=3, reported=reported, projects=projects)
            self.assertIsNone(error)
            cross = result['usage_cross_check']
            self.assertEqual(cross['main_sum'], U1)
            self.assertEqual(cross['all_sum'], add(U1, U2))
            self.assertIs(result['usage_complete'], True)
            self.assertEqual(sorted(e['id'] for e in events), ['req-pm', 'req-w'])
        # a CLI total that matches neither single-count sum is not accepted as complete
        result, _, _ = self.turn_case(arm=3, reported=add(U1, U2, U2), projects=projects)
        self.assertIs(result['usage_complete'], False)

    def test_requests_are_not_recounted_across_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'm', 1)
            r1, r2 = assistant('r1', OPUS, U1), assistant('r2', OPUS, U2)
            first, _, e1, _ = drive_turn(root, 'm', reported=U1, projects={'-workspace/s.jsonl': [r1]})
            second, _, e2, _ = drive_turn(root, 'm', resume=True, kind='feedback', reported=U2, projects={'-workspace/s.jsonl': [r1, r2]})
            self.assertIsNone(e1)
            self.assertIsNone(e2)
            self.assertEqual(first['request_ids'], ['r1'])
            self.assertEqual(second['request_ids'], ['r2'])
            events, conflicts = launch.reconcile_events(root / '.runs/m')
            self.assertEqual(sorted(e['id'] for e in events), ['r1', 'r2'])
            self.assertEqual(launch.sum_usage(events), add(U1, U2))
            self.assertEqual(conflicts, [])

    def test_arm3_pm_worker_reviewer_usage_separated_and_reviewer_observed(self):
        # spec 5 파일럿: 서브에이전트 사용량이 부모와 중복 없이 분리 / 두 번째 리뷰어(Fable) 사용량이 관측되는지
        sid = 'aaaaaaaa-0000-4000-8000-000000000001'
        projects = {'-workspace/s.jsonl': [assistant('req-pm', FABLE, U1), assistant('req-w', OPUS, U2, sidechain=True)]}
        log = 'T1-1 started session=%s model=%s\nT1-1 exit=0\n' % (sid, FABLE)
        reviewer = {'-mirror/%s.jsonl' % sid: [assistant('req-rv', FABLE, U2, session=sid)]}
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'p', 3)
            result, turn_dir, error, _ = drive_turn(root, 'p', reported=U1, projects=projects,
                                                    reviewer_log=log, reviewer_projects=reviewer)
            self.assertIsNone(error)
            self.assertIs(result['usage_complete'], True)
            ids_by_role = {}
            for role in ('pm', 'worker', 'reviewer'):
                path = turn_dir / ('events.role-%s.jsonl' % role)
                self.assertTrue(path.exists(), role)
                ids_by_role[role] = {json.loads(l)['id'] for l in path.read_text().splitlines()}
            self.assertEqual(ids_by_role, {'pm': {'req-pm'}, 'worker': {'req-w'}, 'reviewer': {'req-rv'}})
            self.assertEqual(result['usage_cross_check']['main_sum'], U1)            # reviewer not folded into PM
            self.assertEqual(result['usage_cross_check']['reviewer_sum'], U2)
            # a reviewer call whose usage left no transcript must not pass as complete
            init_run(root, 'q', 3)
            result2, _, _, _ = drive_turn(root, 'q', reported=U1, projects=projects, reviewer_log=log, reviewer_projects=None)
            self.assertIs(result2['usage_complete'], False)


class CredentialsNeverPersisted(unittest.TestCase):
    """Spec 2: 자격증명 값은 저장소·로그·기록 어디에도 남기지 않는다."""

    def assert_no_token_under(self, directory):
        needle = TOKEN.encode()
        for path in Path(directory).rglob('*'):
            self.assertNotIn(TOKEN, path.name, 'token in file name %s' % path)
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(needle, path.read_bytes(), 'token in %s' % path)

    def test_token_value_is_never_on_the_command_line_or_dry_run_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            for arm in (1, 2, 3):
                m = init_run(root, 'k%d' % arm, arm)
                env = env_file(tmp)
                self.assertEqual(launch.read_token(env), TOKEN)
                self.assertNotIn(TOKEN, ' '.join(launch.docker_command(m, env, 'n', resume=False)))
                args = SimpleNamespace(runs=root / '.runs', run_id='k%d' % arm, env_file=env, prompt=env, resume=False,
                                       dry_run=True, cache_policy=None, timeout_seconds=5, max_turns=None, max_budget_usd=None)
                out = io.StringIO()
                with patch.object(launch.subprocess, 'run') as run, contextlib.redirect_stdout(out):
                    launch.turn(args)
                    run.assert_not_called()
                self.assertNotIn(TOKEN, out.getvalue())

    def test_token_written_anywhere_during_a_turn_is_gone_from_the_run_and_console(self):
        def leak_into_workspace(dest):
            (dest / 'workspace' / 'notes.txt').write_text('token=' + TOKEN)
        sid = 'aaaaaaaa-0000-4000-8000-000000000002'
        projects = {'-workspace/s.jsonl': [assistant('r1', FABLE, U1), {'type': 'user', 'text': 'echo ' + TOKEN}],
                    '-workspace/leak-%s.jsonl' % TOKEN: [assistant('r2', FABLE, U2)]}
        log = 'T1-1 started session=%s model=%s\nT1-1 exit=0\n' % (sid, FABLE)
        reviewer = {'-m/%s.jsonl' % sid: [assistant('rv', FABLE, U2, session=sid), {'type': 'user', 'text': TOKEN}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'leak', 3)
            result, _, error, console = drive_turn(root, 'leak', reported=U1, projects=projects, stderr=TOKEN.encode(),
                                                   stdout_extra=json.dumps({'type': 'system', 'note': TOKEN}).encode() + b'\n',
                                                   reviewer_log=log, reviewer_projects=reviewer, side_effect=leak_into_workspace)
            self.assertIsNotNone(error)                       # a leak never passes silently
            self.assertNotEqual(result['status'], 'ok')
            self.assert_no_token_under(root / '.runs')
            self.assertNotIn(TOKEN, console)
            self.assertNotIn(TOKEN, str(error))

    def test_container_inspect_keeps_no_environment_values(self):
        raw = json.dumps([{'Config': {'Env': [launch.TOKEN_KEY + '=' + TOKEN, 'HOME=/home/node']}}]).encode()
        self.assertNotIn(TOKEN.encode(), launch.redact_inspect(raw))
        self.assertNotIn(TOKEN.encode(), launch.redact_inspect(b'not json ' + TOKEN.encode()))


class ToolSurfaceArm3(unittest.TestCase):
    """Spec 2 (rev. 2026-11-17 저녁): 도구(웹 도구 포함)는 같다 ... 차이는 입력 상세도와 절차뿐이다.
    하네스 조건만 PM(Fable 5.2)·서브에이전트·하네스 훅을 갖는다."""

    def tools(self, manifest):
        cmd = launch.docker_command(manifest, Path('/e'), 'n', resume=False)
        return set(cmd[cmd.index('--tools') + 1].split(',')) if '--tools' in cmd else None

    def test_arm3_tool_surface_is_the_arms_1_2_surface_plus_subagents_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            t = {arm: self.tools(init_run(root, 'tl%d' % arm, arm)) for arm in (1, 2, 3)}
            self.assertEqual(t[1], t[2])
            self.assertIsNotNone(t[3], 'arm 3 tool surface is not enumerated, so "same tools" cannot hold by construction')
            self.assertTrue(t[1] <= t[3], 'arm 3 lacks tools arms 1/2 have: %s' % (t[1] - t[3]))
            extra = t[3] - t[1]
            self.assertTrue(extra, 'arm 3 has no subagent tool')
            self.assertTrue(extra <= set(SUBAGENT_SURFACE), 'arm 3 has non-subagent extras: %s' % (extra - set(SUBAGENT_SURFACE)))


class SelftestArm3Surface(SelftestToolSurface):
    def test_arm3_selftest_accepts_only_base_plus_subagent_surface(self):
        # spec 2: 하네스 조건만 서브에이전트를 갖는다 / 도구는 같다 — checked on the observed CLI surface of arm 3
        script = self.script_for(3)
        marker = '# Workspace must be a readable git repository'
        self.assertIn(marker, script)
        tools_part = script[:script.index(marker)] + '\nreport selftest_failed "$fail"\n'
        ok = self.run_script(tools_part)
        self.assertEqual(ok.get('selftest_failed'), ['0'], ok)
        observed = set(ok['cli_tools'][0].split(','))
        self.assertTrue(observed & set(SUBAGENT_SURFACE))
        for drift in (dict(FAKE_ADD='Skill'), dict(FAKE_ADD='CronCreate'), dict(FAKE_DROP='WebSearch'),
                      dict(FAKE_DROP=','.join(SUBAGENT_SURFACE))):
            with self.subTest(**drift):
                self.assertEqual(self.run_script(tools_part, **drift).get('selftest_failed'), ['1'])

    # the inherited tests are already run by SelftestToolSurface
    test_arms_1_2_selftest_passes_on_allowed_surface_and_fails_on_subagent_or_missing_web = None
    test_web_tool_check_is_arm_independent = None


def sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_turn(dest, i, started, status, events, kind, counts_as, prompt=None):
    """One turn record as the runner leaves it: prompt.txt and invocation.json with turn_kind (+counts_as) and the
    sha256 of the prompt sent (spec 10.5: recovery 턴은 이어 가는 턴과 같은 프롬프트(해시 일치)만 보낸다)."""
    d = dest / 'turns' / ('%04d' % i)
    d.mkdir(parents=True)
    prompt = 'prompt of turn %04d' % i if prompt is None else prompt
    (d / 'prompt.txt').write_text(prompt)
    invocation = {'started_at': started.isoformat(), 'prompt_sha256': sha256_text(prompt)}
    if kind is not None:
        invocation.update(turn_kind=kind, counts_as=counts_as)
    (d / 'invocation.json').write_text(json.dumps(invocation))
    (d / 'result.json').write_text(json.dumps({'status': status, 'usage_complete': True}))
    (d / 'events.jsonl').write_text(''.join(json.dumps({'id': rid, 'role': role, 'usage': u, 'model': OPUS, 'turn': '%04d' % i}) + '\n'
                                            for rid, role, u in events))
    return d


def write_turns(dest, turns):
    """turns: [(started_at, status, [event]) or (started_at, status, [event], kind)].
    Default kind: the first turn is 'initial', every later turn is an evaluator 'feedback' turn.
    A 'recovery' turn counts as the kind of the turn it continues (the previous one) and re-sends its prompt."""
    previous, previous_prompt = None, None
    for i, turn in enumerate(turns, 1):
        started, status, events = turn[:3]
        kind = turn[3] if len(turn) > 3 else ('initial' if i == 1 else 'feedback')
        counts_as = previous if kind == 'recovery' else kind
        prompt = previous_prompt if kind == 'recovery' else None
        d = write_turn(dest, i, started, status, events, kind, counts_as, prompt=prompt)
        previous, previous_prompt = counts_as, (d / 'prompt.txt').read_text()


def load_script(name):
    spec = importlib.util.spec_from_file_location('spec_inv_' + name, HERE.parent / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HERE.parent / 'scripts'))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HERE.parent / 'scripts'))
    return module


consult_contract = load_script('consult_contract')
PROXY_OUTCOMES = ('ok', 'exit-nonzero', 'empty-response', 'credential-leak', 'timeout')


def proxy_readiness(directory):
    path = Path(directory) / 'proxy-readiness.json'
    path.write_text(json.dumps({'protocol': 'payment-settlement-v3-claude', 'user_proxy_model': OPUS,
                                'image_digests': {'runner': 'sha256:frozen'}, 'token_limit': 20_000_000,
                                'time_limit_seconds': 28_800, 'feedback_limit': 30}))
    return path


def proxy_call(runs, run_id, outcome='ok', *, timeout_seconds=None, record=None, usage=U1, request_id='req-proxy',
               served_model=OPUS):
    """One user-proxy consultation through scripts/consult_contract.py (the public interface) with a fake docker.
    outcome: 'ok' | 'exit-nonzero' | 'empty-response' | 'credential-leak' | 'timeout'. record: list of (cmd, timeout).
    Returns (exit code, console). The consult folder it leaves is exactly what a real call leaves."""
    runs = Path(runs)
    env = env_file(runs.parent)
    questions = runs.parent / 'questions.md'
    questions.write_text('질문: 정산 기준 시각은?')
    state = {}

    def fake_run(cmd, **kw):
        if record is not None:
            record.append((list(cmd), kw.get('timeout')))
        if cmd[:2] == ['docker', 'create']:
            state['session'] = cmd[cmd.index('--session-id') + 1]
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if cmd[:2] == ['docker', 'start']:
            if outcome == 'timeout':
                raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
            text = {'empty-response': '', 'credential-leak': 'APPROVED ' + TOKEN}.get(outcome, 'APPROVED')
            kw['stdout'].write(json.dumps({'type': 'result', 'result': text, 'session_id': state['session'],
                                           'usage': usage}).encode() + b'\n')
            return SimpleNamespace(returncode=1 if outcome == 'exit-nonzero' else 0)
        if cmd[:2] == ['docker', 'cp']:
            target = Path(cmd[-1]) / '-tmp'
            target.mkdir(parents=True, exist_ok=True)
            line = {'type': 'assistant', 'requestId': request_id, 'sessionId': state['session'],
                    'message': {'model': served_model, 'usage': usage}}
            (target / (state['session'] + '.jsonl')).write_text(json.dumps(line) + '\n')
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    argv = ['consult_contract.py', '--run-id', run_id, '--questions', str(questions), '--runs', str(runs),
            '--model', OPUS, '--env-file', str(env), '--readiness', str(proxy_readiness(runs.parent))]
    if timeout_seconds is not None:
        argv += ['--timeout-seconds', str(timeout_seconds)]
    console = io.StringIO()
    with patch.object(sys, 'argv', argv), patch.object(consult_contract.subprocess, 'run', side_effect=fake_run), \
         contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
        try:
            code = consult_contract.main()
        except SystemExit as exc:
            code = exc.code
    return code, console.getvalue()


_CONSULT_TEMPLATE = {}


def completed_consult_template():
    """The consults/NNNN folder a real, completed user-proxy call leaves (made once through proxy_call)."""
    if 'path' not in _CONSULT_TEMPLATE:
        tmp = tempfile.mkdtemp(prefix='consult-template-')
        atexit.register(shutil.rmtree, tmp, True)
        runs = Path(tmp) / 'runs'
        (runs / 'tpl' / 'turns').mkdir(parents=True)
        # the manifest pins the run's image (spec 10.5: 대리 사용자 호출은 그 run의 manifest에 고정된 이미지로만)
        (runs / 'tpl' / 'manifest.json').write_text(json.dumps({'run_id': 'tpl', 'arm': 3, 'core': 1, 'image': 'img',
                                                                'session_id': '11111111-2222-4333-8444-555555555555'}))
        code, console = proxy_call(runs, 'tpl')
        folders = sorted((runs / 'tpl' / 'consults').iterdir()) if (runs / 'tpl' / 'consults').exists() else []
        if code != 0 or len(folders) != 1:
            raise AssertionError('a completed user-proxy call could not be produced: exit %r %s' % (code, console))
        _CONSULT_TEMPLATE['path'] = folders[0]
    return _CONSULT_TEMPLATE['path']


def write_consults(dest, consults):
    """Completed user-proxy consultations as a real call leaves them (exit 0, non-empty response, its usage record),
    with the given request events. Each consults/NNNN starts as a copy of a real call's folder."""
    template = completed_consult_template() if consults else None
    for i, events in enumerate(consults, 1):
        d = dest / 'consults' / ('%04d' % i)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template, d)
        (d / 'events.jsonl').write_text(''.join(json.dumps({'id': rid, 'role': role, 'usage': u, 'model': OPUS, 'turn': 'proxy'}) + '\n'
                                                for rid, role, u in events))
        usage = json.loads((d / 'usage.json').read_text())
        usage.update(run_id=dest.name, usage=add(*[u for _, _, u in events]) if events else usage['usage'],
                     request_ids=[rid for rid, _, _ in events])
        (d / 'usage.json').write_text(json.dumps(usage))


def make_capped_run(tmp, turns, consults=()):
    """turns: see write_turns; event = (requestId, role, usage)."""
    dest = Path(tmp) / 'capped'
    (dest / 'turns').mkdir(parents=True)
    write_turns(dest, turns)
    write_consults(dest, consults)
    return dest


def tokens(input_tokens, output_tokens):
    # cache counters are 0 so "input+output" means the same under any reading of cache tokens
    return {'input_tokens': input_tokens, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': output_tokens}


def commit_terminal():
    spec = importlib.util.spec_from_file_location('commit_run_spec', HERE.parent / 'scripts/commit_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TERMINAL


class RunCaps(unittest.TestCase):
    """Spec 2: 상한: run당 input+output 2,000만 토큰 · 8시간 · 피드백 30회. 먼저 닿으면 CENSORED_*로 끝내고 보존한다.
    실패 run을 결과에서 지우지 않는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def gate(self, dest, feedback=False):
        try:
            launch.limit_gate(dest, next_is_feedback=feedback)
            return None
        except RuntimeError as exc:
            return exc

    def censored(self, dest):
        path = dest / 'censored.json'
        return json.loads(path.read_text())['status'] if path.exists() else None

    def test_token_cap_counts_every_role_of_the_run(self):
        # 20,000,000 = pm 10M + worker 6M + reviewer 4M (input+output)
        at_cap = [(self.now, 'ok', [('p', 'pm', tokens(5_000_000, 5_000_000)), ('w', 'worker', tokens(3_000_000, 3_000_000)),
                                    ('r', 'reviewer', tokens(2_000_000, 2_000_000))])]
        below = [(self.now, 'ok', [('p', 'pm', tokens(5_000_000, 4_999_999)), ('w', 'worker', tokens(3_000_000, 3_000_000)),
                                   ('r', 'reviewer', tokens(2_000_000, 2_000_000))])]
        dest = make_capped_run(Path(self.tmp.name) / 'below', below)
        self.assertIsNone(self.gate(dest))
        self.assertIsNone(self.censored(dest))
        dest = make_capped_run(Path(self.tmp.name) / 'at', at_cap)
        self.assertIsNotNone(self.gate(dest))
        status = self.censored(dest)
        self.assertTrue(status and status.startswith('CENSORED_'), status)
        self.assertTrue((dest / 'turns/0001/events.jsonl').exists())          # 보존한다

    def test_time_cap_is_eight_hours_from_the_first_call(self):
        dest = make_capped_run(Path(self.tmp.name) / 'young', [(self.now - timedelta(hours=7, minutes=59), 'ok', [('p', 'pm', tokens(1, 1))])])
        self.assertIsNone(self.gate(dest))
        dest = make_capped_run(Path(self.tmp.name) / 'old', [(self.now - timedelta(hours=8, seconds=1), 'ok', [('p', 'pm', tokens(1, 1))]),
                                                            (self.now - timedelta(minutes=5), 'ok', [('q', 'pm', tokens(1, 1))])])
        self.assertIsNotNone(self.gate(dest))
        self.assertTrue((self.censored(dest) or '').startswith('CENSORED_'))

    def test_feedback_cap_is_thirty_follow_up_prompts(self):
        # §0 추가 결정(2026-11-17): 평가자가 판정 결과를 전하는 후속 턴만 30회에 넣는다
        def run(n_feedback):
            turns = [(self.now, 'ok', [('i', 'implementer', tokens(1, 1))], 'initial')]
            turns += [(self.now, 'ok', [('r%d' % i, 'implementer', tokens(1, 1))], 'feedback') for i in range(n_feedback)]
            return make_capped_run(Path(self.tmp.name) / ('fb%d' % n_feedback), turns)
        thirty_delivered = run(30)                       # initial prompt + 30 feedbacks
        self.assertIsNotNone(self.gate(thirty_delivered, feedback=True), '31st feedback accepted')
        self.assertTrue((self.censored(thirty_delivered) or '').startswith('CENSORED_'))
        self.assertIsNone(self.gate(run(29), feedback=True), '30th feedback refused')

    def test_three_caps_end_in_distinct_committable_censored_statuses(self):
        token = make_capped_run(Path(self.tmp.name) / 'ct', [(self.now, 'ok', [('p', 'pm', tokens(10_000_000, 10_000_000))])])
        time = make_capped_run(Path(self.tmp.name) / 'cm', [(self.now - timedelta(hours=9), 'ok', [('p', 'pm', tokens(1, 1))])])
        feedback = make_capped_run(Path(self.tmp.name) / 'cf', [(self.now, 'ok', [('r%d' % i, 'pm', tokens(1, 1))]) for i in range(31)])
        # ^ initial + 30 evaluator feedback turns
        self.gate(token)
        self.gate(time)
        self.gate(feedback, feedback=True)
        statuses = [self.censored(d) for d in (token, time, feedback)]
        self.assertEqual(len(set(statuses)), 3, statuses)
        for status in statuses:
            self.assertTrue(status.startswith('CENSORED_'))
            self.assertIn(status, commit_terminal())    # a censored run is kept and committed, not dropped

    def test_censored_run_accepts_no_further_call_and_keeps_its_records(self):
        dest = make_capped_run(Path(self.tmp.name) / 'done', [(self.now, 'ok', [('p', 'pm', tokens(10_000_000, 10_000_000))])])
        self.assertIsNotNone(self.gate(dest))
        first = (dest / 'censored.json').read_text()
        before = sorted(str(p.relative_to(dest)) for p in dest.rglob('*') if 'censored' not in p.name and 'ledger' not in p.name)
        self.assertIsNotNone(self.gate(dest))
        self.assertIsNotNone(self.gate(dest, feedback=True))
        self.assertEqual(json.loads((dest / 'censored.json').read_text())['status'], json.loads(first)['status'])
        after = sorted(str(p.relative_to(dest)) for p in dest.rglob('*') if 'censored' not in p.name and 'ledger' not in p.name)
        self.assertEqual(before, after)

    def test_turn_on_a_capped_run_makes_no_paid_call(self):
        root = make_root(self.tmp.name)
        m = init_run(root, 'cap', 1)
        dest = root / '.runs/cap'
        write_turn(dest, 1, self.now, 'ok', [('big', 'implementer', tokens(15_000_000, 5_000_000))], 'initial', 'initial')
        prompt = Path(self.tmp.name) / 'fb.txt'
        prompt.write_text('feedback')
        args = SimpleNamespace(runs=root / '.runs', run_id='cap', env_file=env_file(self.tmp.name), prompt=prompt, resume=True,
                               kind='feedback', dry_run=False, cache_policy='observed-cache', timeout_seconds=5, max_turns=None, max_budget_usd=None)
        with patch.object(launch.subprocess, 'run') as run, patch.object(launch, 'session_exists', return_value=True), \
             patch.object(launch, 'selftest', return_value=SELFTEST_OK), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                launch.turn(args)
            self.assertFalse([c for c in run.call_args_list if c.args and c.args[0][:2] == ['docker', 'create']])
        self.assertTrue(self.censored(dest))
        del m

    def test_a_single_call_cannot_outlast_the_eight_hour_cap(self):
        root = make_root(self.tmp.name)
        init_run(root, 'late', 1)
        dest = root / '.runs/late'
        write_turn(dest, 1, self.now - timedelta(hours=8) + timedelta(seconds=120), 'ok', [('r1', 'implementer', U1)],
                   'initial', 'initial')
        calls = []
        drive_turn(root, 'late', resume=True, kind='feedback', reported=U2, timeout=3600, record=calls,
                   projects={'-workspace/s.jsonl': [assistant('r1', OPUS, U1), assistant('r2', OPUS, U2)]})
        starts = [t for c, t in calls if c[:2] == ['docker', 'start']]
        self.assertEqual(len(starts), 1)
        self.assertLessEqual(starts[0], 120)


def last_prompt(root, run_id):
    """The prompt file of the run's latest turn record (what a conductor re-sends to continue that turn)."""
    turns = sorted((Path(root) / '.runs' / run_id / 'turns').iterdir())
    return (turns[-1] / 'prompt.txt').read_text()


def attempt_turn(root, run_id, *, resume, kind, session=None, selftest=None, prompt_text=None, selftest_report=SELFTEST_OK):
    """Call launch.turn with a docker stub that only records; returns (error, paid calls).
    A paid call is any `docker create`/`docker start` (the model call happens inside the started container).
    session: what the config volume says about the session transcript (default: exists iff resume).
    prompt_text: default 'next prompt'; a recovery re-sends the prompt of the turn it continues (spec 10.5)."""
    if prompt_text is None:
        prompt_text = last_prompt(root, run_id) if kind == 'recovery' else 'next prompt'
    prompt = Path(root).parent / 'attempt.txt'
    prompt.write_text(prompt_text)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ['docker', 'start'] and 'stdout' in kw:
            kw['stdout'].write(b'{}\n')
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'stub')
    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, env_file=env_file(Path(root).parent), prompt=prompt,
                           resume=resume, kind=kind, dry_run=False, cache_policy='observed-cache', timeout_seconds=5,
                           max_turns=None, max_budget_usd=None)
    error = None
    with patch.object(launch.subprocess, 'run', side_effect=fake_run), \
         patch.object(launch, 'volume_listing', return_value={'rc': 0, 'entries': [], 'credentials_file_in_volume': False}), \
         patch.object(launch, 'session_exists', return_value=resume if session is None else session), \
         selftest_patch(selftest, selftest_report), \
         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            launch.turn(args)
        except BaseException as exc:          # SystemExit included: a refusal may exit
            error = exc
    paid = [c for c in calls if c[:2] in (['docker', 'create'], ['docker', 'start'])]
    return error, paid


class FeedbackCapCountsOnlyEvaluatorFeedback(unittest.TestCase):
    """§0 추가 결정(2026-11-17) 피드백 30회 셈법 → "판정 피드백만 셈": 평가자가 판정 결과를 전하는 후속 턴만 30회에 넣는다.
    하네스 조건의 질의응답 턴(대리 사용자의 답을 PM에게 전하는 턴)은 상한에 넣지 않고 횟수를 따로 기록한다.
    근거: experiment-plan.md:51 "외부 피드백 30회 ... CENSORED_BUDGET/TIME/TURNS", :25 "질문 횟수·답변 길이를 별도 기록"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def turns(self, *kinds, status='ok'):
        """Turn tuples in order; each kind may be 'kind' or ('kind', status)."""
        out = []
        for i, k in enumerate(kinds):
            kind, st = (k, status) if isinstance(k, str) else k
            out.append((self.now, st, [('q%d' % i, 'pm', tokens(1, 1))], kind))
        return out

    def run_dir(self, name, turns, consults=0):
        return make_capped_run(self.base / name, turns, consults=[[('c%d' % i, 'user-proxy', tokens(1, 1))] for i in range(consults)])

    def gate(self, dest, feedback):
        try:
            launch.limit_gate(dest, next_is_feedback=feedback)
            return None
        except RuntimeError as exc:
            return exc

    def censored(self, dest):
        path = dest / 'censored.json'
        return json.loads(path.read_text())['status'] if path.exists() else None

    def live_run(self, run_id, arm, kinds, consults=0):
        """A runner-initialized run with turn records already on disk (as earlier turns would leave them)."""
        root = self.base / ('root-' + run_id)
        root.mkdir()
        root = make_root(root)
        init_run(root, run_id, arm)
        dest = root / '.runs' / run_id
        write_turns(dest, self.turns(*kinds))
        write_consults(dest, [[('c%d' % i, 'user-proxy', tokens(1, 1))] for i in range(consults)])
        return root, dest

    # --- the cap itself -------------------------------------------------------------------------------

    def test_thirty_feedback_turns_are_allowed_and_the_31st_ends_censored_turns(self):
        # experiment-plan.md:51 외부 피드백 30회 ... 먼저 도달하면 CENSORED_BUDGET/TIME/TURNS
        for n in range(0, 30):
            dest = self.run_dir('ok%d' % n, self.turns('initial', *['feedback'] * n))
            self.assertIsNone(self.gate(dest, True), 'feedback no. %d refused' % (n + 1))
            self.assertIsNone(self.censored(dest))
        dest = self.run_dir('full', self.turns('initial', *['feedback'] * 30))
        self.assertIsNotNone(self.gate(dest, True), '31st feedback accepted')
        self.assertEqual(self.censored(dest), 'CENSORED_TURNS')
        self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 30)

    def test_answer_turns_never_consume_the_feedback_cap(self):
        # 질의응답 턴은 상한에 넣지 않고 — however many there are, before or between feedbacks
        for n_answers in (1, 31, 120):
            kinds = ['initial'] + ['answer'] * n_answers + ['feedback'] * 29
            dest = self.run_dir('a%d' % n_answers, self.turns(*kinds), consults=n_answers)
            self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 29, n_answers)
            self.assertIsNone(self.gate(dest, True), '30th feedback refused after %d answer turns' % n_answers)
            self.assertIsNone(self.censored(dest))
        interleaved = ['initial'] + ['answer', 'feedback'] * 29 + ['answer'] * 5
        dest = self.run_dir('inter', self.turns(*interleaved), consults=34)
        self.assertIsNone(self.gate(dest, True))
        self.assertIsNone(self.gate(dest, False))

    def test_answer_turn_after_thirty_feedbacks_is_not_capped(self):
        # 질의응답 턴은 상한에 넣지 않고: reaching 30 feedbacks blocks only the 31st feedback, not an answer delivery
        dest = self.run_dir('after30', self.turns('initial', *['feedback'] * 30), consults=1)
        self.assertIsNone(self.gate(dest, False), 'answer turn refused by the feedback cap')
        self.assertIsNone(self.censored(dest))

    def test_answer_turn_count_is_recorded_separately_and_reproducible_from_run_files(self):
        # 횟수를 따로 기록한다 (experiment-plan.md:25 질문 횟수 ... 별도 기록)
        kinds = ['initial', 'answer', 'answer', 'feedback', 'answer', 'feedback', 'feedback', 'answer']
        dest = self.run_dir('rec', self.turns(*kinds), consults=4)
        state = launch.limit_state(dest)
        self.assertEqual(state['answer_turns'], 4)
        self.assertEqual(state['used']['feedback_limit'], 3)
        ledger = launch.run_ledger(dest)
        self.assertEqual(ledger['turn_kinds']['delivered']['answer'], 4)
        self.assertEqual(ledger['turn_kinds']['delivered']['feedback'], 3)
        # recomputed from the turn records alone: a stale or missing ledger does not change the counts
        for derived in ('ledger.json', 'censored.json'):
            (dest / derived).unlink(missing_ok=True)
        again = launch.limit_state(dest)
        self.assertEqual((again['answer_turns'], again['used']['feedback_limit']), (4, 3))
        # the count follows the files: one more answer record on disk is one more answer
        write_turn(dest, len(kinds) + 1, self.now, 'ok', [('extra', 'pm', tokens(1, 1))], 'answer', 'answer')
        self.assertEqual(launch.limit_state(dest)['answer_turns'], 5)
        self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 3)

    def test_recovered_feedback_counts_once_and_cannot_slip_past_the_cap(self):
        # 판정 결과를 전하는 후속 턴만 센다: one judging result delivered through a failed turn + its recovery is one feedback
        base = ['initial'] + ['feedback'] * 29 + [('feedback', 'failed'), 'recovery']
        dest = self.run_dir('rec30', self.turns(*base))
        self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 30)
        self.assertIsNotNone(self.gate(dest, True), 'feedback after a recovered 30th feedback accepted')
        self.assertEqual(self.censored(dest), 'CENSORED_TURNS')
        dest = self.run_dir('rec29', self.turns('initial', *['feedback'] * 28, ('feedback', 'failed'), 'recovery'))
        self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 29)
        self.assertIsNone(self.gate(dest, True))

    def test_recovered_answer_stays_outside_the_cap(self):
        # recovery counts as the kind it continues: an answer recovered is still an answer, not a feedback
        dest = self.run_dir('ransw', self.turns('initial', *['feedback'] * 29, ('answer', 'failed'), 'recovery'), consults=1)
        state = launch.limit_state(dest)
        self.assertEqual(state['used']['feedback_limit'], 29)
        self.assertEqual(state['answer_turns'], 1)
        self.assertIsNone(self.gate(dest, True))

    def test_turn_record_without_a_known_kind_is_not_skipped_by_the_count(self):
        # a record the count cannot classify might be a feedback; skipping it would let a feedback escape the 30 cap
        for name, invocation in {'no turn_kind': {}, 'unknown turn_kind': {'turn_kind': 'chat', 'counts_as': 'chat'}}.items():
            with self.subTest(name):
                dest = self.run_dir('unk-' + name.replace(' ', ''), self.turns('initial', *['feedback'] * 3))
                d = write_turn(dest, 5, self.now, 'ok', [('u', 'pm', tokens(1, 1))], None, None)
                inv = json.loads((d / 'invocation.json').read_text())
                inv.update(invocation)
                (d / 'invocation.json').write_text(json.dumps(inv))
                with self.assertRaises(Exception):
                    launch.limit_gate(dest, next_is_feedback=False)
                with self.assertRaises(Exception):
                    launch.limit_gate(dest, next_is_feedback=True)

    # --- declarations through the public turn interface ------------------------------------------------

    def test_control_legitimate_declarations_do_reach_the_paid_call(self):
        # control for the refusal tests below: with this stub a permitted turn does reach docker create
        cases = [(1, ['initial'], 0, 'feedback'), (3, ['initial'], 1, 'answer'),
                 (1, ['initial', ('feedback', 'failed')], 0, 'recovery'), (3, ['initial', *['feedback'] * 30], 1, 'answer')]
        for i, (arm, kinds, consults, kind) in enumerate(cases):
            with self.subTest(arm=arm, kind=kind, turns=len(kinds)):
                root, _ = self.live_run('ctl%d' % i, arm, kinds, consults=consults)
                _, paid = attempt_turn(root, 'ctl%d' % i, resume=True, kind=kind)
                self.assertTrue(paid, 'a permitted %s turn never reached the call' % kind)

    def test_follow_up_turn_without_a_declared_kind_makes_no_paid_call(self):
        for arm in (1, 2, 3):
            with self.subTest(arm=arm):
                root, _ = self.live_run('nokind%d' % arm, arm, ['initial'], consults=1 if arm == 3 else 0)
                error, paid = attempt_turn(root, 'nokind%d' % arm, resume=True, kind=None)
                self.assertIsNotNone(error)
                self.assertEqual(paid, [])

    def test_cli_requires_a_kind_on_resume_and_offers_no_way_to_declare_initial(self):
        # the only follow-up declarations are feedback / answer / recovery; a resumed turn cannot pose as the initial prompt
        parser_help = subprocess.run(['python3', str(HERE / 'launch.py'), 'turn', '--help'], capture_output=True, text=True)
        self.assertEqual(parser_help.returncode, 0)
        with tempfile.TemporaryDirectory() as tmp:
            env = env_file(tmp)
            prompt = Path(tmp) / 'p.txt'
            prompt.write_text('x')
            done = subprocess.run(['python3', str(HERE / 'launch.py'), '--runs', str(Path(tmp) / 'runs'), 'turn',
                                   '--run-id', 'claude-arm1-core01', '--prompt', str(prompt), '--env-file', str(env),
                                   '--resume', '--kind', 'initial', '--dry-run'], capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('initial', done.stderr)          # argparse rejects the choice before anything runs

    def test_first_turn_takes_no_kind(self):
        # the first turn is recorded as initial; declaring a kind on it is not a way to start counting differently
        root, _ = self.live_run('first', 1, [])
        error, paid = attempt_turn(root, 'first', resume=False, kind='answer', session=False)
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])

    def test_answer_turns_are_impossible_in_arms_1_and_2(self):
        # 하네스 조건의 질의응답 턴 — arms 1 and 2 have no user proxy, so nothing can be delivered as an answer there
        for arm in (1, 2):
            with self.subTest(arm=arm):
                root, _ = self.live_run('ans%d' % arm, arm, ['initial'], consults=1)   # even with a consult record on disk
                error, paid = attempt_turn(root, 'ans%d' % arm, resume=True, kind='answer')
                self.assertIsNotNone(error)
                self.assertEqual(paid, [])

    def test_answer_turn_in_arm3_is_accepted_and_recorded_as_answer_outside_the_cap(self):
        root, dest = self.live_run('ans3', 3, ['initial', *['feedback'] * 30], consults=1)
        line = assistant('a1', FABLE, U1)
        result, turn_dir, error, _ = drive_turn(root, 'ans3', resume=True, kind='answer', reported=U1,
                                                projects={'-workspace/s.jsonl': [line]})
        self.assertIsNone(error)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(json.loads((turn_dir / 'invocation.json').read_text())['turn_kind'], 'answer')
        self.assertIsNone(self.censored(dest))
        state = launch.limit_state(dest)
        self.assertEqual(state['answer_turns'], 1)
        self.assertEqual(state['used']['feedback_limit'], 30)
        # the next evaluator feedback is the 31st: no paid call, CENSORED_TURNS
        error, paid = attempt_turn(root, 'ans3', resume=True, kind='feedback')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        self.assertEqual(self.censored(dest), 'CENSORED_TURNS')

    def test_feedback_cannot_escape_the_cap_by_being_declared_as_an_answer(self):
        # after 30 feedbacks a conductor might declare the 31st feedback as an answer; without an undelivered proxy
        # answer there is nothing to deliver as an answer
        cases = {'no consultation at all': 0, 'every consultation already delivered': 2}
        for name, consults in cases.items():
            with self.subTest(name):
                kinds = ['initial'] + ['answer'] * consults + ['feedback'] * 30
                root, dest = self.live_run('esc%d' % consults, 3, kinds, consults=consults)
                error, paid = attempt_turn(root, 'esc%d' % consults, resume=True, kind='answer')
                self.assertIsNotNone(error, name)
                self.assertEqual(paid, [], name)
                self.assertEqual(launch.limit_state(dest)['answer_turns'], consults)

    def test_one_consultation_backs_exactly_one_answer_turn(self):
        root, dest = self.live_run('once', 3, ['initial', 'answer'], consults=1)
        error, paid = attempt_turn(root, 'once', resume=True, kind='answer')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])

    def test_feedback_cannot_escape_the_cap_by_being_declared_as_a_recovery(self):
        # recovery is only for continuing a turn that did not end ok, and counts as that turn's kind
        for arm in (1, 3):
            with self.subTest('recovery after an ok feedback', arm=arm):
                root, _ = self.live_run('rc%d' % arm, arm, ['initial', *['feedback'] * 30])
                error, paid = attempt_turn(root, 'rc%d' % arm, resume=True, kind='recovery')
                self.assertIsNotNone(error)
                self.assertEqual(paid, [])
            with self.subTest('recovery after an ok answer', arm=arm):
                if arm != 3:
                    continue
                root, _ = self.live_run('rca', 3, ['initial', *['feedback'] * 30, 'answer'], consults=1)
                error, paid = attempt_turn(root, 'rca', resume=True, kind='recovery')
                self.assertIsNotNone(error)
                self.assertEqual(paid, [])

    def test_recovery_of_a_failed_feedback_is_capped_like_a_feedback(self):
        # 30 feedbacks delivered, then a failed feedback record: its recovery would be a 31st delivery
        root = make_root(self.base / 'rf')
        init_run(root, 'rf', 1)
        dest = root / '.runs/rf'
        write_turns(dest, self.turns('initial', *['feedback'] * 30, ('feedback', 'failed')))
        error, paid = attempt_turn(root, 'rf', resume=True, kind='recovery')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        self.assertEqual(self.censored(dest), 'CENSORED_TURNS')


class RoleSeparatedLedger(unittest.TestCase):
    """Spec 2 (the user-proxy part only): 대리 사용자의 토큰은 역할별로 분리 기록한다 / 중복 합산이 없다.
    The conductor and judging-adapter parts of the same sentence are checked in EvaluationCostIsSeparate."""

    def test_user_proxy_tokens_are_a_separate_role_in_the_run_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            dest = make_capped_run(tmp, [(now, 'ok', [('p', 'pm', U1), ('w', 'worker', U2)])],
                                   consults=[[('x', 'user-proxy', tokens(7, 3))]])
            ledger = launch.run_ledger(dest)
            roles = ledger['roles']
            self.assertEqual(set(roles), {'pm', 'worker', 'user-proxy'})
            self.assertEqual(roles['pm']['usage'], U1)
            self.assertEqual(roles['worker']['usage'], U2)
            self.assertEqual(roles['user-proxy']['usage'], tokens(7, 3))
            self.assertEqual(sum(r['total_tokens'] for r in roles.values()), ledger['total_tokens'])


def new_run(base, run_id, arm, turns=(), started=None, consults=0):
    """A runner-initialized run with turn records on disk. turns: kinds, or (kind, status) pairs.
    consults: number of completed user-proxy consultations (real-call records, see write_consults)."""
    root = make_root(Path(base) / run_id)
    init_run(root, run_id, arm)
    dest = root / '.runs' / run_id
    started = started or datetime.now(timezone.utc)
    role = 'pm' if arm == 3 else 'implementer'
    records = []
    for i, k in enumerate(turns):
        kind, status = (k, 'ok') if isinstance(k, str) else k
        records.append((started, status, [('t%d' % i, role, tokens(1, 1))], kind))
    write_turns(dest, records)
    write_consults(dest, [[('c%d' % i, 'user-proxy', tokens(1, 1))] for i in range(consults)])
    return root, dest


def paid_calls(record):
    return [c for c, _ in record if c[:2] in (['docker', 'create'], ['docker', 'start'])]


def numbers_in(value):
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers_in(v)]
    if isinstance(value, list):
        return [n for v in value for n in numbers_in(v)]
    return [value] if isinstance(value, (int, float)) and not isinstance(value, bool) else []


def age_first_turn(dest, started):
    """Wall-clock time passes: the run's first call now lies at `started`."""
    inv = dest / 'turns' / '0001' / 'invocation.json'
    data = json.loads(inv.read_text())
    data['started_at'] = started.isoformat()
    inv.write_text(json.dumps(data))


class FailedFeedbackIsNotSkipped(unittest.TestCase):
    """§0 피드백 30회 셈법 "판정 피드백만 셈" + spec 2 상한 피드백 30회: a judging result that was delivered through a turn
    that then failed may have reached the model, so it cannot drop out of the count by sending a fresh feedback instead
    (runbook: 앞 턴이 ok가 아니면 재개 턴은 recovery만 허용)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_feedback_after_a_failed_feedback_makes_no_paid_call(self):
        for arm in (1, 3):
            for prior in (0, 29):
                with self.subTest(arm=arm, ok_feedbacks_before=prior):
                    run_id = 'ff%d-%d' % (arm, prior)
                    root, dest = new_run(self.base, run_id, arm, ['initial', *['feedback'] * prior, ('feedback', 'failed')])
                    error, paid = attempt_turn(root, run_id, resume=True, kind='feedback')
                    self.assertIsNotNone(error)
                    self.assertEqual(paid, [])

    def test_failed_feedback_is_continued_by_a_recovery_that_counts_as_that_feedback(self):
        # control: the failed delivery is continued, and it is the one feedback
        root, dest = new_run(self.base, 'fr', 1, ['initial', *['feedback'] * 29, ('feedback', 'failed')])
        error, paid = attempt_turn(root, 'fr', resume=True, kind='recovery')
        self.assertTrue(paid, 'recovery of a failed feedback never reached the call')
        # once that recovery is delivered the count is 30, and a further feedback is the 31st
        root, dest = new_run(self.base, 'fr2', 1, ['initial', *['feedback'] * 29, ('feedback', 'failed'), 'recovery'])
        self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 30)
        error, paid = attempt_turn(root, 'fr2', resume=True, kind='feedback')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])

    def test_answer_turn_cannot_replace_the_recovery_of_a_failed_feedback(self):
        # arm 3 with an undelivered completed consultation: the failed feedback must still be continued first
        root, dest = new_run(self.base, 'fa', 3, ['initial', ('feedback', 'failed')], consults=1)
        error, paid = attempt_turn(root, 'fa', resume=True, kind='answer')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])

    def test_failed_recovery_still_allows_only_a_recovery(self):
        root, dest = new_run(self.base, 'frr', 1, ['initial', ('feedback', 'failed'), ('recovery', 'failed')])
        error, paid = attempt_turn(root, 'frr', resume=True, kind='feedback')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        root, dest = new_run(self.base, 'frr2', 1, ['initial', ('feedback', 'failed'), ('recovery', 'failed')])
        _, paid = attempt_turn(root, 'frr2', resume=True, kind='recovery')
        self.assertTrue(paid)


class AnswerNeedsCompletedConsultation(unittest.TestCase):
    """§0 피드백 30회 셈법: 질의응답 턴 = 대리 사용자의 답을 PM에게 전하는 턴 (상한 밖, 따로 기록). An answer turn therefore
    needs a consultation that actually produced an answer: a failed or empty consultation permits none, and one
    consultation permits one answer turn. Consultations are made through scripts/consult_contract.py with a fake docker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def deliver_answer(self, root, run_id):
        result, turn_dir, error, _ = drive_turn(root, run_id, resume=True, kind='answer', reported=U1,
                                                projects={'-workspace/s.jsonl': [assistant('ans-' + turn_dir_hint(root, run_id), FABLE, U1)]})
        self.assertIsNone(error)
        self.assertEqual(result['status'], 'ok')

    def test_failed_or_empty_consultations_permit_no_answer_turn(self):
        for outcome in PROXY_OUTCOMES[1:]:
            with self.subTest(outcome):
                run_id = 'bad-' + outcome
                root, dest = new_run(self.base, run_id, 3, ['initial'])
                code, console = proxy_call(root / '.runs', run_id, outcome)
                self.assertNotEqual(code, 0, console)                          # the failure is visible to the conductor
                error, paid = attempt_turn(root, run_id, resume=True, kind='answer')
                self.assertIsNotNone(error, outcome)
                self.assertEqual(paid, [], outcome)
                self.assertTrue(launch.run_ledger(dest)['consults_not_completed'], outcome)

    def test_several_failed_consultations_still_permit_no_answer_turn(self):
        root, dest = new_run(self.base, 'many', 3, ['initial'])
        for outcome in PROXY_OUTCOMES[1:]:
            proxy_call(root / '.runs', 'many', outcome)
        error, paid = attempt_turn(root, 'many', resume=True, kind='answer')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])

    def test_one_completed_consultation_permits_exactly_one_answer_turn(self):
        root, dest = new_run(self.base, 'one', 3, ['initial'])
        code, console = proxy_call(root / '.runs', 'one', 'ok')
        self.assertEqual(code, 0, console)
        _, paid = attempt_turn(root, 'one', resume=True, kind='answer')
        self.assertTrue(paid, 'a completed consultation did not permit its answer turn')     # control

        root, dest = new_run(self.base, 'two', 3, ['initial'])
        self.assertEqual(proxy_call(root / '.runs', 'two', 'ok')[0], 0)
        self.deliver_answer(root, 'two')
        error, paid = attempt_turn(root, 'two', resume=True, kind='answer')
        self.assertIsNotNone(error, 'one consultation permitted a second answer turn')
        self.assertEqual(paid, [])
        proxy_call(root / '.runs', 'two', 'empty-response')                  # a failed one adds nothing
        error, paid = attempt_turn(root, 'two', resume=True, kind='answer')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        self.assertEqual(proxy_call(root / '.runs', 'two', 'ok')[0], 0)     # a second completed one permits a second
        self.deliver_answer(root, 'two')
        self.assertEqual(launch.limit_state(dest)['answer_turns'], 2)

    def test_consult_folder_without_the_calls_completion_record_permits_no_answer(self):
        # a folder holding a response but no record that the call ran and ended ok is not "a consultation that answered"
        root, dest = new_run(self.base, 'hand', 3, ['initial'])
        d = dest / 'consults' / '0001'
        d.mkdir(parents=True)
        (d / 'response.txt').write_text('답변')
        (d / 'usage.json').write_text(json.dumps({'role': 'user-proxy', 'usage_complete': True}))
        error, paid = attempt_turn(root, 'hand', resume=True, kind='answer')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])


def turn_dir_hint(root, run_id):
    turns = Path(root) / '.runs' / run_id / 'turns'
    return str(len(list(turns.iterdir())) if turns.exists() else 0)


def init_frozen(root, run_id, arm, readiness, image=None):
    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, arm=arm, core=1, image=image,
                           harness=root / 'harness-source' if arm == 3 else None, network=None, readiness=readiness)
    with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
         patch.object(launch, 'selftest', return_value=SELFTEST_OK), patch.object(launch, 'seed_harness', return_value='listing'), \
         contextlib.redirect_stdout(io.StringIO()):
        launch.initialize(args)
    return json.loads((root / '.runs' / run_id / 'manifest.json').read_text())


FROZEN_CONTRACT = 'c' * 64
FROZEN_IMAGE = 'sha256:' + 'f' * 64


def write_readiness(directory, contract=FROZEN_CONTRACT, image=FROZEN_IMAGE):
    path = Path(directory) / ('readiness-%s-%s.json' % (contract[:4], image[-4:]))
    path.write_text(json.dumps({'protocol': 'payment-settlement-v3-claude', 'contract_sha256': contract,
                                'image_digests': {'runner': image}, 'user_proxy_model': OPUS}))
    return path


class NothingFrozenChangesAtInit(unittest.TestCase):
    """Spec 2: 판정 기준과 요구사항은 동결 후 바꾸지 않는다. 바꾸면 프로토콜 버전을 올리고 전 조건 재실행이다.
    spec 5: 동결 해시 기록 — a run initialised after the freeze runs the frozen image and records the frozen contract hash."""

    def test_run_after_freeze_uses_the_frozen_image_and_records_the_contract_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            readiness = write_readiness(tmp)
            for arm, image in ((1, None), (2, FROZEN_IMAGE), (3, None)):
                with self.subTest(arm=arm, image=image):
                    m = init_frozen(root, 'fz%d' % arm, arm, readiness, image=image)
                    self.assertEqual(m['image'], FROZEN_IMAGE)
                    self.assertIs(m['image_frozen'], True)
                    self.assertEqual(m['contract_sha256'], FROZEN_CONTRACT)

    def test_run_after_freeze_cannot_use_another_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            readiness = write_readiness(tmp)
            for other in ('payment-claude-runner:2.1.326', 'sha256:' + '0' * 64, 'img'):
                with self.subTest(other):
                    with self.assertRaises(Exception):
                        init_frozen(root, 'other', 1, readiness, image=other)
                    self.assertFalse((root / '.runs' / 'other' / 'manifest.json').exists())


class PilotEvidenceNeedsTheFrozenContractAndImage(unittest.TestCase):
    """Spec 2: 동결 후 바꾸지 않는다 / §0 보고 단위 + task 07: the pilot that frees the cycles must have run under the freeze —
    pilot evidence from a pilot made under another contract hash or another image does not count."""

    @classmethod
    def setUpClass(cls):
        cls.freeze = load_script('freeze_readiness')

    def pilot(self, root, run_id, readiness=None):
        if readiness is None:
            init_run(root, run_id, 3)
        else:
            init_frozen(root, run_id, 3, readiness)
        sid = 'aaaaaaaa-0000-4000-8000-0000000000%02d' % len(run_id)
        projects = {'-workspace/s.jsonl': [assistant('pm-' + run_id, FABLE, U1), assistant('w-' + run_id, OPUS, U2, sidechain=True)],
                    '-workspace/s/subagents/agent-a1.jsonl': [assistant('w-' + run_id, OPUS, U2, sidechain=True)]}
        log = 'T1-1 started session=%s model=%s\nT1-1 exit=0\n' % (sid, FABLE)
        reviewer = {'-mirror/%s.jsonl' % sid: [assistant('rv-' + run_id, FABLE, U2, session=sid)]}
        result, _, error, _ = drive_turn(root, run_id, reported=U1, projects=projects, reviewer_log=log, reviewer_projects=reviewer)
        self.assertIsNone(error)
        self.assertEqual(result['status'], 'ok')

    def passed(self, root, run_id, contract, image):
        return bool(self.freeze.pilot_evidence(root / '.runs', run_id, contract, image)[0])

    def test_pilot_counts_only_under_the_same_contract_and_frozen_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            self.pilot(root, 'pilot', write_readiness(tmp))
            self.assertTrue(self.passed(root, 'pilot', FROZEN_CONTRACT, FROZEN_IMAGE), 'control: the frozen pilot does not count')
            self.assertFalse(self.passed(root, 'pilot', 'd' * 64, FROZEN_IMAGE), 'pilot under another contract counted')
            self.assertFalse(self.passed(root, 'pilot', FROZEN_CONTRACT, 'sha256:' + '0' * 64), 'pilot on another image counted')

    def test_pilot_made_before_the_freeze_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            self.pilot(root, 'early')                                       # initialised without readiness, image 'img'
            self.assertFalse(self.passed(root, 'early', FROZEN_CONTRACT, 'img'))
            self.assertFalse(self.passed(root, 'early', FROZEN_CONTRACT, FROZEN_IMAGE))


REAL_EVALUATION_LEDGER = HERE.parent / 'docs' / 'results' / 'evaluation-cost.jsonl'


def tree_state(directory):
    """Every file under `directory` with its bytes (to show a step wrote nothing there)."""
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(Path(directory).rglob('*')) if p.is_file()}


class EvaluationCostIsSeparate(unittest.TestCase):
    """Spec 10.4 (사용자 결정 2026-11-17): 진행자 비용은 실험 전체 단위의 평가 비용 장부에 기록하고 조건별 비용과 run 상한에
    넣지 않는다. 장부: claude/docs/results/evaluation-cost.jsonl, 한 줄 = 진행자 세션 1개 또는 어댑터 작성 1건. 칸: 역할, 세션
    식별자, 모델, 토큰(네 카운터 또는 합계), 숫자의 출처, 맡은 run id 목록, 시각. 덧붙이기만 한다. run 장부의 evaluation_cost
    블록과 run 단위 기록은 없앤다. 누락 점검: 모든 run id가 어느 한 줄의 run 목록에 들어 있어야 하고, 빠진 run은 드러난다.
    Spec 2: 관측하지 못한 사용량은 0으로 채우지 않는다. Spec 10.5 (U6): 비용 인자는 0 이상의 유한수만 받는다.
    Every call points --ledger at a temporary file; the tracked ledger is checked untouched after the class."""

    @classmethod
    def setUpClass(cls):
        cls.recorder = load_script('record_evaluation_cost')
        cls.real_before = REAL_EVALUATION_LEDGER.read_bytes() if REAL_EVALUATION_LEDGER.exists() else None

    @classmethod
    def tearDownClass(cls):
        after = REAL_EVALUATION_LEDGER.read_bytes() if REAL_EVALUATION_LEDGER.exists() else None
        if after != cls.real_before:
            raise AssertionError('the tracked evaluation-cost ledger was written by a test')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.dest = new_run(self.tmp.name, 'ev', 1, [])
        big = tokens(5_000_000, 5_000_000)
        result, _, error, _ = drive_turn(self.root, 'ev', reported=big, projects={'-workspace/s.jsonl': [assistant('run-req', OPUS, big)]})
        self.assertIsNone(error)
        self.ledger = Path(self.tmp.name) / 'results' / 'evaluation-cost.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, role, *counters, total=None, cost=None, session='conductor-session-1', run_ids=('ev',), adapter=False,
               model=OPUS, source='agent completion notice usage', item=None):
        argv = ['record_evaluation_cost.py', '--ledger', str(self.ledger), '--role', role, '--session', session,
                '--model', model, '--source', source]
        if item is not None:
            argv += ['--item', item]
        for flag, value in zip(('--input-tokens', '--cache-creation-input-tokens', '--cache-read-input-tokens',
                                '--output-tokens'), counters):
            argv += [flag, str(value)]
        if total is not None:
            argv += ['--total-tokens', str(total)]
        if cost is not None:
            argv += ['--cost-usd', str(cost)]
        for run_id in run_ids:
            argv += ['--run-id', run_id]
        if adapter:
            argv.append('--includes-judging-adapter')
        out = io.StringIO()
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = self.recorder.main()
            except SystemExit as exc:
                code = exc.code
            except Exception as exc:                 # a refusal raised instead of exiting is still a refusal
                code, _ = 1, out.write(repr(exc))
        return code or 0, out.getvalue()

    def lines(self):
        return [json.loads(l) for l in self.ledger.read_text().splitlines()] if self.ledger.exists() else []

    def ledger_bytes(self):
        return self.ledger.read_bytes() if self.ledger.exists() else None

    def test_default_ledger_is_the_experiment_level_file(self):
        # 장부: claude/docs/results/evaluation-cost.jsonl (experiment level, not under a run)
        self.assertEqual(Path(self.recorder.default_ledger()).resolve(), REAL_EVALUATION_LEDGER.resolve())

    def test_one_line_carries_role_session_model_tokens_source_runs_and_time(self):
        code, out = self.record('conductor', 10_000_000, 5_000_000, 5_000_000, 1_000_000, cost=7.5,
                                run_ids=('claude-arm1-core01', 'claude-arm2-core01'))
        self.assertEqual(code, 0, out)
        [line] = self.lines()
        self.assertEqual(line['role'], 'conductor')
        self.assertEqual(line['session'], 'conductor-session-1')
        self.assertEqual(line['model'], OPUS)
        self.assertEqual(line['usage'], {'input_tokens': 10_000_000, 'cache_creation_input_tokens': 5_000_000,
                                         'cache_read_input_tokens': 5_000_000, 'output_tokens': 1_000_000})
        self.assertEqual(line['total_tokens'], 21_000_000)
        self.assertIn('agent completion notice usage', json.dumps(line, ensure_ascii=False))     # 숫자의 출처
        self.assertEqual(sorted(line['run_ids']), ['claude-arm1-core01', 'claude-arm2-core01'])   # 맡은 run id 목록
        self.assertTrue(line.get('recorded_at'))                                                  # 시각

    def test_evaluation_cost_never_enters_a_run_ledger_or_the_run_cap(self):
        before_files = tree_state(self.dest)
        before = launch.run_ledger(self.dest)
        code, out = self.record('conductor', 10_000_000, 5_000_000, 5_000_000, 1_000_000)   # 21M: more than the run cap alone
        self.assertEqual(code, 0, out)
        code, out = self.record('judging-adapter', total=777, session='adapter-author-1', item='adapter for ev')
        self.assertEqual(code, 0, out)
        self.assertEqual(tree_state(self.dest), before_files, 'recording evaluation cost wrote into the run folder')
        after = launch.run_ledger(self.dest)
        self.assertNotIn('evaluation_cost', after)                 # run 장부의 evaluation_cost 블록은 없앤다
        self.assertEqual(after['total_tokens'], before['total_tokens'])
        self.assertEqual(after['total_tokens'], 10_000_000)
        self.assertEqual(set(after['roles']), {'implementer'})
        self.assertEqual(launch.limit_state(self.dest)['used']['token_limit'], 10_000_000)
        launch.limit_gate(self.dest, next_is_feedback=True)          # 10M run + 21M evaluation: the next call still passes
        self.assertFalse((self.dest / 'censored.json').exists())
        launch.write_ledger(self.dest)
        written = json.loads((self.dest / 'ledger.json').read_text())
        self.assertNotIn('evaluation_cost', written)
        self.assertFalse({'conductor', 'judging-adapter'} & set(written['roles']))

    def test_ledger_is_append_only(self):
        self.assertEqual(self.record('conductor', total=100)[0], 0)
        first = self.ledger_bytes()
        self.assertEqual(self.record('conductor', total=50, session='conductor-session-2')[0], 0)
        self.assertTrue(self.ledger_bytes().startswith(first), 'an earlier line was rewritten')
        self.assertEqual([l['total_tokens'] for l in self.lines()], [100, 50])

    def test_the_same_session_is_not_recorded_twice(self):
        # runbook 평가 비용: 같은 역할·세션은 두 번 기록되지 않는다 (one line = one session — no double count)
        self.assertEqual(self.record('conductor', total=100)[0], 0)
        first = self.ledger_bytes()
        code, _ = self.record('conductor', total=100)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.ledger_bytes(), first)

    def test_an_unreadable_ledger_is_not_appended_to(self):
        # runbook 평가 비용: 읽을 수 없는 줄이 있으면 덧붙이지 않고 멈춘다
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text('{"role": "conductor", "broken\n')
        first = self.ledger_bytes()
        code, _ = self.record('conductor', total=100)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.ledger_bytes(), first)

    def test_a_total_only_source_leaves_the_split_unobserved_not_zero(self):
        code, out = self.record('judging-adapter', total=500, session='adapter-author-1', item='adapter for ev')
        self.assertEqual(code, 0, out)
        [line] = self.lines()
        self.assertEqual(line['total_tokens'], 500)
        self.assertEqual(numbers_in(line.get('usage')), [], 'unobserved split recorded as numbers: %r' % line.get('usage'))

    def test_conductor_that_wrote_the_adapter_is_marked_on_its_one_line(self):
        # 진행자가 어댑터를 직접 썼으면 진행자 한 줄에 포함되고 ... 줄에 그 사실을 적는다
        code, out = self.record('conductor', total=100, adapter=True)
        self.assertEqual(code, 0, out)
        [line] = self.lines()
        self.assertIs(line['includes_judging_adapter'], True)

    def test_only_conductor_and_adapter_lines_exist(self):
        # 한 줄 = 진행자 세션 1개 또는 어댑터 작성 1건
        for role in ('implementer', 'user-proxy', 'pm'):
            with self.subTest(role):
                code, _ = self.record(role, total=1)
                self.assertNotEqual(code, 0)
                self.assertIsNone(self.ledger_bytes())

    def test_cost_arguments_are_non_negative_finite_numbers(self):
        # spec 10.5 (U6): 비용 인자는 0 이상의 유한수만 받는다
        bad = {'cost -1': dict(cost=-1), 'cost nan': dict(cost='nan'), 'cost inf': dict(cost='inf'),
               'cost -inf': dict(cost='-inf'), 'total -1': dict(total=-1)}
        for name, kw in bad.items():
            with self.subTest(name):
                kw.setdefault('total', 10)
                code, _ = self.record('conductor', session='s-' + name, **kw)
                self.assertNotEqual(code, 0, name)
                self.assertIsNone(self.ledger_bytes(), name)
        with self.subTest('negative counter'):
            code, _ = self.record('conductor', 1, 1, -1, 1, session='s-neg')
            self.assertNotEqual(code, 0)
            self.assertIsNone(self.ledger_bytes())
        code, out = self.record('conductor', total=0, cost=0, session='s-zero')      # control: zero is allowed
        self.assertEqual(code, 0, out)

    def test_one_session_can_record_one_adapter_line_per_job(self):
        # spec 10.4: 같은 세션이 어댑터를 여러 번 작성하면 건마다 한 줄 — 건을 구분하는 표시를 함께 적는다
        first = self.record('judging-adapter', total=300, session='adapter-author-1', item='arm1 core01 adapter',
                            run_ids=('claude-arm1-core01',))
        second = self.record('judging-adapter', total=400, session='adapter-author-1', item='arm2 core01 adapter',
                             run_ids=('claude-arm2-core01',))
        self.assertEqual(first[0], 0, first[1])
        self.assertEqual(second[0], 0, second[1])
        lines = self.lines()
        self.assertEqual([l['total_tokens'] for l in lines], [300, 400])
        self.assertEqual({l['session'] for l in lines}, {'adapter-author-1'})
        labels = [l.get('item') for l in lines]
        self.assertEqual(sorted(labels), ['arm1 core01 adapter', 'arm2 core01 adapter'])

    def test_an_exact_duplicate_adapter_job_is_refused(self):
        self.assertEqual(self.record('judging-adapter', total=300, session='adapter-author-1', item='job-1')[0], 0)
        first = self.ledger_bytes()
        code, _ = self.record('judging-adapter', total=300, session='adapter-author-1', item='job-1')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.ledger_bytes(), first)

    def test_adapter_line_without_a_job_label_is_refused(self):
        # 건을 구분하는 표시를 함께 적는다 — required on an adapter line (coordinator clarification 2026-11-18)
        for name, item in {'absent': None, 'empty': '', 'blank': '   '}.items():
            with self.subTest(name):
                before = self.ledger_bytes()
                code, _ = self.record('judging-adapter', total=300, session='adapter-author-' + name, item=item)
                self.assertNotEqual(code, 0, name)
                self.assertEqual(self.ledger_bytes(), before, name)

    def test_conductor_line_takes_no_job_label(self):
        # 한 줄 = 진행자 세션 1개: the label distinguishes adapter jobs only (runbook: 진행자 줄은 --item을 받지 않는다)
        code, _ = self.record('conductor', total=100, item='job-1')
        self.assertNotEqual(code, 0)
        self.assertIsNone(self.ledger_bytes())

    def test_every_run_id_is_listed_and_an_unrecorded_run_shows_up(self):
        # 누락 점검: 모든 run id가 어느 한 줄의 run 목록에 들어 있어야 하고, 빠진 run은 "평가 비용 미기록"으로 드러난다
        expected = {'claude-pilot-01', 'claude-arm1-core01', 'claude-arm2-core01', 'claude-arm3-core01'}
        self.assertEqual(self.record('conductor', total=100, run_ids=('claude-pilot-01', 'claude-arm1-core01'))[0], 0)
        self.assertEqual(self.record('judging-adapter', total=7, session='adapter-1', run_ids=('claude-arm2-core01',),
                                     item='claude-arm2-core01 adapter')[0], 0)
        covered = {run_id for line in self.lines() for run_id in line['run_ids']}
        self.assertEqual(expected - covered, {'claude-arm3-core01'})


class NoCallOutlastsTheEightHourCap(unittest.TestCase):
    """Spec 2: 상한 8시간 — no call of the run (a session turn or a user-proxy consultation) may run past it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_proxy_consultation_timeout_is_cut_to_the_remaining_time(self):
        root, dest = new_run(self.base, 'px', 3, ['initial'], started=self.now - timedelta(hours=8) + timedelta(seconds=120))
        record = []
        code, console = proxy_call(root / '.runs', 'px', timeout_seconds=3600, record=record)
        self.assertEqual(code, 0, console)
        starts = [t for c, t in record if c[:2] == ['docker', 'start']]
        self.assertEqual(len(starts), 1)
        self.assertIsNotNone(starts[0])
        self.assertLessEqual(starts[0], 120)
        self.assertGreater(starts[0], 0)

    def test_proxy_consultation_after_eight_hours_makes_no_paid_call(self):
        root, dest = new_run(self.base, 'px8', 3, ['initial'], started=self.now - timedelta(hours=8, seconds=1))
        record = []
        code, _ = proxy_call(root / '.runs', 'px8', timeout_seconds=3600, record=record)
        self.assertNotEqual(code, 0)
        self.assertEqual(paid_calls(record), [])
        self.assertEqual(json.loads((dest / 'censored.json').read_text())['status'], 'CENSORED_TIME')

    def test_arm3_turn_timeout_is_recomputed_after_the_hook_selftest(self):
        # time spent in the self-test is time of the run: the call gets only what is left after it
        start = self.now - timedelta(hours=8) + timedelta(seconds=120)
        root, dest = new_run(self.base, 'st', 3, ['initial'], started=start)
        record = []
        drive_turn(root, 'st', resume=True, kind='feedback', reported=U2, timeout=3600, record=record,
                   selftest=lambda m: age_first_turn(dest, start - timedelta(seconds=90)),
                   projects={'-workspace/s.jsonl': [assistant('t0', FABLE, U1), assistant('r2', FABLE, U2)]})
        starts = [t for c, t in record if c[:2] == ['docker', 'start']]
        self.assertEqual(len(starts), 1)
        self.assertLessEqual(starts[0], 30)

    def test_arm3_selftest_that_reaches_eight_hours_leaves_no_paid_call(self):
        start = self.now - timedelta(hours=8) + timedelta(seconds=60)
        root, dest = new_run(self.base, 'st8', 3, ['initial'], started=start)
        error, paid = attempt_turn(root, 'st8', resume=True, kind='feedback',
                                   selftest=lambda m: age_first_turn(dest, start - timedelta(seconds=120)))
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        self.assertEqual(json.loads((dest / 'censored.json').read_text())['status'], 'CENSORED_TIME')


CRITERIA = tuple('C%02d' % n for n in range(1, 21))
FINISHER = load_script('finish_run')
CAPTURE = load_script('capture_turn')
_rec_spec = importlib.util.spec_from_file_location('spec_inv_recording', HERE.parents[1] / 'recording' / 'record.py')
RECORDER = importlib.util.module_from_spec(_rec_spec)
_rec_spec.loader.exec_module(RECORDER)
RUNNER_TOOLS = ('Bash', 'Read', 'Edit', 'Write', 'Glob', 'Grep', 'WebSearch', 'WebFetch')


def record_folder(root, run_id, arm, core=1, pilot=False):
    """Where the spec puts a run's records: docs/experiments/<arm>/core<N>/, a pilot under docs/pilots/<run id>/ (10.3)."""
    return root / 'docs' / 'pilots' / run_id if pilot else root / 'docs' / 'experiments' / str(arm) / ('core%d' % core)


def write_record(root, run_id, arm, core=1, status='RUNNING', **fields):
    """The run.json a cycle start leaves for a main-experiment run (status RUNNING after its first turn)."""
    folder = record_folder(root, run_id, arm, core)
    folder.mkdir(parents=True, exist_ok=True)
    RECORDER.init_run(folder)                    # the NOT_STARTED journal prepare.py leaves (recording/README.md init_run)
    (folder / 'run.json').write_text(json.dumps(dict({'run_id': run_id, 'arm': arm, 'core': core, 'status': status}, **fields)))
    return folder


def write_verdict(folder, run_id, results=None, judged_by='main supervising session', evidence=('acceptance-audit.json',),
                  drop=(), create_evidence=True, **extra):
    """The main session's verdict table (spec 10.2): C01..C20 each a result and evidence paths in the record folder.
    results: {criterion: result} overrides on top of all PASS."""
    folder.mkdir(parents=True, exist_ok=True)
    if create_evidence:
        for rel in evidence:
            (folder / rel).write_text(json.dumps({'judged': rel}))
    results = dict({c: 'PASS' for c in CRITERIA}, **(results or {}))
    criteria = {c: {'result': results[c], 'evidence': list(evidence), 'note': 'observed'} for c in CRITERIA if c not in drop}
    table = dict({'run_id': run_id, 'judged_by': judged_by, 'judged_at': '2026-11-18T00:00:00+00:00', 'criteria': criteria}, **extra)
    if judged_by is None:
        del table['judged_by']
    (folder / 'verdict.json').write_text(json.dumps(table))
    return folder


def finish(root, run_id, *options):
    """scripts/finish_run.py main (the public CLI) with the record tree rooted at `root`. Returns (exit code, console)."""
    argv = ['finish_run.py', '--run-id', run_id, '--runs', str(root / '.runs')] + list(options)
    out = io.StringIO()
    with patch.object(FINISHER, 'ROOT', root), patch.object(sys, 'argv', argv), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = FINISHER.main()
        except SystemExit as exc:
            code = exc.code
        except Exception as exc:                     # a refusal raised instead of exiting is still a refusal
            code, _ = 1, out.write(repr(exc))
    return code or 0, out.getvalue()


def read_record(folder):
    return json.loads((folder / 'run.json').read_text())


def finished_turns(root, run_id):
    """Turn numbers of the run directory whose result.json says the turn ended (status ok or failed — runbook ⑤)."""
    turns = Path(root) / '.runs' / run_id / 'turns'
    out = []
    for d in sorted(turns.iterdir()) if turns.exists() else []:
        result = d / 'result.json'
        if result.exists() and json.loads(result.read_text()).get('status') in ('ok', 'failed'):
            out.append(int(d.name))
    return out


def run_record_folder(root, run_id):
    manifest = json.loads((Path(root) / '.runs' / run_id / 'manifest.json').read_text())
    return record_folder(root, run_id, manifest['arm'], manifest.get('core', 1), pilot=bool(manifest.get('pilot')))


def close_turns(root, run_id, only=None):
    """Spec 10.1 steps before the finish: launch.py normalize -> capture_turn.py for every finished turn that is not yet
    in the record folder (both through their public entry points). only: capture just these turn numbers.
    Returns the turn numbers captured."""
    root = Path(root)
    folder = run_record_folder(root, run_id)
    todo = [n for n in finished_turns(root, run_id)
            if (only is None or n in only) and not (folder / 'turns' / ('%04d' % n)).exists()]
    if not todo:                                               # nothing left to close: the record folder stays as it is
        return []
    for shared in ('recording', 'measurement'):                # the shared recorder and adapter (read only, linked)
        link = root.parent / shared
        if not link.exists():
            link.symlink_to(HERE.parents[1] / shared)
    with patch.object(launch, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        launch.normalize(SimpleNamespace(runs=root / '.runs', run_id=run_id))
    captured = []
    for n in todo:
        analysis = root.parent / ('analysis-%04d.md' % n)
        analysis.write_text('turn %d analysis' % n)
        with patch.object(CAPTURE, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            CAPTURE.capture(run_id, n, analysis, 'RUNNING', runs=root / '.runs')
        captured.append(n)
    return captured


def close_and_finish(root, run_id, *options):
    """The fixed order of spec 10.1: normalize -> capture_turn -> finish_run."""
    close_turns(root, run_id)
    return finish(root, run_id, *options)


def first_turn(root, run_id, arm, usage=U1, reported='same'):
    """The run's first turn through launch.turn with the fake docker (what a real call writes)."""
    model = FABLE if arm == 3 else OPUS
    result, _, error, _ = drive_turn(root, run_id, reported=usage if reported == 'same' else reported,
                                     projects={'-workspace/s.jsonl': [assistant('first-' + run_id, model, usage)]})
    assert error is None, error
    return result


def arm3_turn(root, run_id, *, reviewer=True, worker=True):
    """An arm 3 turn whose PM (Fable), worker (Opus) and second reviewer (Fable) requests are all observed."""
    sid = 'aaaaaaaa-0000-4000-8000-%012d' % (abs(hash(run_id)) % 10 ** 12)
    parent = [assistant('pm-' + run_id, FABLE, U1)]
    projects = {'-workspace/s.jsonl': parent}
    if worker:
        parent.append(assistant('w-' + run_id, OPUS, U2, sidechain=True))
        projects['-workspace/s/subagents/agent-a1.jsonl'] = [assistant('w-' + run_id, OPUS, U2, sidechain=True)]
    kw = {}
    if reviewer:
        kw = dict(reviewer_log='T1-1 started session=%s model=%s\nT1-1 exit=0\n' % (sid, FABLE),
                  reviewer_projects={'-mirror/%s.jsonl' % sid: [assistant('rv-' + run_id, FABLE, U2, session=sid)]})
    result, _, error, _ = drive_turn(root, run_id, reported=U1, projects=projects, **kw)
    assert error is None, error
    return result


class FinishFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def started_run(self, run_id='claude-arm1-core01', arm=1, core=1, **record_fields):
        """A main-experiment run with one real (fake-docker) turn, normalized and captured into its RUNNING record
        (spec 10.1: the finish comes after normalize and capture_turn). record_fields are then set on run.json as a
        record that says otherwise would (e.g. a NOT_STARTED status, hand-written evidence fields).
        Returns (root, record folder)."""
        root = make_root(self.base / run_id)
        init_run(root, run_id, arm, core=core)
        first_turn(root, run_id, arm)
        folder = write_record(root, run_id, arm, core)
        self.assertEqual(close_turns(root, run_id), [1])
        if record_fields:
            run = read_record(folder)
            run.update(record_fields)
            (folder / 'run.json').write_text(json.dumps(run))
        return root, folder


class CapRecordIsAnnotation(FinishFixture):
    """Spec 10.2 (2026-11-18, replaces "완료 + 초과 only when the capped call was a turn" and "blocked before a call + all
    PASS is refused"): 통과 여부는 판정표만으로 정하고, 상한 기록은 표시로 붙는다. 판정표가 전부 PASS이면 상한 기록이 있든 없든
    PASSED다. 상한 기록이 있으면 cap_reached=true와 함께 상한의 종류·시점(호출 전 차단 / 호출 뒤 초과)·호출 종류(턴 / 대리
    사용자)·초과량을 run.json에 옮겨 적는다(결과 표에서 "완료", "완료 + 상한 도달", "완료 + 초과"를 구분). 통과하지 못했고 상한
    기록이 있으면 CENSORED_*다. 상한 기록 자체는 고치지 않는다. Spec 2 / §0: 상한은 다음 호출을 막는 규칙 — a capped run gets no
    further paid call. Field names (cap_status, cap_when, cap_call, cap_overshoot; "before call"/"after call") are the
    runbook's (종료 상태 표)."""

    def setup_run(self, run_id, arm, prior_tokens, started=None):
        root = make_root(self.base / run_id)
        init_run(root, run_id, arm)
        dest = root / '.runs' / run_id
        first_turn(root, run_id, arm, usage=prior_tokens)
        if started is not None:
            age_first_turn(dest, started)
        return root, dest, write_record(root, run_id, arm)

    def final_call(self, root, run_id, arm, usage, side_effect=None):
        model = FABLE if arm == 3 else OPUS
        drive_turn(root, run_id, resume=True, kind='feedback', reported=usage, side_effect=side_effect,
                   projects={'-workspace/s.jsonl': [assistant('last', model, usage)]})

    # --- the six kinds of cap record a run can carry ---------------------------------------------------

    def token_after_turn(self, run_id, arm=3):
        # 19,999,980 before the final call; the final turn adds 1,000 -> 980 over the 20,000,000 cap
        root, dest, folder = self.setup_run(run_id, arm, tokens(9_999_990, 9_999_990))
        self.assertFalse((dest / 'censored.json').exists())
        self.final_call(root, run_id, arm, tokens(500, 500))
        return root, dest, folder

    def time_after_turn(self, run_id, arm=1):
        start = self.now - timedelta(hours=8) + timedelta(seconds=60)
        root, dest, folder = self.setup_run(run_id, arm, tokens(1, 1), started=start)
        self.final_call(root, run_id, arm, U2, side_effect=lambda d: age_first_turn(d, start - timedelta(seconds=90)))
        return root, dest, folder

    def token_after_proxy(self, run_id, arm=3):
        root, dest, folder = self.setup_run(run_id, arm, tokens(9_999_990, 9_999_990))
        code, console = proxy_call(root / '.runs', run_id, usage=tokens(500, 500))
        self.assertTrue((dest / 'censored.json').exists(), 'crossing the cap in a proxy call left no record: %s' % console)
        return root, dest, folder

    def time_before_turn(self, run_id, arm=1):
        # the eight hours pass between calls: the next turn is refused before it is made
        root, dest, folder = self.setup_run(run_id, arm, tokens(1, 1), started=self.now - timedelta(hours=8, seconds=1))
        self.assertFalse((dest / 'censored.json').exists())
        _, paid = attempt_turn(root, run_id, resume=True, kind='feedback')
        self.assertEqual(paid, [])
        return root, dest, folder

    def time_before_proxy(self, run_id, arm=3):
        root, dest, folder = self.setup_run(run_id, arm, tokens(1, 1), started=self.now - timedelta(hours=8, seconds=1))
        record = []
        code, _ = proxy_call(root / '.runs', run_id, record=record)
        self.assertNotEqual(code, 0)
        self.assertEqual(paid_calls(record), [])
        return root, dest, folder

    def feedback_cap(self, run_id, arm=1):
        # initial + 30 delivered feedback turns (real fake-docker turns); the 31st feedback is refused before the call
        root, dest, folder = self.setup_run(run_id, arm, tokens(1, 1))
        model = FABLE if arm == 3 else OPUS
        lines = [assistant('first-' + run_id, model, tokens(1, 1))]
        for i in range(30):
            lines = lines + [assistant('fb%d' % i, model, tokens(1, 1))]
            result, _, error, _ = drive_turn(root, run_id, resume=True, kind='feedback', reported=tokens(1, 1),
                                             projects={'-workspace/s.jsonl': lines})
            self.assertIsNone(error)
            self.assertEqual(result['status'], 'ok')
        _, paid = attempt_turn(root, run_id, resume=True, kind='feedback')
        self.assertEqual(paid, [])
        return root, dest, folder

    CASES = {
        # name: (maker, arm, status, when, call, overshoot expected)
        'token cap crossed in the final turn': ('token_after_turn', 3, 'CENSORED_BUDGET', 'after call', 'turn', True),
        'time cap crossed in the final turn': ('time_after_turn', 1, 'CENSORED_TIME', 'after call', 'turn', True),
        'token cap crossed in a user-proxy call': ('token_after_proxy', 3, 'CENSORED_BUDGET', 'after call', 'user-proxy', True),
        'time cap blocked a turn before the call': ('time_before_turn', 1, 'CENSORED_TIME', 'before call', 'turn', False),
        'time cap blocked a user-proxy call before the call': ('time_before_proxy', 3, 'CENSORED_TIME', 'before call', 'user-proxy',
                                                               False),
        'feedback cap blocked the 31st feedback': ('feedback_cap', 1, 'CENSORED_TURNS', 'before call', 'turn', False),
    }

    def capped(self, name, run_id):
        maker, arm, status, when, call, overshoot = self.CASES[name]
        self.base = Path(self.tmp.name) / run_id
        root, dest, folder = getattr(self, maker)(run_id, arm)
        self.assertTrue((dest / 'censored.json').exists(), name)
        self.assertEqual(json.loads((dest / 'censored.json').read_text())['status'], status, name)
        return root, dest, folder, arm

    def assert_no_further_paid_call(self, root, run_id, arm):
        error, paid = attempt_turn(root, run_id, resume=True, kind='feedback')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        if arm == 3:
            record = []
            code, _ = proxy_call(root / '.runs', run_id, record=record)
            self.assertNotEqual(code, 0)
            self.assertEqual(paid_calls(record), [])

    def assert_annotation(self, run, name):
        _, _, status, when, call, overshoot = self.CASES[name]
        self.assertIs(run['cap_reached'], True, name)
        self.assertEqual(run['cap_status'], status, name)
        self.assertEqual(run['cap_when'], when, name)
        self.assertEqual(run['cap_call'], call, name)
        if overshoot:
            self.assertTrue([n for n in numbers_in(run['cap_overshoot']) if n > 0], '%s: overshoot not recorded' % name)
        else:
            self.assertIsNone(run['cap_overshoot'], name)

    def test_every_cap_kind_with_an_all_pass_table_is_passed_with_its_annotation(self):
        for i, name in enumerate(self.CASES):
            with self.subTest(name):
                run_id = 'cap%d' % i
                root, dest, folder, arm = self.capped(name, run_id)
                censored = (dest / 'censored.json').read_bytes()
                write_verdict(folder, run_id)
                code, out = close_and_finish(root, run_id)
                self.assertEqual(code, 0, '%s: %s' % (name, out))
                run = read_record(folder)
                self.assertEqual(run['status'], 'PASSED', name)
                self.assertIs(run['all_acceptance_passed'], True, name)
                self.assert_annotation(run, name)
                self.assertEqual((dest / 'censored.json').read_bytes(), censored, '%s: cap record modified' % name)
                self.assert_no_further_paid_call(root, run_id, arm)
                self.assertEqual((dest / 'censored.json').read_bytes(), censored, name)

    def test_token_overshoot_amount_is_carried_over(self):
        root, dest, folder, _ = self.capped('token cap crossed in the final turn', 'tok')
        write_verdict(folder, 'tok')
        code, out = close_and_finish(root, 'tok')
        self.assertEqual(code, 0, out)
        self.assertIn(980, numbers_in(read_record(folder)['cap_overshoot']), 'token overshoot (20,000,980 - 20,000,000) not recorded')

    def test_completed_completed_at_cap_and_completed_over_cap_are_distinguishable(self):
        # 결과 표에서 "완료", "완료 + 상한 도달", "완료 + 초과"를 구분
        def mark(run):
            return (run['cap_reached'], run['cap_when'], run['cap_overshoot'] is not None)
        root, folder = self.started_run('clean', 1)
        write_verdict(folder, 'clean')
        self.assertEqual(finish(root, 'clean')[0], 0)
        marks = {'완료': mark(read_record(folder))}
        for label, name in (('완료 + 상한 도달', 'time cap blocked a turn before the call'),
                            ('완료 + 초과', 'time cap crossed in the final turn')):
            run_id = 'd' + str(len(marks))
            root, dest, folder, _ = self.capped(name, run_id)
            write_verdict(folder, run_id)
            self.assertEqual(close_and_finish(root, run_id)[0], 0)
            marks[label] = mark(read_record(folder))
        self.assertEqual(len(set(marks.values())), 3, marks)
        self.assertEqual(marks['완료'][0], False)

    def test_not_passed_capped_run_is_censored_with_the_cap_records_status(self):
        for i, name in enumerate(self.CASES):
            for result in ('FAIL', 'PARTIAL', 'NOT_RUN'):
                with self.subTest(name, result=result):
                    run_id = 'np%d-%s' % (i, result)
                    root, dest, folder, arm = self.capped(name, run_id)
                    censored = (dest / 'censored.json').read_bytes()
                    write_verdict(folder, run_id, results={'C11': result})
                    code, out = close_and_finish(root, run_id)
                    self.assertEqual(code, 0, out)
                    run = read_record(folder)
                    self.assertEqual(run['status'], json.loads(censored)['status'])
                    self.assertTrue(run['status'].startswith('CENSORED_'))
                    self.assertIs(run['all_acceptance_passed'], False)
                    self.assert_annotation(run, name)
                    self.assertEqual((dest / 'censored.json').read_bytes(), censored)
                    self.assert_no_further_paid_call(root, run_id, arm)

    def test_cap_record_without_the_call_kind_still_passes_and_is_left_as_it_is(self):
        # 상한 기록 자체는 고치지 않는다 / 판정표가 전부 PASS이면 상한 기록이 있든 없든 PASSED (runbook: 옛 기록은 cap_call=null)
        root, dest, folder, _ = self.capped('token cap crossed in the final turn', 'nocall')

        def strip(value):
            if isinstance(value, dict):
                return {k: strip(v) for k, v in value.items() if k != 'call'}
            if isinstance(value, list):
                return [strip(v) for v in value]
            return value
        record = json.loads((dest / 'censored.json').read_text())
        self.assertIn('"call"', json.dumps(record), 'control: the runner records the call kind')
        (dest / 'censored.json').write_text(json.dumps(strip(record)))           # a cap record written without that field
        censored = (dest / 'censored.json').read_bytes()
        write_verdict(folder, 'nocall')
        code, out = close_and_finish(root, 'nocall')
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertEqual(run['status'], 'PASSED')
        self.assertIs(run['cap_reached'], True)
        self.assertIsNone(run['cap_call'])
        self.assertEqual((dest / 'censored.json').read_bytes(), censored)

    def test_uncapped_passed_run_is_completed_without_a_cap_mark(self):
        root, folder = self.started_run('clean', 1)
        write_verdict(folder, 'clean')
        code, out = finish(root, 'clean')
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertEqual(run['status'], 'PASSED')
        self.assertIs(run['cap_reached'], False)
        for field in ('cap_status', 'cap_when', 'cap_call', 'cap_overshoot'):
            self.assertIsNone(run.get(field), field)


class FinishComesAfterCapture(FinishFixture):
    """Spec 10.1: normalize → capture_turn(남은 턴 기록 — 기록 폴더의 파일이 여기서 마지막으로 바뀐다) → 종료 기록(근거 해시는 이
    시점 값). A run whose finished turns are not yet captured into the record folder cannot be finished; "captured" means
    the capture of that turn ran to completion."""

    def assert_refused(self, root, run_id, folder, name=''):
        before = (folder / 'run.json').read_bytes()
        code, out = finish(root, run_id)
        self.assertNotEqual(code, 0, '%s: %s' % (name, out))
        self.assertEqual((folder / 'run.json').read_bytes(), before, name)
        self.assertNotIn(read_record(folder)['status'], ('PASSED', 'FAILED'), name)

    def test_no_turn_captured_blocks_the_finish(self):
        root = make_root(self.base / 'none')
        init_run(root, 'claude-arm1-core01', 1)
        first_turn(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        write_verdict(folder, 'claude-arm1-core01')
        self.assert_refused(root, 'claude-arm1-core01', folder)
        self.assertEqual(close_and_finish(root, 'claude-arm1-core01')[0], 0)          # control: after capture it finishes

    def test_a_later_turn_not_captured_blocks_the_finish(self):
        for result in ('PASS', 'FAIL'):
            with self.subTest(result):
                self.base = Path(self.tmp.name) / result
                root, folder = self.started_run()                    # turn 0001 captured
                drive_turn(root, 'claude-arm1-core01', resume=True, kind='feedback', reported=U2,
                           projects={'-workspace/s.jsonl': [assistant('first-claude-arm1-core01', OPUS, U1),
                                                            assistant('second', OPUS, U2)]})
                write_verdict(folder, 'claude-arm1-core01', results={'C05': result})
                self.assert_refused(root, 'claude-arm1-core01', folder, result)
                self.assertEqual(close_turns(root, 'claude-arm1-core01'), [2])
                code, out = finish(root, 'claude-arm1-core01')
                self.assertEqual(code, 0, out)

    def test_a_failed_turn_not_captured_blocks_the_finish(self):
        # finished = result.json status ok or failed (runbook ⑤)
        root, folder = self.started_run()

        def container_died(dest):
            raise RuntimeError('container died during the call')
        result, _, _, _ = drive_turn(root, 'claude-arm1-core01', resume=True, kind='feedback', reported=U2,
                                     side_effect=container_died,
                                     projects={'-workspace/s.jsonl': [assistant('r2', OPUS, U2)]})
        self.assertEqual(result['status'], 'failed', 'control: the fake call left a failed turn')
        write_verdict(folder, 'claude-arm1-core01', results={'C05': 'FAIL'})
        self.assert_refused(root, 'claude-arm1-core01', folder)

    def test_capped_run_with_its_last_turn_uncaptured_is_not_finished(self):
        # the order holds for a capped run as well: a cap record does not stand in for the capture
        root = make_root(self.base / 'cap')
        init_run(root, 'claude-arm1-core01', 1)
        first_turn(root, 'claude-arm1-core01', 1, usage=tokens(9_999_990, 9_999_990))
        folder = write_record(root, 'claude-arm1-core01', 1)
        self.assertEqual(close_turns(root, 'claude-arm1-core01'), [1])
        drive_turn(root, 'claude-arm1-core01', resume=True, kind='feedback', reported=tokens(500, 500),
                   projects={'-workspace/s.jsonl': [assistant('last', OPUS, tokens(500, 500))]})
        self.assertTrue((root / '.runs/claude-arm1-core01/censored.json').exists())
        write_verdict(folder, 'claude-arm1-core01')
        self.assert_refused(root, 'claude-arm1-core01', folder)

    def test_a_captured_turn_whose_files_are_incomplete_blocks_the_finish(self):
        # a capture that stopped partway leaves only part of what a completed capture leaves
        root, folder = self.started_run('probe')
        names = sorted(p.name for p in (folder / 'turns' / '0001').iterdir())
        self.assertIn('result.json', names)
        for name in names:
            with self.subTest(missing=name):
                self.base = Path(self.tmp.name) / ('part-' + name)
                root, folder = self.started_run()
                (folder / 'turns' / '0001' / name).unlink()
                write_verdict(folder, 'claude-arm1-core01')
                self.assert_refused(root, 'claude-arm1-core01', folder, 'turn file %s missing' % name)

    def test_a_capture_that_failed_before_its_journal_entry_blocks_the_finish(self):
        # capture_turn run through its public entry point, with the shared recorder failing when it appends the turn:
        # the turn folder may exist, but the capture did not complete
        root = make_root(self.base / 'jfail')
        init_run(root, 'claude-arm1-core01', 1)
        first_turn(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        broken = root.parent / 'recording'
        broken.mkdir()
        (broken / 'record.py').write_text(
            'import importlib.util as u\n'
            's = u.spec_from_file_location("real_record", %r)\n'
            'm = u.module_from_spec(s); s.loader.exec_module(m)\n'
            'globals().update({k: v for k, v in vars(m).items() if not k.startswith("__")})\n'
            'def record_turn(*a, **k):\n'
            '    raise OSError("disk full while appending the turn")\n' % str(HERE.parents[1] / 'recording' / 'record.py'))
        with self.assertRaises(BaseException):
            close_turns(root, 'claude-arm1-core01')
        write_verdict(folder, 'claude-arm1-core01')
        self.assert_refused(root, 'claude-arm1-core01', folder)

    def test_finish_does_not_capture_by_itself(self):
        # the record folder changes last at capture_turn; a refused finish leaves the turns uncaptured
        root = make_root(self.base / 'self')
        init_run(root, 'claude-arm1-core01', 1)
        first_turn(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        write_verdict(folder, 'claude-arm1-core01')
        before = tree_state(folder)
        finish(root, 'claude-arm1-core01')
        self.assertEqual(tree_state(folder), before)


def write_criteria(folder, run_id, per, judged_by='main supervising session'):
    """A verdict table: all PASS with an existing evidence file, overridden per criterion by `per` {criterion: entry}."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'acceptance-audit.json').write_text('{}')
    criteria = {c: {'result': 'PASS', 'evidence': ['acceptance-audit.json'], 'note': 'observed'} for c in CRITERIA}
    criteria.update(per)
    (folder / 'verdict.json').write_text(json.dumps({'run_id': run_id, 'judged_by': judged_by,
                                                     'judged_at': '2026-11-18T00:00:00+00:00', 'criteria': criteria}))


class EveryCriterionNeedsSupport(FinishFixture):
    """Spec 10.2 (2026-11-18): 판정표의 각 기준은 근거가 있어야 한다: PASS·FAIL·PARTIAL은 실제로 있는 근거 파일 1개 이상,
    NOT_RUN은 사유(note). 이를 채우지 못한 판정표로는 종료 기록을 할 수 없다(통과가 아닌 종료 상태도 마찬가지)."""

    def assert_refused(self, root, folder, *options, name=''):
        before = (folder / 'run.json').read_bytes()
        code, out = finish(root, 'claude-arm1-core01', *options)
        self.assertNotEqual(code, 0, '%s: %s' % (name, out))
        self.assertEqual((folder / 'run.json').read_bytes(), before, name)

    UNSUPPORTED = {
        'no evidence listed': {'evidence': []},
        'evidence key absent': None,
        'only a missing file': {'evidence': ['never-written.json']},
        'only a path outside the record folder': {'evidence': ['../../../../outside.json']},
        'only an absolute path': {'evidence': ['/etc/hostname']},
    }

    def entry(self, result, how):
        if how is None:
            return {'result': result, 'note': 'observed'}
        return dict({'result': result, 'note': 'observed'}, **how)

    def test_pass_fail_partial_without_an_existing_evidence_file_block_the_finish(self):
        for result in ('PASS', 'FAIL', 'PARTIAL'):
            for name, how in self.UNSUPPORTED.items():
                with self.subTest(result=result, case=name):
                    self.base = Path(self.tmp.name) / ('%s-%s' % (result, name.replace(' ', '-')))
                    root, folder = self.started_run()
                    (folder / '../../../../outside.json').resolve().write_text('{}')
                    write_criteria(folder, 'claude-arm1-core01', {'C07': self.entry(result, how)})
                    self.assert_refused(root, folder, name='%s %s' % (result, name))

    def test_not_run_without_a_note_blocks_the_finish(self):
        for name, entry in {'empty note': {'result': 'NOT_RUN', 'evidence': [], 'note': ''},
                            'blank note': {'result': 'NOT_RUN', 'evidence': [], 'note': '   '},
                            'no note': {'result': 'NOT_RUN', 'evidence': []},
                            'null note with evidence': {'result': 'NOT_RUN', 'evidence': ['acceptance-audit.json'],
                                                        'note': None}}.items():
            with self.subTest(name):
                self.base = Path(self.tmp.name) / ('nr-' + name.replace(' ', '-'))
                root, folder = self.started_run()
                write_criteria(folder, 'claude-arm1-core01', {'C20': entry})
                self.assert_refused(root, folder, name=name)

    def test_unsupported_criterion_blocks_invalid_infra_too(self):
        # 통과가 아닌 종료 상태도 마찬가지
        root, folder = self.started_run()
        (folder / 'docker.log').write_text('daemon died')
        write_criteria(folder, 'claude-arm1-core01', {'C03': {'result': 'FAIL', 'evidence': [], 'note': 'x'}})
        self.assert_refused(root, folder, '--invalid-infra', 'docker daemon died', '--evidence', 'docker.log')

    def test_supported_tables_are_accepted(self):
        # control: NOT_RUN with a reason and no file, FAIL/PARTIAL with one existing file among missing ones
        cases = {'NOT_RUN with a note': ({'C20': {'result': 'NOT_RUN', 'evidence': [], 'note': 'k6 not runnable'}}, 'FAILED'),
                 'FAIL with a file': ({'C04': {'result': 'FAIL', 'evidence': ['acceptance-audit.json'], 'note': ''}}, 'FAILED'),
                 'PARTIAL with one existing file': ({'C04': {'result': 'PARTIAL',
                                                             'evidence': ['acceptance-audit.json', 'gone.json'],
                                                             'note': 'x'}}, 'FAILED')}
        for name, (per, status) in cases.items():
            with self.subTest(name):
                self.base = Path(self.tmp.name) / ('ok-' + name.replace(' ', '-'))
                root, folder = self.started_run()
                write_criteria(folder, 'claude-arm1-core01', per)
                code, out = finish(root, 'claude-arm1-core01')
                self.assertEqual(code, 0, '%s: %s' % (name, out))
                self.assertEqual(read_record(folder)['status'], status)

    def test_pass_with_one_existing_and_one_missing_file_is_not_all_acceptance_passed(self):
        # all_acceptance_passed 원천: 20개 모두 PASS이고 근거 파일이 모두 존재
        root, folder = self.started_run()
        write_criteria(folder, 'claude-arm1-core01',
                       {'C09': {'result': 'PASS', 'evidence': ['acceptance-audit.json', 'never-written.json'], 'note': 'x'}})
        code, out = finish(root, 'claude-arm1-core01')
        if code != 0:
            return                                       # refusing is also within the spec
        self.assertIs(read_record(folder)['all_acceptance_passed'], False, out)


class VerdictTableDecidesTheOutcome(FinishFixture):
    """Spec 10.2: 종료 상태 PASSED·FAILED는 인자로 받지 않는다. 판정표에서 도출한다. 판정표(verdict.json)는 C01~C20 각각에
    PASS·FAIL·PARTIAL·NOT_RUN 중 하나와 근거 산출물 경로를 적는다. 종료 기록은 근거 파일이 실제로 있는지 확인하고 해시를 함께
    남긴다. 20개가 모두 PASS일 때만 통과다(PARTIAL·NOT_RUN은 통과가 아니다). 종료 기록이 기계로 확인하는 것은 근거 파일의 존재와
    해시, 판정표의 작성자 표시(judged_by)까지다."""

    def assert_not_passed(self, root, run_id, folder, name):
        before = (folder / 'run.json').read_bytes()
        code, out = finish(root, run_id)
        run = read_record(folder)
        if code != 0:
            self.assertEqual((folder / 'run.json').read_bytes(), before, name)
        self.assertNotEqual(run['status'], 'PASSED', '%s: %s' % (name, out))
        self.assertIsNot(run.get('all_acceptance_passed'), True, name)

    def test_passed_and_failed_cannot_be_given_as_arguments(self):
        root, folder = self.started_run()
        write_verdict(folder, 'claude-arm1-core01', results={'C01': 'FAIL'})
        before = (folder / 'run.json').read_bytes()
        for option in (['--verdict', 'PASSED'], ['--status', 'PASSED'], ['--passed'], ['--verdict', 'FAILED']):
            with self.subTest(option):
                code, _ = finish(root, 'claude-arm1-core01', *option)
                self.assertNotEqual(code, 0)
                self.assertEqual((folder / 'run.json').read_bytes(), before)
        usage = subprocess.run([sys.executable, str(HERE.parent / 'scripts' / 'finish_run.py'), '--help'],
                               capture_output=True, text=True).stdout
        for option in ('--verdict', '--status', '--passed', '--failed'):
            self.assertNotIn(option + ' ', usage)

    def test_all_twenty_pass_with_existing_evidence_is_passed_and_hashed(self):
        root, folder = self.started_run()
        write_verdict(folder, 'claude-arm1-core01', evidence=('acceptance-http.json', 'acceptance-audit.json'))
        code, out = finish(root, 'claude-arm1-core01')
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertEqual(run['status'], 'PASSED')
        self.assertIs(run['all_acceptance_passed'], True)
        text = json.dumps(run)
        for rel in ('acceptance-http.json', 'acceptance-audit.json'):
            self.assertIn(hashlib.sha256((folder / rel).read_bytes()).hexdigest(), text, 'hash of %s not recorded' % rel)
        self.assertIn('main supervising session', text)                         # who judged

    def test_any_criterion_not_pass_is_not_a_pass(self):
        for result in ('FAIL', 'PARTIAL', 'NOT_RUN'):
            for criterion in ('C01', 'C13', 'C20'):
                with self.subTest(result=result, criterion=criterion):
                    run_id = 'claude-arm2-core01'
                    self.base = Path(self.tmp.name) / (result + criterion)
                    root, folder = self.started_run(run_id, 2)
                    write_verdict(folder, run_id, results={criterion: result})
                    code, out = finish(root, run_id)
                    self.assertEqual(code, 0, out)
                    run = read_record(folder)
                    self.assertEqual(run['status'], 'FAILED')
                    self.assertIs(run['all_acceptance_passed'], False)

    def test_malformed_tables_never_pass(self):
        cases = {
            'missing criterion': dict(drop=('C07',)),
            'unknown result': dict(results={'C09': 'OK'}),
            'lower-case pass': dict(results={'C09': 'pass'}),
            'null result': dict(results={'C09': None}),
            'example table': dict(_example='EXAMPLE ONLY'),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.base = Path(self.tmp.name) / name.replace(' ', '-')
                root, folder = self.started_run()
                write_verdict(folder, 'claude-arm1-core01', **kw)
                self.assert_not_passed(root, 'claude-arm1-core01', folder, name)

    def test_missing_or_absent_evidence_makes_all_acceptance_passed_false(self):
        # all_acceptance_passed 원천: 판정표 20개 모두 PASS이고 근거 파일이 모두 존재
        cases = {'evidence file missing': dict(evidence=('acceptance-audit.json', 'never-written.json'), create_evidence=False),
                 'no evidence listed': dict(evidence=()),
                 'evidence outside the record folder': dict(evidence=('../../../../outside.json',), create_evidence=False),
                 'absolute evidence path': dict(evidence=('/etc/hostname',), create_evidence=False)}
        for name, kw in cases.items():
            with self.subTest(name):
                self.base = Path(self.tmp.name) / name.replace(' ', '-')
                root, folder = self.started_run()
                if name == 'evidence file missing':
                    (folder / 'acceptance-audit.json').write_text('{}')
                if name == 'evidence outside the record folder':
                    (folder / '../../../../outside.json').resolve().write_text('{}')
                write_verdict(folder, 'claude-arm1-core01', **kw)
                before = (folder / 'run.json').read_bytes()
                code, out = finish(root, 'claude-arm1-core01')
                if code != 0:
                    self.assertEqual((folder / 'run.json').read_bytes(), before)
                    continue
                self.assertIs(read_record(folder)['all_acceptance_passed'], False, '%s: %s' % (name, out))

    def test_verdict_without_a_judge_is_refused(self):
        # runbook 판정표: judged_by 필수, 비어 있으면 거부 (spec 10.2: 기계로 확인하는 것은 … judged_by까지)
        for name, judge in {'empty': '', 'blank': '   ', 'absent': None}.items():
            with self.subTest(name):
                self.base = Path(self.tmp.name) / ('judge-' + name)
                root, folder = self.started_run()
                write_verdict(folder, 'claude-arm1-core01', judged_by=judge)
                before = (folder / 'run.json').read_bytes()
                code, _ = finish(root, 'claude-arm1-core01')
                self.assertNotEqual(code, 0)
                self.assertEqual((folder / 'run.json').read_bytes(), before)

    def test_no_verdict_table_is_refused(self):
        root, folder = self.started_run()
        before = (folder / 'run.json').read_bytes()
        code, _ = finish(root, 'claude-arm1-core01')
        self.assertNotEqual(code, 0)
        self.assertEqual((folder / 'run.json').read_bytes(), before)

    def test_finish_does_not_need_an_evaluation_cost_record(self):
        # spec 10.4: 종료 기록은 평가 비용을 요구하지 않는다
        root, folder = self.started_run()
        write_verdict(folder, 'claude-arm1-core01')
        self.assertFalse(list(root.rglob('evaluation-cost.jsonl')))
        code, out = finish(root, 'claude-arm1-core01')
        self.assertEqual(code, 0, out)
        self.assertNotIn('evaluation_cost', read_record(folder))


_FROZEN_WORLD = {}


def frozen_world():
    """A record tree frozen by scripts/freeze_readiness.py from three (fake-docker) smoke runs whose self-test reported
    RUNNER_TOOLS (arm 3: + Task). Made once; tests copy it. Returns the template root."""
    if 'root' not in _FROZEN_WORLD:
        freeze = load_script('freeze_readiness')
        tmp = tempfile.mkdtemp(prefix='frozen-world-')
        atexit.register(shutil.rmtree, tmp, True)
        root = make_root(tmp)
        (root / 'docs/results').mkdir(parents=True)
        for arm in (1, 2, 3):
            run_id = 'smoke%d' % arm
            init_run(root, run_id, arm, tools=RUNNER_TOOLS + (('Task',) if arm == 3 else ()))
            (arm3_turn(root, run_id) if arm == 3 else first_turn(root, run_id, arm))
        argv = ['freeze_readiness.py', '--runs', str(root / '.runs'), '--output', str(root / 'docs/results/readiness.json'),
                '--smoke-run', 'smoke1', '--smoke-run', 'smoke2', '--smoke-run', 'smoke3']
        out = io.StringIO()
        with patch.object(sys, 'argv', argv), \
             patch.object(freeze, 'images', return_value=({'payment-claude-runner:2.1.326': FROZEN_IMAGE}, [])), \
             patch.object(freeze, 'clean_baseline', return_value=('f' * 40, [])), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = freeze.main()
            except SystemExit as exc:
                code = exc.code
        if code not in (0, None) or not (root / 'docs/results/readiness.json').exists():
            raise AssertionError('freeze for the fixture failed: %r %s' % (code, out.getvalue()))
        _FROZEN_WORLD['root'] = root
    return _FROZEN_WORLD['root']


class EvidenceFieldsComeOnlyFromTheirSources(FinishFixture):
    """Spec 10.2: commit_run.py가 PASSED에 요구하는 네 칸은 종료 기록이 아래 원천에서만 쓴다. 원천이 없거나 거짓이면 그 칸은
    false이고, 손으로 true를 쓰는 경로는 없다.
    all_acceptance_passed ← 판정표 / usage_complete ← 실행 폴더에서 그 자리에서 계산한 run 장부 / all_role_models_verified ←
    manifest와 요청 이벤트, 그 조건에 있어야 할 역할이 관측되지 않으면 false / isolation_verified ← 그 run의 self-test 통과 +
    그 run의 도구 목록이 동결 파일에 기록된 그 조건의 도구 목록과 같음."""

    HAND_WRITTEN = dict(all_acceptance_passed=True, usage_complete=True, all_role_models_verified=True, isolation_verified=True)

    def frozen_run(self, run_id, arm, tools):
        root = self.base / 'world'
        shutil.copytree(frozen_world(), root, symlinks=True)
        init_run(root, run_id, arm, image=None, tools=tools, readiness=root / 'docs/results/readiness.json')
        return root

    def fields(self, root, run_id, folder, preset=None, **verdict):
        """verdict -> normalize -> capture_turn -> (run.json says `preset`) -> finish_run; returns the finished record."""
        write_verdict(folder, run_id, **verdict)
        close_turns(root, run_id)
        if preset:
            run = read_record(folder)
            run.update(preset)
            (folder / 'run.json').write_text(json.dumps(run))
        code, out = finish(root, run_id)
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertEqual(run['status'], 'PASSED')
        return run

    def test_control_all_four_true_from_their_sources(self):
        root = self.frozen_run('claude-arm1-core01', 1, RUNNER_TOOLS)
        first_turn(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        run = self.fields(root, 'claude-arm1-core01', folder)
        for field in self.HAND_WRITTEN:
            self.assertIs(run[field], True, field)

    def test_values_written_into_run_json_beforehand_do_not_survive(self):
        # no freeze in the manifest, the turn's usage unobserved, the implementer served another model
        root = make_root(self.base / 'pre')
        init_run(root, 'claude-arm1-core01', 1)
        drive_turn(root, 'claude-arm1-core01', reported=None,
                   projects={'-workspace/s.jsonl': [assistant('x', FABLE, U1)]})
        folder = write_record(root, 'claude-arm1-core01', 1)
        write_verdict(folder, 'claude-arm1-core01', results={'C03': 'FAIL'})
        close_turns(root, 'claude-arm1-core01')
        run = read_record(folder)
        run.update(self.HAND_WRITTEN)                                    # the record says all four are true at finish time
        (folder / 'run.json').write_text(json.dumps(run))
        code, out = finish(root, 'claude-arm1-core01')
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        for field in self.HAND_WRITTEN:
            self.assertIs(run[field], False, field)

    def test_usage_complete_is_computed_from_the_run_folder_at_finish(self):
        root = make_root(self.base / 'usage')
        init_run(root, 'claude-arm1-core01', 1)
        drive_turn(root, 'claude-arm1-core01', reported=U1, projects={'-workspace/s.jsonl': [
            assistant('ok', OPUS, U1), assistant('no-usage', OPUS, None)]})
        dest = root / '.runs/claude-arm1-core01'
        ledger = json.loads((dest / 'ledger.json').read_text()) if (dest / 'ledger.json').exists() else {}
        ledger['usage_complete'] = True                                     # a stale or edited ledger file says complete
        (dest / 'ledger.json').write_text(json.dumps(ledger))
        folder = write_record(root, 'claude-arm1-core01', 1)
        self.assertIs(self.fields(root, 'claude-arm1-core01', folder, preset=dict(usage_complete=True))['usage_complete'], False)

    def test_role_models_must_be_the_agreed_models(self):
        root = make_root(self.base / 'roles1')
        init_run(root, 'claude-arm1-core01', 1)
        first_turn(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        self.assertIs(self.fields(root, 'claude-arm1-core01', folder)['all_role_models_verified'], True)   # control
        root = make_root(self.base / 'roles2')
        init_run(root, 'claude-arm1-core01', 1)
        drive_turn(root, 'claude-arm1-core01', reported=U1, projects={'-workspace/s.jsonl': [assistant('f', FABLE, U1)]})
        folder = write_record(root, 'claude-arm1-core01', 1)
        self.assertIs(self.fields(root, 'claude-arm1-core01', folder,
                                  preset=dict(all_role_models_verified=True))['all_role_models_verified'], False)

    def arm3(self, name, *, reviewer=True, worker=True, proxy=True, proxy_model=OPUS):
        root = make_root(self.base / name)
        init_run(root, 'claude-arm3-core01', 3)
        arm3_turn(root, 'claude-arm3-core01', reviewer=reviewer, worker=worker)
        if proxy:
            code, console = proxy_call(root / '.runs', 'claude-arm3-core01', served_model=proxy_model)
            self.assertEqual(code, 0, console)
        folder = write_record(root, 'claude-arm3-core01', 3)
        return self.fields(root, 'claude-arm3-core01', folder, preset=dict(all_role_models_verified=True))['all_role_models_verified']

    def test_arm3_with_every_required_role_on_its_agreed_model_is_verified(self):
        # control: PM Fable, worker Opus, second reviewer Fable, user proxy Opus
        self.assertIs(self.arm3('all'), True)

    def test_arm3_without_a_required_role_is_not_verified(self):
        # 그 조건에 있어야 할 역할이 관측되지 않으면 false
        self.assertIs(self.arm3('no-reviewer', reviewer=False), False)
        self.assertIs(self.arm3('no-worker', worker=False), False)

    def test_arm3_without_the_user_proxy_is_not_verified(self):
        # decision (b): the user proxy is a required arm-3 role — the arm-3 interview is mandatory
        # (docs/experiment-plan.md "3만 의무 사전 인터뷰"; spec 2: 대리 사용자는 Opus 5.5다)
        self.assertIs(self.arm3('no-proxy', proxy=False), False)

    def test_arm3_user_proxy_served_by_another_model_is_not_verified(self):
        # spec 2: 대리 사용자는 Opus 5.5다 — checked on the proxy's request events
        self.assertIs(self.arm3('proxy-fable', proxy_model=FABLE), False)

    def test_isolation_needs_the_frozen_tool_list_of_that_arm(self):
        root = self.frozen_run('claude-arm1-core01', 1, RUNNER_TOOLS + ('Skill',))       # surface drifted from the freeze
        first_turn(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        self.assertIs(self.fields(root, 'claude-arm1-core01', folder, preset=dict(isolation_verified=True))['isolation_verified'],
                      False)

    def test_isolation_without_a_freeze_is_false(self):
        root = make_root(self.base / 'nofreeze')
        init_run(root, 'claude-arm1-core01', 1, tools=RUNNER_TOOLS)
        first_turn(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        self.assertIs(self.fields(root, 'claude-arm1-core01', folder, preset=dict(isolation_verified=True))['isolation_verified'],
                      False)


SELFTEST_FAILED = {'passed': False, 'selftest_failed': '1'}


class PreCallSelftestFailureIsRecorded(FinishFixture):
    """§0 격리 칸(2026-11-18): 호출 전 self-test가 실패해 턴이 시작되지 못한 경우도 run에 기록으로 남기고 격리 검사가 그것을
    읽는다. Spec 10.2 isolation_verified ← 그 run의 self-test가 통과. Runbook (실패한 턴의 복구): the record is not a turn —
    it does not enter turn numbers, the cap count or the ledger, and the next attempt is the kind originally declared."""

    RUN = 'claude-arm3-core01'
    frozen_run = EvidenceFieldsComeOnlyFromTheirSources.frozen_run
    fields = EvidenceFieldsComeOnlyFromTheirSources.fields

    def arm3_run(self):
        root = self.frozen_run(self.RUN, 3, RUNNER_TOOLS + ('Task',))
        arm3_turn(root, self.RUN)
        code, console = proxy_call(root / '.runs', self.RUN)
        self.assertEqual(code, 0, console)
        return root, root / '.runs' / self.RUN

    def next_feedback(self, root):
        lines = [assistant('pm-' + self.RUN, FABLE, U1), assistant('fb-pm', FABLE, U2)]
        result, turn_dir, error, _ = drive_turn(root, self.RUN, resume=True, kind='feedback', reported=U2,
                                                projects={'-workspace/s.jsonl': lines})
        self.assertIsNone(error)
        self.assertEqual(result['status'], 'ok')
        return turn_dir

    def turn_names(self, dest):
        return sorted(p.name for p in (dest / 'turns').iterdir())

    def test_control_arm3_with_passing_selftests_is_isolated(self):
        root, dest = self.arm3_run()
        self.next_feedback(root)
        folder = write_record(root, self.RUN, 3)
        run = self.fields(root, self.RUN, folder)
        self.assertIs(run['isolation_verified'], True)

    def test_failed_precall_selftest_makes_no_call_and_is_recorded_in_the_run(self):
        root, dest = self.arm3_run()
        before_turns = self.turn_names(dest)
        before_state = launch.limit_state(dest)
        before_ledger = launch.run_ledger(dest)
        error, paid = attempt_turn(root, self.RUN, resume=True, kind='feedback', selftest_report=SELFTEST_FAILED)
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        self.assertEqual(self.turn_names(dest), before_turns, 'the stopped attempt became a turn')
        recorded = [p for p in dest.rglob('*') if p.is_file() and 'selftest-failures-before-call' in p.parts]
        self.assertTrue(recorded, 'the failed pre-call self-test left no record in the run')
        state = launch.limit_state(dest)
        self.assertEqual(state['used']['feedback_limit'], before_state['used']['feedback_limit'])
        self.assertEqual(state['answer_turns'], before_state['answer_turns'])
        ledger = launch.run_ledger(dest)
        self.assertEqual(ledger['total_tokens'], before_ledger['total_tokens'])
        self.assertEqual(ledger['turn_kinds'], before_ledger['turn_kinds'])

    def test_next_attempt_after_the_failure_is_the_declared_kind_not_a_recovery(self):
        root, dest = self.arm3_run()
        attempt_turn(root, self.RUN, resume=True, kind='feedback', selftest_report=SELFTEST_FAILED)
        error, paid = attempt_turn(root, self.RUN, resume=True, kind='recovery')
        self.assertIsNotNone(error, 'a recovery was accepted after a self-test failure that started no turn')
        self.assertEqual(paid, [])
        turn_dir = self.next_feedback(root)
        self.assertEqual(turn_dir.name, '0002')
        inv = json.loads((turn_dir / 'invocation.json').read_text())
        self.assertEqual(inv['turn_kind'], 'feedback')
        self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 1)

    def test_recorded_failure_makes_isolation_false_even_after_a_good_turn(self):
        root, dest = self.arm3_run()
        attempt_turn(root, self.RUN, resume=True, kind='feedback', selftest_report=SELFTEST_FAILED)
        self.next_feedback(root)
        folder = write_record(root, self.RUN, 3)
        run = self.fields(root, self.RUN, folder, preset=dict(isolation_verified=True))
        self.assertIs(run['isolation_verified'], False)


class OneFinishPerRun(FinishFixture):
    """Spec 10.2: 종료 기록은 run당 한 번이다. 이미 종료 상태가 있는 기록에 다시 실행하면 거부한다. 정정이 필요하면
    --supersede --reason으로만 하고, 이전 run.json을 지우지 않고 옆에 보존한다. 종료 기록의 대상은 턴이 한 번 이상 나간 run이다.
    NOT_STARTED·SETUP_FAILED는 종료 기록이 거부한다."""

    def test_second_finish_is_refused(self):
        root, folder = self.started_run()
        write_verdict(folder, 'claude-arm1-core01', results={'C01': 'FAIL'})
        self.assertEqual(finish(root, 'claude-arm1-core01')[0], 0)
        first = (folder / 'run.json').read_bytes()
        write_verdict(folder, 'claude-arm1-core01')
        code, _ = finish(root, 'claude-arm1-core01')
        self.assertNotEqual(code, 0)
        self.assertEqual((folder / 'run.json').read_bytes(), first)

    def test_supersede_needs_a_reason_and_keeps_the_previous_record(self):
        root, folder = self.started_run()
        write_verdict(folder, 'claude-arm1-core01', results={'C01': 'FAIL'})
        self.assertEqual(finish(root, 'claude-arm1-core01')[0], 0)
        first = (folder / 'run.json').read_bytes()
        write_verdict(folder, 'claude-arm1-core01')                        # corrected judgement
        for options in (['--supersede'], ['--supersede', '--reason', ''], ['--reason', 'regraded']):
            with self.subTest(options):
                code, _ = finish(root, 'claude-arm1-core01', *options)
                self.assertNotEqual(code, 0)
                self.assertEqual((folder / 'run.json').read_bytes(), first)
        code, out = finish(root, 'claude-arm1-core01', '--supersede', '--reason', 'C01 regraded after re-reading the audit')
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertEqual(run['status'], 'PASSED')
        self.assertIn('C01 regraded after re-reading the audit', json.dumps(run, ensure_ascii=False))
        kept = [p for p in folder.iterdir() if p.name != 'run.json' and p.is_file() and p.read_bytes() == first]
        self.assertTrue(kept, 'the superseded run.json was not kept beside the new one')

    def test_not_started_and_setup_failed_records_are_refused(self):
        for status in ('NOT_STARTED', 'SETUP_FAILED'):
            with self.subTest(status):
                self.base = Path(self.tmp.name) / status
                root, folder = self.started_run(status=status)
                write_verdict(folder, 'claude-arm1-core01')
                before = (folder / 'run.json').read_bytes()
                code, _ = finish(root, 'claude-arm1-core01')
                self.assertNotEqual(code, 0)
                self.assertEqual((folder / 'run.json').read_bytes(), before)

    def test_run_without_any_turn_is_refused(self):
        root = make_root(self.base / 'noturn')
        init_run(root, 'claude-arm1-core01', 1)
        folder = write_record(root, 'claude-arm1-core01', 1)
        write_verdict(folder, 'claude-arm1-core01')
        before = (folder / 'run.json').read_bytes()
        code, _ = finish(root, 'claude-arm1-core01')
        self.assertNotEqual(code, 0)
        self.assertEqual((folder / 'run.json').read_bytes(), before)


class InvalidInfraNeedsReasonAndEvidence(FinishFixture):
    """Spec 10.2: INVALID_INFRA만 인자로 받는다(사유 필수). 사유와 근거 파일을 함께 기록한다."""

    def test_invalid_infra_without_reason_or_evidence_is_refused(self):
        cases = {'no evidence': ['--invalid-infra', 'docker daemon died'],
                 'empty reason': ['--invalid-infra', '', '--evidence', 'docker.log'],
                 'evidence file absent': ['--invalid-infra', 'docker daemon died', '--evidence', 'nowhere.log'],
                 'evidence outside the record folder': ['--invalid-infra', 'docker daemon died', '--evidence', '../../../outside.log']}
        for name, options in cases.items():
            with self.subTest(name):
                self.base = Path(self.tmp.name) / name.replace(' ', '-')
                root, folder = self.started_run()
                (folder / 'docker.log').write_text('daemon died')
                (folder / '../../../outside.log').resolve().write_text('x')
                before = (folder / 'run.json').read_bytes()
                code, _ = finish(root, 'claude-arm1-core01', *options)
                self.assertNotEqual(code, 0, name)
                self.assertEqual((folder / 'run.json').read_bytes(), before, name)

    def test_invalid_infra_with_reason_and_evidence_is_recorded(self):
        root, folder = self.started_run(**EvidenceFieldsComeOnlyFromTheirSources.HAND_WRITTEN)
        (folder / 'docker.log').write_text('daemon died')
        code, out = finish(root, 'claude-arm1-core01', '--invalid-infra', 'docker daemon died', '--evidence', 'docker.log')
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertEqual(run['status'], 'INVALID_INFRA')
        self.assertIs(run['all_acceptance_passed'], False)
        text = json.dumps(run)
        self.assertIn('docker daemon died', text)
        self.assertIn(hashlib.sha256(b'daemon died').hexdigest(), text)


class PilotRecordsAreSeparate(FinishFixture):
    """Spec 10.3: 파일럿은 본 실험과 run id·기록 위치가 다르다: run id claude-pilot-NN, 기록 claude/docs/pilots/<run id>/.
    init --pilot이 manifest에 표시한다. 기록 도구(capture_turn·finish_run)는 manifest의 표시로 위치를 정하고, 기록 폴더의 run id와
    실행 run id가 다르면 거부한다."""

    def pilot_root(self, run_id='claude-pilot-01', arm=3):
        root = make_root(self.base / run_id)
        (root.parent / 'recording').symlink_to(HERE.parents[1] / 'recording')      # the shared recorder capture_turn uses
        manifest = init_run(root, run_id, arm, pilot=True)
        self.assertIs(manifest['pilot'], True)
        (arm3_turn(root, run_id) if arm == 3 else first_turn(root, run_id, arm))
        return root

    def capture(self, root, run_id):
        analysis = root.parent / 'analysis.md'
        analysis.write_text('turn analysis')
        with patch.object(CAPTURE, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                CAPTURE.capture(run_id, 1, analysis, 'RUNNING', runs=root / '.runs')
                return None
            except BaseException as exc:
                return exc

    def test_pilot_ids_and_the_pilot_flag_go_together(self):
        root = make_root(self.base / 'ids')
        for run_id, options in (('claude-arm1-core01', dict(pilot=True)), ('claude-pilot-02', {}),
                                ('claude-pilot-03', dict(pilot=False))):
            with self.subTest(run_id=run_id, **options):
                with self.assertRaises(Exception):
                    init_run(root, run_id, 1, **options)
                self.assertFalse((root / '.runs' / run_id / 'manifest.json').exists())

    def test_pilot_is_recorded_under_the_pilot_location_only(self):
        root = self.pilot_root()
        self.assertIsNone(self.capture(root, 'claude-pilot-01'))
        folder = record_folder(root, 'claude-pilot-01', 3, pilot=True)
        self.assertTrue((folder / 'run.json').exists())
        write_verdict(folder, 'claude-pilot-01', results={'C20': 'NOT_RUN'})
        code, out = finish(root, 'claude-pilot-01')
        self.assertEqual(code, 0, out)
        self.assertEqual(read_record(folder)['run_id'], 'claude-pilot-01')
        self.assertEqual(read_record(folder)['status'], 'FAILED')
        experiments = root / 'docs' / 'experiments'
        self.assertEqual([p for p in experiments.rglob('*') if p.is_file()] if experiments.exists() else [], [])

    def test_record_folder_of_another_run_is_refused(self):
        root = self.pilot_root()
        folder = record_folder(root, 'claude-pilot-01', 3, pilot=True)
        folder.mkdir(parents=True)
        (folder / 'run.json').write_text(json.dumps({'run_id': 'claude-pilot-09', 'status': 'RUNNING'}))
        before = (folder / 'run.json').read_bytes()
        self.assertIsNotNone(self.capture(root, 'claude-pilot-01'))
        self.assertEqual((folder / 'run.json').read_bytes(), before)
        write_verdict(folder, 'claude-pilot-01')
        code, _ = finish(root, 'claude-pilot-01')
        self.assertNotEqual(code, 0)
        self.assertEqual((folder / 'run.json').read_bytes(), before)

    def test_main_record_folder_of_another_run_is_refused(self):
        root, folder = self.started_run()
        (folder / 'run.json').write_text(json.dumps({'run_id': 'claude-arm1-core02', 'arm': 1, 'core': 1, 'status': 'RUNNING'}))
        before = (folder / 'run.json').read_bytes()
        write_verdict(folder, 'claude-arm1-core01')
        code, _ = finish(root, 'claude-arm1-core01')
        self.assertNotEqual(code, 0)
        self.assertEqual((folder / 'run.json').read_bytes(), before)


class InitRefusesANamedReadinessThatDoesNotExist(unittest.TestCase):
    """Spec 10.5 (U1): 동결 파일 경로를 명시했는데 파일이 없으면 init은 거부한다. 동결 전 실행은 기본 위치에 동결 파일이 없을 때만
    된다."""

    def test_named_missing_readiness_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaises(Exception):
                init_run(root, 'claude-arm1-core01', 1, image=None, readiness=Path(tmp) / 'results' / 'readiness.json')
            self.assertFalse((root / '.runs' / 'claude-arm1-core01' / 'manifest.json').exists())

    def test_default_readiness_present_means_the_run_is_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            default = root / 'docs' / 'results'
            default.mkdir(parents=True)
            write_readiness(default).rename(default / 'readiness.json')
            m = init_run(root, 'claude-arm1-core01', 1, image=None)
            self.assertEqual(m['image'], FROZEN_IMAGE)
            self.assertIs(m['image_frozen'], True)
            with self.assertRaises(Exception):
                init_run(root, 'claude-arm2-core01', 2, image='img')            # no pre-freeze run once the freeze exists

    def test_pre_freeze_run_only_without_the_default_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = init_run(make_root(tmp), 'claude-arm1-core01', 1)
            self.assertIs(m['image_frozen'], False)
            self.assertIsNone(m['readiness'])


class RecoveryResendsTheSamePrompt(unittest.TestCase):
    """Spec 10.5 (U9): recovery 턴은 이어 가는 턴과 같은 프롬프트(해시 일치)만 보낸다 — refused before any paid call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recovery_with_another_prompt_makes_no_paid_call(self):
        for arm in (1, 3):
            with self.subTest(arm=arm):
                run_id = 'rp%d' % arm
                root, dest = new_run(self.base, run_id, arm, ['initial', ('feedback', 'failed')])
                error, paid = attempt_turn(root, run_id, resume=True, kind='recovery',
                                           prompt_text=last_prompt(root, run_id) + ' (and one more thing)')
                self.assertIsNotNone(error)
                self.assertEqual(paid, [])
                _, paid = attempt_turn(root, run_id, resume=True, kind='recovery')     # control: the same prompt
                self.assertTrue(paid)

    def test_recovery_of_a_turn_whose_prompt_hash_is_not_recorded_makes_no_paid_call(self):
        # runbook 상한 표: 다르거나 해시가 없으면 호출 전 거부
        root, dest = new_run(self.base, 'nohash', 1, ['initial', ('feedback', 'failed')])
        inv_path = sorted((dest / 'turns').iterdir())[-1] / 'invocation.json'
        inv = json.loads(inv_path.read_text())
        del inv['prompt_sha256']
        inv_path.write_text(json.dumps(inv))
        error, paid = attempt_turn(root, 'nohash', resume=True, kind='recovery')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])

    def test_recovery_after_a_real_failed_turn_needs_its_prompt(self):
        # the failed turn is a real (fake-docker) turn: its own record carries the hash the recovery is checked against
        root, dest = new_run(self.base, 'real', 1, [])
        first_turn(root, 'real', 1)
        def container_died(dest):
            raise RuntimeError('container died during the call')
        result, turn_dir, error, _ = drive_turn(root, 'real', resume=True, kind='feedback', reported=U2, side_effect=container_died,
                                                projects={'-workspace/s.jsonl': [assistant('r2', OPUS, U2)]})
        self.assertNotEqual(result['status'], 'ok', 'control: the fake call left a failed turn')
        sent = (turn_dir / 'prompt.txt').read_text()
        error, paid = attempt_turn(root, 'real', resume=True, kind='recovery', prompt_text=sent + ' changed')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        _, paid = attempt_turn(root, 'real', resume=True, kind='recovery', prompt_text=sent)
        self.assertTrue(paid)


class ProxyRunsOnlyOnThePinnedImage(unittest.TestCase):
    """Spec 10.5 (U10): 대리 사용자 호출은 그 run의 manifest에 고정된 이미지와 같은 이미지로만 실행한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def images_used(self, record, candidates):
        return {c for cmd, _ in record if cmd[:2] in (['docker', 'create'], ['docker', 'run']) for c in candidates if c in cmd}

    def test_proxy_uses_the_image_pinned_in_the_manifest(self):
        root, dest = new_run(self.base, 'pin', 3, ['initial'])
        pinned = json.loads((dest / 'manifest.json').read_text())['image']
        record = []
        code, console = proxy_call(root / '.runs', 'pin', record=record)
        self.assertEqual(code, 0, console)
        # proxy_call's readiness names another runner digest ('sha256:frozen'); only the run's pinned image is run
        self.assertEqual(self.images_used(record, {pinned, 'sha256:frozen', 'payment-claude-runner:2.1.326'}), {pinned})
        invocation = json.loads((sorted((dest / 'consults').iterdir())[-1] / 'invocation.json').read_text())
        self.assertEqual(invocation.get('image'), pinned)

    def test_frozen_run_proxy_uses_the_frozen_digest(self):
        root = make_root(self.base / 'frozen')
        readiness = write_readiness(self.base)
        init_frozen(root, 'fz', 3, readiness)
        write_turns(root / '.runs/fz', [(datetime.now(timezone.utc), 'ok', [('t', 'pm', tokens(1, 1))], 'initial')])
        record = []
        code, console = proxy_call(root / '.runs', 'fz', record=record)
        self.assertEqual(code, 0, console)
        used = self.images_used(record, {FROZEN_IMAGE, 'sha256:frozen', 'img', 'payment-claude-runner:2.1.326'})
        self.assertEqual(used, {FROZEN_IMAGE})

    def test_manifest_without_a_pinned_image_gets_no_proxy_call(self):
        root, dest = new_run(self.base, 'nopin', 3, ['initial'])
        manifest = json.loads((dest / 'manifest.json').read_text())
        del manifest['image']
        (dest / 'manifest.json').write_text(json.dumps(manifest))
        record = []
        code, _ = proxy_call(root / '.runs', 'nopin', record=record)
        self.assertNotEqual(code, 0)
        self.assertEqual(paid_calls(record), [])


if __name__ == '__main__':
    unittest.main()
