"""Spec-derived invariant tests for runner/launch.py (no docker, no model call, no network).

Source of every expectation: docs/plans/2026-11-17/claude-only-run/requirement-spec.md, sections 2 and 5.
Each test names the spec sentence it checks. Expected values come from the spec, not from the implementation.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
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


def init_run(root, run_id, arm, harness='default', seed=None):
    if harness == 'default':
        harness = root / 'harness-source' if arm == 3 else None
    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, arm=arm, core=1, image='img', harness=harness, network=None)
    seeder = seed or patch.object(launch, 'seed_harness', return_value='listing')
    with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
         patch.object(launch, 'selftest', return_value=SELFTEST_OK), seeder, contextlib.redirect_stdout(io.StringIO()):
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


def env_values(cmd):
    return dict(cmd[i + 1].split('=', 1) for i, arg in enumerate(cmd) if arg == '--env' and '=' in cmd[i + 1])


def drive_turn(root, run_id, *, resume=False, kind=None, reported=None, projects=None, projects_rc=0, stderr=b'',
               reviewer_log=None, reviewer_projects=None, side_effect=None, stdout_extra=b'', timeout=5, record=None):
    """Run launch.turn with a fake docker. projects: {relative path: [line dict or raw str]}.
    kind: the declared turn kind of a resumed turn (--kind); a resumed turn without one is the caller's choice to test."""
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
         patch.object(launch, 'session_exists', return_value=resume), patch.object(launch, 'selftest', return_value=SELFTEST_OK), \
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


def write_turn(dest, i, started, status, events, kind, counts_as):
    """One turn record as the runner leaves it: invocation.json carries turn_kind (+counts_as)."""
    d = dest / 'turns' / ('%04d' % i)
    d.mkdir(parents=True)
    invocation = {'started_at': started.isoformat()}
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
    A 'recovery' turn counts as the kind of the turn it continues (the previous one)."""
    previous = None
    for i, turn in enumerate(turns, 1):
        started, status, events = turn[:3]
        kind = turn[3] if len(turn) > 3 else ('initial' if i == 1 else 'feedback')
        counts_as = previous if kind == 'recovery' else kind
        write_turn(dest, i, started, status, events, kind, counts_as)
        previous = counts_as


def write_consults(dest, consults):
    for i, events in enumerate(consults, 1):
        d = dest / 'consults' / ('%04d' % i)
        d.mkdir(parents=True)
        (d / 'usage.json').write_text(json.dumps({'role': 'user-proxy', 'usage_complete': True, 'cost_usd': 0.0}))
        (d / 'response.txt').write_text('답변 %d' % i)
        (d / 'events.jsonl').write_text(''.join(json.dumps({'id': rid, 'role': role, 'usage': u, 'model': OPUS, 'turn': 'proxy'}) + '\n'
                                                for rid, role, u in events))


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


def attempt_turn(root, run_id, *, resume, kind, session=None):
    """Call launch.turn with a docker stub that only records; returns (error, paid calls).
    A paid call is any `docker create`/`docker start` (the model call happens inside the started container).
    session: what the config volume says about the session transcript (default: exists iff resume)."""
    prompt = Path(root).parent / 'attempt.txt'
    prompt.write_text('next prompt')
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
         patch.object(launch, 'selftest', return_value=SELFTEST_OK), \
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
    """Spec 2: 진행자·판정 어댑터·대리 사용자의 토큰은 역할별로 분리 기록한다 / 중복 합산이 없다."""

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


if __name__ == '__main__':
    unittest.main()
