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
import threading
import time
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
# Spec 11 승인 ② (2026-11-18): 세 조건 공통 1억 5천만 토큰(캐시 포함) · 8시간 · 판정 피드백 30회 상한은 그대로.
TOKEN_CAP = 150_000_000
TIME_CAP_SECONDS = 8 * 3600
FEEDBACK_CAP = 30
HALF_CAP = TOKEN_CAP // 2


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
               selftest=None, total_cost=0.1, results=None, reviewer_raw=None, via_cli=False, image_id=None, hold=None,
               call_timeout=None, stops=None):
    """Run launch.turn with a fake docker. projects: {relative path: [line dict or raw str]}.
    kind: the declared turn kind of a resumed turn (--kind); a resumed turn without one is the caller's choice to test.
    selftest: optional side effect of the (no-cost) hook self-test, e.g. to let wall-clock time pass during it.
    total_cost: total_cost_usd of the stream result (None: the field is absent). results: several result events of one
    turn instead (list of dicts merged over a success result of the pinned session). reviewer_raw: {file name: dict|str}
    the reviewer volume's raw/ holds ({} = raw/ missing; None = the generic copy below). via_cli: go through
    `launch.py turn` argument parsing (timeout None = --timeout-seconds not given). image_id: the image id `docker inspect`
    reports for the turn's container (None: an empty inspect record).
    hold: seconds the started container keeps running after it wrote its stream (a running turn); a `docker stop`/`docker kill`
    of the container ends it early (exit 137). stops: list that receives each stop/kill command.
    call_timeout: callable(dest) run when the call's timeout fires — the start then raises TimeoutExpired."""
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

    stopped = threading.Event()

    def fake_run(cmd, **kw):
        if record is not None:
            record.append((list(cmd), kw.get('timeout')))
        if cmd[:2] == ['docker', 'create']:
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if cmd[:2] in (['docker', 'stop'], ['docker', 'kill']):
            if stops is not None:
                stops.append(list(cmd))
            stopped.set()
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if cmd[:2] == ['docker', 'start']:
            if results is not None:
                lines = b''.join(json.dumps(dict({'type': 'result', 'subtype': 'success', 'session_id': manifest['session_id']},
                                                 **r)).encode() + b'\n' for r in results)
            else:
                result = {'type': 'result', 'subtype': 'success', 'session_id': manifest['session_id']}
                if total_cost is not None:
                    result['total_cost_usd'] = total_cost
                if reported is not None:
                    result['usage'] = reported
                lines = json.dumps(result).encode() + b'\n'
            if hold is not None:                      # a running turn: its stream so far, then the result at the end
                kw['stdout'].write(stdout_extra)
                kw['stdout'].flush()
                if stopped.wait(hold):
                    kw['stderr'].write(stderr)
                    return SimpleNamespace(returncode=137)
                kw['stdout'].write(lines)
            else:
                kw['stdout'].write(stdout_extra + lines)
            kw['stderr'].write(stderr)
            if side_effect:
                side_effect(dest)
            if call_timeout is not None:
                call_timeout(dest)
                raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
            return SimpleNamespace(returncode=0)
        if cmd[:2] == ['docker', 'inspect']:
            if image_id is None:
                return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
            inspect = [{'Image': image_id, 'Config': {'Image': manifest.get('image'), 'Env': []}}]
            return SimpleNamespace(returncode=0, stdout=json.dumps(inspect).encode(), stderr=b'')
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
        if cmd[:2] == ['docker', 'cp'] and '/.fable-review/raw' in cmd[2] and reviewer_raw is not None:
            if not reviewer_raw:
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'Error: No such file or directory')
            target = Path(cmd[-1])
            target.mkdir(parents=True, exist_ok=True)
            for name, content in reviewer_raw.items():
                (target / name).write_text(content if isinstance(content, str) else json.dumps(content))
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if cmd[:2] == ['docker', 'cp']:
            if projects_rc:
                return SimpleNamespace(returncode=projects_rc, stdout=b'', stderr=b'copy failed')
            write_tree(cmd[-1], projects or {})
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    envf = env_file(Path(root).parent)
    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, env_file=envf, prompt=prompt,
                           resume=resume, kind=kind, dry_run=False, cache_policy='observed-cache', timeout_seconds=timeout,
                           max_turns=None, max_budget_usd=None)
    argv = ['launch.py', '--runs', str(root / '.runs'), 'turn', '--run-id', run_id, '--prompt', str(prompt),
            '--env-file', str(envf), '--cache-policy', 'observed-cache']
    if resume:
        argv += ['--resume', '--kind', kind]
    if timeout is not None:
        argv += ['--timeout-seconds', str(timeout)]
    error = None
    out, err = io.StringIO(), io.StringIO()
    with patch.object(launch.subprocess, 'run', side_effect=fake_run), \
         patch.object(launch, 'volume_listing', return_value={'rc': 0, 'entries': [], 'credentials_file_in_volume': False}), \
         patch.object(launch, 'session_exists', return_value=resume), selftest_patch(selftest), \
         patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if via_cli:
                code = launch.main()
                if code:
                    error = RuntimeError('launch.py turn exited %r' % code)
            else:
                launch.turn(args)
        except SystemExit as exc:             # a CLI refusal
            if exc.code:
                error = exc
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


READ_ONLY_TOOLS = {'Read', 'Grep', 'Glob'}         # spec 11.1 + runbook: the reviewer gets read-only tools only
REVIEW_TEXT = 'REVIEW: no findings'
STUB_CLI = r'''#!%(python)s
# Stand-in for the `claude` CLI the reviewer helper runs (no model call). Records what it was started with.
import json, os, pathlib, sys
base = pathlib.Path(%(base)r)
a = sys.argv[1:]
sid = a[a.index('--session-id') + 1] if '--session-id' in a and a.index('--session-id') + 1 < len(a) else None
log = base / 'cfg' / 'reviewer-calls.log'
logged = bool(sid) and log.is_file() and any(('started session=%%s' %% sid) in l for l in log.read_text().splitlines())
tree = {}
for top, dirs, files in os.walk('.', followlinks=False):
    for name in dirs + files:
        path = os.path.join(top, name)[2:]
        if os.path.islink(path):
            tree[path] = 'link'
        elif os.path.isdir(path):
            tree[path] = 'dir'
        else:
            with open(path, 'rb') as fh:
                tree[path] = 'file:' + fh.read(200).decode(errors='replace')
token = %(token)r
record = dict(argv=a, cwd=os.getcwd(), stdin=sys.stdin.read(), token_in_env=any(token in v for v in os.environ.values()),
              token_var=bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')), logged_before_start=logged, session=sid, tree=tree,
              config_dir=os.environ.get('CLAUDE_CONFIG_DIR'), via_drop=os.environ.get('LAB_SPEC_VIA_CAPABILITY_DROP'))
with open(base / 'calls.jsonl', 'a') as f:
    f.write(json.dumps(record) + '\n')
mode = (base / 'mode').read_text().strip()
zero = {'input_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 0}
used = {'input_tokens': 3, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 5, 'output_tokens': 7}
if mode == 'ok':
    out = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': %(text)r, 'session_id': sid,
           'total_cost_usd': 0.25, 'usage': used}
elif mode == 'empty':
    out = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '', 'session_id': sid, 'total_cost_usd': 0.01,
           'usage': used}
elif mode in ('error', 'error-exit-0'):
    out = {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'Not logged in', 'session_id': sid,
           'total_cost_usd': 0, 'usage': zero}
elif mode == 'leak-env':                # a PM-written prompt makes the reviewer return its own environment
    out = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': json.dumps(dict(os.environ)), 'session_id': sid,
           'total_cost_usd': 0.25, 'usage': used}
elif mode == 'slow':                    # a long review; the PM side may go away meanwhile
    (base / 'slow-started').write_text(sid or '')
    import time
    time.sleep(30)
    (base / 'slow-finished').write_text(sid or '')
    out = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': %(text)r, 'session_id': sid,
           'total_cost_usd': 0.25, 'usage': used}
else:                                   # 'garbage': no result at all
    print('not json at all')
    sys.exit(0)
print(json.dumps(out))
sys.exit(1 if mode == 'error' else 0)
'''


# Stand-in for `setpriv` (the helper's test-only `serve --setpriv` names it; the container start script never does). The
# helper must start the reviewer CLI only through it, asking it to clear the bounding, inheritable and ambient capability
# sets (runbook: `setpriv --bounding-set=-all --inh-caps=-all --ambient-caps=-all`). It records what it was asked to
# drop; in mode 'ok' it then runs the rest of its command line (the CLI) with a marker in the environment, in the other
# modes it fails the way a real `setpriv` without the privilege does (the CLI never starts).
SETPRIV_STANDIN = r'''#!%(python)s
import json, os, pathlib, sys
base = pathlib.Path(%(base)r)
a = sys.argv[1:]
mode_file = base / 'setpriv-mode'
mode = mode_file.read_text().strip() if mode_file.exists() else 'ok'
drops, i = {}, 0
while i < len(a) and a[i].startswith('-'):
    if a[i] == '--':
        i += 1
        break
    name, eq, value = a[i].partition('=')
    if not eq and name in ('--bounding-set', '--inh-caps', '--ambient-caps') and i + 1 < len(a):
        value, i = a[i + 1], i + 1
    drops[name] = value if (eq or name in ('--bounding-set', '--inh-caps', '--ambient-caps')) else True
    i += 1
command = a[i:]
with open(base / 'setpriv.jsonl', 'a') as f:
    f.write(json.dumps(dict(argv=a, drops=drops, command=command, mode=mode)) + '\n')
if mode == 'fail':
    sys.stderr.write('setpriv: setting bounding set: Operation not permitted\n')
    sys.exit(127)
if mode == 'fail-1':
    sys.stderr.write('setpriv: capng_apply failed\n')
    sys.exit(1)
if mode == 'exit-0-without-running':
    sys.exit(0)
if not command:
    sys.stderr.write('setpriv: no command\n')
    sys.exit(127)
os.environ['LAB_SPEC_VIA_CAPABILITY_DROP'] = '1'
os.execv(command[0], command)
'''


def frame(raw):
    import struct
    return struct.pack('>Q', len(raw)) + raw


def limit_file_size():
    # the helper copies the review mirror (spec 11.1-b); a helper without a size cap must not fill the disk in a test
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (320 << 20, 320 << 20))


class ReviewerHelperFixture(unittest.TestCase):
    """The arm 3 reviewer helper (`fable_review.py serve`) and its PM-side client (`fable_review.py request`, what
    `fable-review` runs) as local processes: the helper runs a stub CLI instead of `claude` (no model call), listens on a
    socket in a temporary directory and accepts the current uid. No docker is involved."""

    helper_model = FABLE
    use_setpriv_standin = True        # False: the helper uses the real `setpriv`, as the container start script does

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sockdir = tempfile.mkdtemp(prefix='fr')            # AF_UNIX paths are short; the scratch path may be long
        self.socket = os.path.join(self.sockdir, 's')
        self.allowed = self.base / 'allowed'
        (self.allowed / 'sub').mkdir(parents=True)
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.config = self.base / 'cfg'
        self.config.mkdir()
        (self.base / 'home').mkdir()
        (self.base / 'mode').write_text('ok')
        stub = self.base / 'claude-stub'
        stub.write_text(STUB_CLI % {'python': sys.executable, 'base': str(self.base), 'text': REVIEW_TEXT, 'token': TOKEN})
        os.chmod(stub, 0o755)
        standin = []
        if self.use_setpriv_standin:
            setpriv = self.base / 'setpriv-standin'
            setpriv.write_text(SETPRIV_STANDIN % {'python': sys.executable, 'base': str(self.base)})
            os.chmod(setpriv, 0o755)
            standin = ['--setpriv', str(setpriv)]
        self.helper_err = open(self.base / 'helper.stderr', 'wb')
        # the token reaches the helper through an inherited descriptor (serve --token-fd), never its environment
        token_r, token_w = os.pipe()
        os.write(token_w, TOKEN.encode())
        os.close(token_w)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, '-I', str(HERE / 'fable_review.py'), 'serve', '--socket', self.socket, '--model', self.helper_model,
                 '--config-dir', str(self.config), '--home', str(self.base / 'home'), '--cli', str(stub),
                 '--root', str(self.allowed), '--peer-uid', str(os.getuid()), '--token-fd', str(token_r)] + standin,
                env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')}, pass_fds=(token_r,),
                stdin=subprocess.DEVNULL, stdout=self.helper_err, stderr=self.helper_err, preexec_fn=limit_file_size)
        finally:
            os.close(token_r)
        for _ in range(200):
            if os.path.exists(self.socket) or self.proc.poll() is not None:
                break
            time.sleep(0.05)
        if not os.path.exists(self.socket):
            self.helper_err.flush()
            why = (self.base / 'helper.stderr').read_text()[-2000:]
            self.tearDown()
            self.fail('reviewer helper did not start: %s' % why)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.helper_err.close()
        shutil.rmtree(self.sockdir, ignore_errors=True)
        self.tmp.cleanup()

    def calls(self):
        path = self.base / 'calls.jsonl'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def drops(self):
        """What the setpriv stand-in was asked to do, one record per start."""
        path = self.base / 'setpriv.jsonl'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def client(self, cwd, prompt, extra=()):
        """The PM side: `fable_review.py request` with the helper's socket; prompt on stdin, cwd = the caller's cwd."""
        code = ('import sys; sys.path.insert(0, %r); import fable_review as f; f.SOCKET = %r; '
                'sys.exit(f.main(["request"] + %r))' % (str(HERE), self.socket, list(extra)))
        return subprocess.run([sys.executable, '-I', '-c', code], cwd=str(cwd), input=prompt.encode(), capture_output=True,
                              timeout=60)

    def send(self, payload):
        """A raw request on the socket, as any process of the PM's user could send it."""
        import socket as socketlib
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        s = socketlib.socket(socketlib.AF_UNIX)
        s.settimeout(30)
        try:
            s.connect(self.socket)
            s.sendall(frame(raw))                       # wire format (fable_review.py docstring): 8-byte length + JSON
            data = b''
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        try:
            return json.loads(data) if data else None
        except ValueError:
            return {'unparsed': data.decode(errors='replace')}

    def assert_refused(self, reply, what, code=94):
        self.assertIsNotNone(reply, what)
        self.assertEqual(reply.get('exit'), code, '%s: %r' % (what, reply))


class DualReviewArm3(ReviewerHelperFixture):
    """Spec 2: 하네스 조건의 리뷰는 Opus 워커와 Fable 5.2 두 리뷰어다(codex 자리 대체).
    Spec 11.1: 모델·도구·effort는 실행기가 정한 값으로 고정되고 PM이 바꿀 수 없다."""

    def test_arm3_reviewers_are_opus_worker_and_fable_second_reviewer_without_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            m = init_run(root, 'r3', 3)
            self.assertEqual(m['requested_worker_model'], OPUS)
            self.assertEqual(m['requested_reviewer_model'], FABLE)
            cmd = launch.docker_command(m, Path('/outside/claude.env'), 'n', resume=False)
            env = env_values(cmd)
            self.assertEqual(env.get('LAB_REVIEWER_MODEL'), FABLE)       # the runner fixes the reviewer model at container start
            self.assertNotIn('codex', ' '.join(cmd).lower())
        # the second reviewer entry point (helper started with the runner's model, PM-side client) really runs the Fable
        # model at effort high with read-only tools — and the PM cannot ask for anything else
        self.assertEqual(self.helper_model, env['LAB_REVIEWER_MODEL'])
        done = self.client(self.allowed / 'sub', 'review this')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(REVIEW_TEXT, done.stdout.decode())
        [call] = self.calls()
        argv = call['argv']
        self.assertEqual(argv[argv.index('--model') + 1], FABLE)
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertIn('--tools', argv)
        self.assertTrue(set(argv[argv.index('--tools') + 1].split(',')) <= READ_ONLY_TOOLS, argv)
        self.assertNotIn('codex', ' '.join(argv).lower())
        for override in ({'model': OPUS}, {'effort': 'low'}, {'tools': 'Bash,Write'}):
            self.assert_refused(self.send(dict({'cwd': str(self.allowed / 'sub'), 'prompt': 'x'}, **override)), override)
        self.assertEqual(len(self.calls()), 1)

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
    # finished when written: a turn record as the runner leaves it (started_at .. finished_at is its length)
    (d / 'result.json').write_text(json.dumps({'status': status, 'usage_complete': True,
                                               'finished_at': datetime.now(timezone.utc).isoformat()}))
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
                                'image_digests': {'runner': 'sha256:frozen'}, 'token_limit': TOKEN_CAP,
                                'time_limit_seconds': 28_800, 'feedback_limit': 30}))
    return path


def proxy_call(runs, run_id, outcome='ok', *, timeout_seconds=None, record=None, usage=U1, request_id='req-proxy',
               served_model=OPUS, total_cost=None):
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
            line = {'type': 'result', 'result': text, 'session_id': state['session'], 'usage': usage}
            if total_cost is not None:
                line['total_cost_usd'] = total_cost
            kw['stdout'].write(json.dumps(line).encode() + b'\n')
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
    """Spec 2: 상한: run당 토큰 · 8시간 · 피드백 30회. 먼저 닿으면 CENSORED_*로 끝내고 보존한다. 실패 run을 결과에서 지우지 않는다.
    Spec 11 승인 ②: 토큰 상한 = 세 조건 공통 1억 5천만 토큰(캐시 포함); 8시간·판정 피드백 30회는 그대로."""

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
        # TOKEN_CAP = pm 50% + worker 30% + reviewer 20% (each split input/output evenly)
        pm, worker = TOKEN_CAP // 2, TOKEN_CAP * 3 // 10
        reviewer = TOKEN_CAP - pm - worker
        self.assertEqual(pm + worker + reviewer, TOKEN_CAP)
        at_cap = [(self.now, 'ok', [('p', 'pm', tokens(pm // 2, pm - pm // 2)), ('w', 'worker', tokens(worker // 2, worker - worker // 2)),
                                    ('r', 'reviewer', tokens(reviewer // 2, reviewer - reviewer // 2))])]
        below = [(self.now, 'ok', [('p', 'pm', tokens(pm // 2, pm - pm // 2 - 1)), ('w', 'worker', tokens(worker // 2, worker - worker // 2)),
                                   ('r', 'reviewer', tokens(reviewer // 2, reviewer - reviewer // 2))])]
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
        token = make_capped_run(Path(self.tmp.name) / 'ct', [(self.now, 'ok', [('p', 'pm', tokens(HALF_CAP, TOKEN_CAP - HALF_CAP))])])
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
        dest = make_capped_run(Path(self.tmp.name) / 'done', [(self.now, 'ok', [('p', 'pm', tokens(HALF_CAP, TOKEN_CAP - HALF_CAP))])])
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
        write_turn(dest, 1, self.now, 'ok', [('big', 'implementer', tokens(TOKEN_CAP - 5_000_000, 5_000_000))], 'initial', 'initial')
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
    """Paid call time passes: the run's first call started at `started` and lasted until now (decision 2026-11-19: the
    time cap counts the calls' lengths — its measured call_seconds and its start..end stamps both say so)."""
    inv = dest / 'turns' / '0001' / 'invocation.json'
    data = json.loads(inv.read_text())
    data['started_at'] = started.isoformat()
    inv.write_text(json.dumps(data))
    res = dest / 'turns' / '0001' / 'result.json'
    if res.exists():
        now = datetime.now(timezone.utc)
        res.write_text(json.dumps(dict(json.loads(res.read_text()), finished_at=now.isoformat(),
                                       call_seconds=(now - started).total_seconds())))


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
        (d / 'usage.json').write_text(json.dumps({'role': 'user-proxy', 'usage_complete': True, 'call_seconds': 0}))
        error, paid = attempt_turn(root, 'hand', resume=True, kind='answer')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])


def turn_dir_hint(root, run_id):
    turns = Path(root) / '.runs' / run_id / 'turns'
    return str(len(list(turns.iterdir())) if turns.exists() else 0)


def init_frozen(root, run_id, arm, readiness, image=None, pilot=False):
    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, arm=arm, core=1, image=image,
                           harness=root / 'harness-source' if arm == 3 else None, network=None, readiness=readiness, pilot=pilot)
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
            init_run(root, run_id, 3, pilot=True)
        else:
            init_frozen(root, run_id, 3, readiness, pilot=True)
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
            self.pilot(root, 'claude-pilot-01', write_readiness(tmp))
            self.assertTrue(self.passed(root, 'claude-pilot-01', FROZEN_CONTRACT, FROZEN_IMAGE), 'control: the frozen pilot does not count')
            self.assertFalse(self.passed(root, 'claude-pilot-01', 'd' * 64, FROZEN_IMAGE), 'pilot under another contract counted')
            self.assertFalse(self.passed(root, 'claude-pilot-01', FROZEN_CONTRACT, 'sha256:' + '0' * 64), 'pilot on another image counted')

    def test_pilot_made_before_the_freeze_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            self.pilot(root, 'claude-pilot-02')                             # initialised without readiness, image 'img'
            self.assertFalse(self.passed(root, 'claude-pilot-02', FROZEN_CONTRACT, 'img'))
            self.assertFalse(self.passed(root, 'claude-pilot-02', FROZEN_CONTRACT, FROZEN_IMAGE))


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
        over = TOKEN_CAP + 1_000_000                       # more than the run cap alone
        code, out = self.record('conductor', over - 3_000_000, 1_000_000, 1_000_000, 1_000_000)
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
        launch.limit_gate(self.dest, next_is_feedback=True)          # 10M run + >cap evaluation: the next call still passes
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

    def test_a_conductor_session_that_ran_several_runs_lists_them_all_unallocated(self):
        # spec 10.4: 한 줄 = 진행자 세션 1개 ... 맡은 run id 목록; runbook: 여러 run을 맡은 세션은 한 줄에 --run-id를 모두 적고
        # per_run_split: "unallocated"로 남기며 run별로 나누지 않는다
        runs = ('claude-pilot-02', 'claude-pilot-03')
        code, out = self.record('conductor', 1000, 200, 300, 400, run_ids=runs, session='conductor-two-pilots')
        self.assertEqual(code, 0, out)
        [line] = self.lines()
        self.assertEqual(sorted(line['run_ids']), sorted(runs))
        self.assertEqual(line.get('per_run_split'), 'unallocated')
        self.assertEqual(line['total_tokens'], 1900)
        for run_id in runs:                                  # no per-run share is written anywhere on the line
            self.assertNotIn(run_id, json.dumps({k: v for k, v in line.items() if k != 'run_ids'}))
        code, _ = self.record('conductor', 1000, 200, 300, 400, run_ids=runs[1:], session='conductor-two-pilots')
        self.assertNotEqual(code, 0)                         # the same session is not split into a line per run
        self.assertEqual(len(self.lines()), 1)

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
        # TOKEN_CAP - 20 before the final call; the final turn adds 1,000 -> 980 over the cap
        root, dest, folder = self.setup_run(run_id, arm, tokens(HALF_CAP - 10, TOKEN_CAP - HALF_CAP - 10))
        self.assertFalse((dest / 'censored.json').exists())
        self.final_call(root, run_id, arm, tokens(500, 500))
        return root, dest, folder

    def time_after_turn(self, run_id, arm=1):
        start = self.now - timedelta(hours=8) + timedelta(seconds=60)
        root, dest, folder = self.setup_run(run_id, arm, tokens(1, 1), started=start)
        self.final_call(root, run_id, arm, U2, side_effect=lambda d: age_first_turn(d, start - timedelta(seconds=90)))
        return root, dest, folder

    def token_after_proxy(self, run_id, arm=3):
        root, dest, folder = self.setup_run(run_id, arm, tokens(HALF_CAP - 10, TOKEN_CAP - HALF_CAP - 10))
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
        self.assertIn(980, numbers_in(read_record(folder)['cap_overshoot']), 'token overshoot (TOKEN_CAP + 980 - TOKEN_CAP) not recorded')

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
        first_turn(root, 'claude-arm1-core01', 1, usage=tokens(HALF_CAP - 10, TOKEN_CAP - HALF_CAP - 10))
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
        # this fixture's freeze carries no attested flags and no pilot: the start gate of a measured init would refuse it.
        # These tests are about the finish evidence; the start gate itself is MeasuredInitPassesTheStartGate.
        with patch.object(launch, 'measured_start_check'):
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
            # (not a measured run id: a measured init under a freeze also passes the start gate — MeasuredInitPassesTheStartGate)
            m = init_run(root, 'frozen-arm1', 1, image=None)
            self.assertEqual(m['image'], FROZEN_IMAGE)
            self.assertIs(m['image_frozen'], True)
            with self.assertRaises(Exception):
                init_run(root, 'frozen-arm2', 2, image='img')                   # no pre-freeze run once the freeze exists

    def test_pre_freeze_run_only_without_the_default_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = init_run(make_root(tmp), 'claude-arm1-core01', 1)
            self.assertIs(m['image_frozen'], False)
            self.assertIsNone(m['readiness'])


class MeasuredInitPassesTheStartGate(unittest.TestCase):
    """requirement-spec "파일럿 게이트 결정" 메인 정리: 본 실험 run의 init도 같은 개시 게이트를 본다 — pilot_passed가 false인
    동결에서는 사이클 1의 3번 조건 run만, true면 모두. 파일럿·재동결 스모크 경로는 그대로."""

    FLAGS = ('isolation_verified', 'usage_collector_verified', 'harness_adapter_verified', 'acceptance_suite_verified',
             'oauth_token_source_verified', 'role_models_verified', 'cache_observation_verified', 'hook_enforcement_verified')

    def world(self, tmp, pilot_passed, **flags):
        root = make_root(tmp)
        (root / '3/core2').mkdir(parents=True)
        (root / '3/core2/Main.kt').write_text('starter')
        for core in (1, 2):
            for arm in (1, 2, 3):
                path = root / 'docs/experiments' / str(arm) / ('core%d' % core) / 'run.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'run_id': 'claude-arm%d-core%02d' % (arm, core), 'arm': arm, 'core': core,
                                            'status': 'NOT_STARTED', 'requested_worker_model': OPUS if arm == 3 else None}))
        ready = {'protocol': 'payment-settlement-v3-claude', 'provider': 'claude-code', 'auth': 'subscription-oauth-token',
                 'reasoning_effort': 'high', 'cache_policy': 'observed-cache', 'billed_cost': None, 'baseline_sha': 'abc',
                 'contract_sha256': FROZEN_CONTRACT, 'image_digests': {'runner': FROZEN_IMAGE}, 'token_limit': 150000000,
                 'time_limit_seconds': 28800, 'feedback_limit': 30, 'user_proxy_model': OPUS,
                 'actual_models': {'1': OPUS, '2': OPUS, '3': FABLE}, 'pilot_passed': pilot_passed,
                 **{f: True for f in self.FLAGS}}
        ready.update(flags)
        (root / 'docs/results').mkdir(parents=True)
        (root / 'docs/results/readiness.json').write_text(json.dumps(ready))
        return root

    def init(self, root, run_id, arm, core=1, **options):
        return init_run(root, run_id, arm, image=None, core=core, **options)

    def test_false_freeze_inits_only_arm3_of_cycle1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.world(tmp, False)
            self.assertEqual(self.init(root, 'claude-arm3-core01', 3)['image'], FROZEN_IMAGE)
            for run_id, arm, core in (('claude-arm1-core01', 1, 1), ('claude-arm2-core01', 2, 1), ('claude-arm3-core02', 3, 2)):
                with self.subTest(run_id), self.assertRaisesRegex(ValueError, 'start gate refuses .*pilot_passed'):
                    self.init(root, run_id, arm, core)
                self.assertFalse((root / '.runs' / run_id).exists())

    def test_passed_freeze_inits_every_arm_and_other_flags_still_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.world(tmp, True)
            self.init(root, 'claude-arm1-core01', 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.world(tmp, False, isolation_verified=False)
            with self.assertRaisesRegex(ValueError, 'isolation_verified'):
                self.init(root, 'claude-arm3-core01', 3)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.world(tmp, True)
            with self.assertRaisesRegex(ValueError, 'does not name arm'):
                self.init(root, 'claude-arm1-core01', 2)                         # the id must name its arm and core

    def test_pilot_and_other_runs_are_not_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.world(tmp, False, isolation_verified=False)
            self.assertIs(self.init(root, 'claude-pilot-07', 3, pilot=True)['pilot'], True)
            self.init(root, 'probe-1', 1)


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


# ---------------------------------------------------------------------------------------------------------------------
# Spec 11 (2026-11-18, after pilot 01). Expectations come from requirement-spec.md §11 (with §2, §5, §10) and the
# interface names in claude/docs/runbook.md and runner/README.md.
# ---------------------------------------------------------------------------------------------------------------------

class ReviewerRequestSurface(ReviewerHelperFixture):
    """Spec 11.1: 요청으로 받는 것은 작업 디렉터리와 프롬프트뿐이다. 모델·도구·effort는 실행기가 정한 값으로 고정되고 PM이
    바꿀 수 없다. 도우미는 임의 명령 실행기가 아니다. (runbook: 94 요청 거부 — 작업 디렉터리가 허용 밖, 빈 프롬프트, 형식 밖 요청)"""

    def ok_request(self, **extra):
        return dict({'cwd': str(self.allowed / 'sub'), 'prompt': 'review the design'}, **extra)

    def test_a_request_of_directory_and_prompt_runs_the_reviewer_there_with_that_prompt(self):
        reply = self.send(self.ok_request())
        self.assertEqual(reply.get('exit'), 0, reply)
        self.assertIn(REVIEW_TEXT, reply.get('stdout', ''))
        [call] = self.calls()
        # spec 11 보강 1-b: the reviewer runs in the helper's snapshot copy of that directory, not in it
        self.assertNotEqual(Path(call['cwd']).resolve(), (self.allowed / 'sub').resolve())
        self.assertEqual(call['stdin'], 'review the design')
        self.assertNotIn('review the design', ' '.join(call['argv']))         # the prompt is data, never an argument
        # spec 11 보강 1-a: 토큰은 리뷰어 CLI와 도우미의 프로세스 환경에도 남지 않는다
        self.assertFalse(call['token_in_env'], 'the token is in the reviewer CLI environment')

    def test_the_pm_cannot_set_model_tools_effort_or_anything_else_through_the_request(self):
        extras = [{'model': OPUS}, {'effort': 'low'}, {'tools': 'Bash,Edit,Write'}, {'allowedTools': 'Bash'},
                  {'cli': '/bin/sh'}, {'command': ['bash', '-c', 'id']}, {'argv': ['--model', OPUS]}, {'args': ['-c']},
                  {'config_dir': str(self.outside)}, {'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'x'}}, {'session_id': 'x'},
                  {'output_format': 'stream-json'}, {'resume': 'x'}]
        for extra in extras:
            with self.subTest(extra=sorted(extra)):
                self.assert_refused(self.send(self.ok_request(**extra)), extra)
        self.assertEqual(self.calls(), [], 'a request with more than directory and prompt reached the CLI')

    def test_requests_not_of_the_one_accepted_form_are_refused(self):
        sub = str(self.allowed / 'sub')
        cases = {'no prompt': {'cwd': sub}, 'no directory': {'prompt': 'x'}, 'prompt not text': {'cwd': sub, 'prompt': ['x']},
                 'directory not text': {'cwd': [sub], 'prompt': 'x'}, 'a list': [sub, 'x'], 'not json': b'cwd=/tmp prompt=x',
                 'empty': b''}
        for name, payload in cases.items():
            with self.subTest(name):
                reply = self.send(payload)
                if reply is not None:
                    self.assertNotEqual(reply.get('exit'), 0, '%s accepted: %r' % (name, reply))
        self.assertEqual(self.calls(), [])

    def test_a_directory_outside_the_allowed_roots_is_refused(self):
        link = self.allowed / 'escape'
        link.symlink_to(self.outside, target_is_directory=True)
        cases = {'outside': str(self.outside), 'dot-dot escape': str(self.allowed / '..' / 'outside'),
                 'symlink out of the root': str(link), 'missing directory': str(self.allowed / 'missing'),
                 'relative path': 'sub', 'a file, not a directory': str(self.base / 'mode'), 'filesystem root': '/'}
        for name, cwd in cases.items():
            with self.subTest(name):
                self.assert_refused(self.send({'cwd': cwd, 'prompt': 'x'}), name)
        done = self.client(self.outside, 'review this')                      # the client sends its own cwd
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_an_empty_prompt_is_refused(self):
        self.assert_refused(self.send(self.ok_request(prompt='')), 'empty prompt')
        done = self.client(self.allowed / 'sub', '')
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_the_helper_only_ever_runs_the_fixed_reviewer_command(self):
        hostile = ['--model %s --effort low --tools Bash' % OPUS, '$(touch %s)' % (self.base / 'pwned'),
                   '"; touch %s; echo "' % (self.base / 'pwned'), '-c\nid', 'normal review prompt']
        for prompt in hostile:
            self.assertEqual(self.send(self.ok_request(prompt=prompt)).get('exit'), 0)
        self.assertEqual(self.client(self.allowed / 'sub', 'via the client').returncode, 0)
        calls = self.calls()
        self.assertEqual(len(calls), len(hostile) + 1)
        self.assertFalse((self.base / 'pwned').exists(), 'a prompt was executed as a command')

        def shape(argv):
            argv = list(argv)
            argv[argv.index('--session-id') + 1] = '<session>'
            return argv
        shapes = {json.dumps(shape(c['argv'])) for c in calls}
        self.assertEqual(len(shapes), 1, 'the command line varies with the request: %s' % shapes)
        argv = calls[0]['argv']
        self.assertEqual(argv[argv.index('--model') + 1], FABLE)
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertTrue(set(argv[argv.index('--tools') + 1].split(',')) <= READ_ONLY_TOOLS)
        self.assertEqual(len({c['session'] for c in calls}), len(calls), 'every call is a fresh session')
        self.assertEqual([c['stdin'] for c in calls], hostile + ['via the client'])


class ReviewerKeptProperties(ReviewerHelperFixture):
    """Spec 11.1: 호출마다 시도를 먼저 기록하는 기존 성질(중단돼도 증거가 남음)을 유지한다.
    runbook: 0 성공, 96 빈/오류 리뷰, 97 시도 기록 불가(호출 거부), 98 리뷰어 없음(1·2번), 2 인자 있음."""

    def test_each_attempt_is_recorded_with_its_session_before_the_cli_starts(self):
        for _ in range(2):
            self.assertEqual(self.client(self.allowed / 'sub', 'review').returncode, 0)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertTrue(call['session'])
            self.assertTrue(call['logged_before_start'], 'attempt %s not in the log when the CLI started' % call['session'])
        log = (self.config / 'reviewer-calls.log').read_text()
        for call in calls:
            self.assertRegex(log, r'started session=%s model=%s' % (call['session'], FABLE))

    def test_an_attempt_that_cannot_be_recorded_is_refused(self):
        log = self.config / 'reviewer-calls.log'
        if log.exists():
            log.unlink()
        log.mkdir()                                                    # nothing can be appended to it
        done = self.client(self.allowed / 'sub', 'review')
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.returncode, 97)
        self.assertEqual(self.calls(), [], 'the reviewer CLI ran although its attempt was not recorded')

    def test_an_empty_or_error_review_is_a_failure_with_a_nonzero_exit(self):
        # 'error': the CLI also exits non-zero; the others exit 0, so only the helper's check stands between them and a
        # silent success (runbook: 96 빈/오류 리뷰)
        for mode in ('empty', 'error', 'error-exit-0', 'garbage'):
            with self.subTest(mode):
                (self.base / 'mode').write_text(mode)
                done = self.client(self.allowed / 'sub', 'review')
                self.assertNotEqual(done.returncode, 0, '%s review passed as a success' % mode)
                if mode != 'error':
                    self.assertEqual(done.returncode, 96)
        (self.base / 'mode').write_text('ok')
        self.assertEqual(self.client(self.allowed / 'sub', 'review').returncode, 0)        # control

    def test_the_wrapper_takes_no_arguments(self):
        for args in (['--model', OPUS], ['x'], ['--']):
            with self.subTest(args=args):
                done = subprocess.run(['bash', str(HERE / 'fable-review.sh')] + args, input=b'review', capture_output=True,
                                      cwd=str(self.allowed / 'sub'), timeout=60,
                                      env={'PATH': os.environ['PATH'], 'FABLE_REVIEW_LIB': str(HERE)})
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(done.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_without_a_helper_there_is_no_reviewer(self):
        # arms 1/2 start no helper: the client finds no socket and refuses (98) instead of reviewing
        if os.path.exists('/run/fable-review/socket'):
            self.skipTest('this host has a reviewer socket at the in-container path')
        done = subprocess.run(['bash', str(HERE / 'fable-review.sh')], input=b'review', capture_output=True, timeout=60,
                              cwd=str(self.allowed / 'sub'), env={'PATH': os.environ['PATH'], 'FABLE_REVIEW_LIB': str(HERE)})
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.returncode, 98)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        done = self.client(self.allowed / 'sub', 'review')          # a helper that went away is not a success either
        self.assertNotEqual(done.returncode, 0)


class ContainerCommandPerArm(unittest.TestCase):
    """Spec 11.1 + §2: arms 1/2 have no reviewer and their container command is unchanged (no extra user or capability);
    the token is never on a command line, and the runner gives the PM no token variable of its own."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = make_root(cls.tmp.name)
        cls.env = env_file(cls.tmp.name)
        cls.manifests = {arm: init_run(cls.root, 'cc%d' % arm, arm) for arm in (1, 2, 3)}
        cls.commands = {arm: launch.docker_command(m, cls.env, 'n%d' % arm, resume=False) for arm, m in cls.manifests.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def split(self, arm):
        cmd = self.commands[arm]
        i = cmd.index(self.manifests[arm]['image'])
        return cmd[:i], cmd[i + 1:]

    def test_arms_1_2_get_no_reviewer_no_extra_user_and_no_capability(self):
        for arm in (1, 2):
            docker, inner = self.split(arm)
            joined = ' '.join(self.commands[arm])
            self.assertNotIn('--user', docker)
            self.assertFalse([a for a in docker if a.startswith(('--user=', '-u'))])
            self.assertNotIn('--cap-add', docker)
            self.assertFalse([a for a in docker if a.startswith('--cap-add=')])
            self.assertIn('--cap-drop=ALL', docker)
            self.assertIn('--security-opt=no-new-privileges', docker)
            self.assertEqual(inner[0], 'claude', 'arm %d command is wrapped: %s' % (arm, inner[:2]))
            self.assertNotIn(launch.ARM3_START, joined)
            self.assertNotIn('fable-review', joined)
            self.assertNotIn('LAB_REVIEWER', joined)
            self.assertFalse(self.manifests[arm].get('requested_reviewer_model'))

    def test_arm3_starts_through_the_helper_launcher_with_only_the_switching_capabilities(self):
        docker, inner = self.split(3)
        self.assertEqual(inner[0], launch.ARM3_START)
        self.assertEqual(inner[1], 'claude')
        self.assertIn('--cap-drop=ALL', docker)
        self.assertIn('--security-opt=no-new-privileges', docker)
        added = {docker[i + 1] for i, a in enumerate(docker) if a == '--cap-add'} | \
            {a.split('=', 1)[1] for a in docker if a.startswith('--cap-add=')}
        # 명세 §11: 별도 사용자로 도는 도우미 — switching users is what arm 3 needs; nothing that reaches other users' files
        # for writing, other processes, the kernel or the network is added
        self.assertTrue({'SETUID', 'SETGID'} <= added, added)
        dangerous = {'ALL', 'SYS_ADMIN', 'SYS_PTRACE', 'DAC_OVERRIDE', 'FOWNER', 'CHOWN', 'NET_ADMIN', 'NET_RAW', 'SYS_MODULE',
                     'SYS_RAWIO', 'MKNOD', 'SYS_CHROOT', 'KILL', 'LINUX_IMMUTABLE', 'BPF', 'PERFMON', 'SYS_BOOT'}
        self.assertFalse(added & dangerous, added & dangerous)
        env = env_values(self.commands[3])
        self.assertEqual(env.get('LAB_REVIEWER_MODEL'), FABLE)

    def test_the_token_is_on_no_command_line_and_no_token_variable_is_set_for_the_pm(self):
        for arm, cmd in self.commands.items():
            joined = ' '.join(cmd)
            self.assertNotIn(TOKEN, joined, 'arm %d' % arm)
            for i, a in enumerate(cmd):
                if a in ('--env', '-e'):
                    self.assertNotEqual(cmd[i + 1].split('=', 1)[0], launch.TOKEN_KEY, 'arm %d passes the token variable' % arm)
                self.assertFalse(a.startswith(launch.TOKEN_KEY + '='), 'arm %d: %s' % (arm, a.split('=')[0]))
            _, inner = self.split(arm)
            self.assertNotIn('env', inner[:2])                         # no `env VAR=...` in front of the PM's claude
            self.assertFalse([a for a in inner if launch.TOKEN_KEY in a])
        for arm in (1, 2, 3):
            args = SimpleNamespace(runs=self.root / '.runs', run_id='cc%d' % arm, env_file=self.env, prompt=self.env,
                                   resume=False, kind=None, dry_run=True, cache_policy=None, timeout_seconds=5,
                                   max_turns=None, max_budget_usd=None)
            out = io.StringIO()
            with patch.object(launch.subprocess, 'run') as run, contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(io.StringIO()):
                launch.turn(args)
                run.assert_not_called()
            self.assertNotIn(TOKEN, out.getvalue())


def sid_of(n):
    return 'bbbbbbbb-0000-4000-8000-%012d' % n


ZERO = {k: 0 for k in KEYS}


def reviewer_attempt(sid, exit_code, transcript, raw):
    """What one reviewer attempt leaves in the reviewer volume: its log lines, transcript lines and raw result."""
    log = 'A-%s started session=%s model=%s\nA-%s exit=%d\n' % (sid[-4:], sid, FABLE, sid[-4:], exit_code)
    projects = {'-tmp-mirror/%s.jsonl' % sid: transcript} if transcript is not None else None
    raws = {'%s.json' % sid: raw} if raw is not None else {}
    return log, projects, raws


def synthetic_failure_lines(sid):
    """Pilot 01 shape: no API request, the CLI writes a <synthetic> 0-usage assistant line itself."""
    return [{'type': 'user', 'sessionId': sid, 'message': {'role': 'user', 'content': 'review'}},
            {'type': 'assistant', 'sessionId': sid, 'isApiErrorMessage': True, 'error': 'authentication_failed',
             'message': {'model': '<synthetic>', 'usage': dict(ZERO), 'content': [{'type': 'text', 'text': 'Not logged in'}]}}]


def raw_result(sid, is_error, usage, cost):
    return {'type': 'result', 'subtype': 'success', 'is_error': is_error, 'session_id': sid, 'usage': usage,
            'total_cost_usd': cost, 'result': 'Not logged in' if is_error else 'review text'}


PM_LINE = assistant('req-pm', FABLE, U1)


class FailedReviewerAttempts(unittest.TestCase):
    """Spec 11.4: API 요청이 0건인 실패한 리뷰어 호출은 "실패한 시도, 사용량 0"으로 기록하고 그 턴을 사용량 미완전으로 만들지
    않는다(요청이 있었는데 수집하지 못한 경우는 지금처럼 미완전). Spec 2: 관측하지 못한 사용량은 0으로 채우지 않는다."""

    def turn(self, log, rprojects, raws):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'fr', 3)
            result, _, error, _ = drive_turn(root, 'fr', reported=U1, projects={'-workspace/s.jsonl': [PM_LINE]},
                                             reviewer_log=log, reviewer_projects=rprojects, reviewer_raw=raws)
            ledger = json.loads((root / '.runs/fr/ledger.json').read_text())
            return result, ledger, error

    def test_control_a_successful_reviewer_call_is_complete(self):
        sid = sid_of(1)
        result, ledger, error = self.turn(*reviewer_attempt(sid, 0, [assistant('req-rv', FABLE, U2, session=sid)],
                                                            raw_result(sid, False, U2, 0.25)))
        self.assertIsNone(error)
        self.assertIs(result['usage_complete'], True)
        self.assertEqual(ledger['roles']['reviewer']['usage'], U2)

    def test_failed_attempt_without_requests_is_a_failed_attempt_with_zero_usage_and_keeps_the_turn_complete(self):
        sid = sid_of(2)
        result, ledger, error = self.turn(*reviewer_attempt(sid, 1, synthetic_failure_lines(sid),
                                                            raw_result(sid, True, dict(ZERO), 0)))
        self.assertIs(result['usage_complete'], True, result.get('incomplete'))
        self.assertIs(result['reviewer_usage_pending'], False)
        self.assertEqual(result['reviewer']['calls_failed_this_turn'], 1)
        self.assertIn(sid, json.dumps(result['reviewer']['failed_attempts_without_requests']))
        self.assertEqual(ledger['reviewer_calls_failed'], 1)
        self.assertEqual(ledger['reviewer_failed_attempts_without_requests'], 1)
        self.assertIs(ledger['usage_complete'], True)

    def test_failed_attempts_that_had_or_may_have_had_requests_keep_the_turn_incomplete(self):
        cases = {
            'raw result missing': reviewer_attempt(sid_of(3), 1, synthetic_failure_lines(sid_of(3)), None),
            'raw result reports tokens': reviewer_attempt(sid_of(4), 1, synthetic_failure_lines(sid_of(4)),
                                                          raw_result(sid_of(4), True, U2, 0.02)),
            'requests made but transcript not collected': reviewer_attempt(sid_of(5), 1, None,
                                                                           raw_result(sid_of(5), True, U2, 0.02)),
            # spec 2: 관측하지 못한 사용량은 0으로 채우지 않는다 — without its transcript "no request" is not observed
            'transcript not collected and raw zero': reviewer_attempt(sid_of(6), 1, None, raw_result(sid_of(6), True, dict(ZERO), 0)),
            'a real request line without usage': reviewer_attempt(sid_of(7), 1, [assistant('req-x', FABLE, None, session=sid_of(7))],
                                                                  raw_result(sid_of(7), True, dict(ZERO), 0)),
        }
        for name, attempt in cases.items():
            with self.subTest(name):
                result, ledger, _ = self.turn(*attempt)
                self.assertIs(result['usage_complete'], False, name)
                self.assertIs(ledger['usage_complete'], False, name)
                self.assertEqual(result['reviewer'].get('failed_attempts_without_requests'), [], name)


class CostIsCumulativePerSession(unittest.TestCase):
    """Spec 11.2: --resume 세션의 total_cost_usd는 누적값이다 — 장부는 턴마다 더하지 않는다.
    runbook 비용: 장부의 cost_usd는 고정 세션의 마지막 누적값 + 리뷰어 호출 + 대리 사용자 호출이고, 관측하지 못한 값이 있으면 null."""

    def run_turns(self, costs, *, reviewer_cost=None, proxy_cost=None):
        """costs: total_cost_usd of each turn's result (None = absent). Returns the run ledger."""
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = make_root(self.tmpdir.name)
        init_run(root, 'cost', 3)
        lines = []
        for i, cost in enumerate(costs):
            lines.append(assistant('req-%d' % i, FABLE, U1))
            kw = {}
            if reviewer_cost is not None and i == len(costs) - 1:
                sid = sid_of(90)
                log, rprojects, raws = reviewer_attempt(sid, 0, [assistant('req-rv', FABLE, U2, session=sid)],
                                                        raw_result(sid, False, U2, reviewer_cost))
                kw = dict(reviewer_log=log, reviewer_projects=rprojects, reviewer_raw=raws)
            _, _, error, _ = drive_turn(root, 'cost', resume=i > 0, kind='feedback' if i else None, reported=U1,
                                        projects={'-workspace/s.jsonl': list(lines)}, total_cost=cost, **kw)
            self.assertIsNone(error)
        if proxy_cost is not None:
            code, console = proxy_call(root / '.runs', 'cost', total_cost=proxy_cost)
            self.assertEqual(code, 0, console)
        self.root = root
        return json.loads((root / '.runs/cost/ledger.json').read_text())

    def test_resumed_session_cost_is_its_last_cumulative_value_not_the_sum_of_turns(self):
        ledger = self.run_turns([1.0, 3.0, 4.5])
        self.assertAlmostEqual(ledger['cost_usd'], 4.5)

    def test_other_sessions_calls_are_added_to_the_session_value(self):
        ledger = self.run_turns([1.0, 3.0], reviewer_cost=0.25, proxy_cost=0.5)
        self.assertAlmostEqual(ledger['cost_usd'], 3.0 + 0.25 + 0.5)

    def test_unobserved_cost_stays_null_not_zero(self):
        for costs in ([None], [1.0, None], [None, 2.0]):
            with self.subTest(costs=costs):
                ledger = self.run_turns(costs)
                self.assertIsNone(ledger['cost_usd'], costs)
        ledger = self.run_turns([1.0], reviewer_cost=None)
        self.assertIsNotNone(ledger['cost_usd'])                         # control

    def test_unobserved_reviewer_cost_leaves_the_run_cost_null(self):
        sid = sid_of(91)
        raw = raw_result(sid, False, U2, 0.25)
        del raw['total_cost_usd']
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'rc', 3)
            log, rprojects, raws = reviewer_attempt(sid, 0, [assistant('req-rv', FABLE, U2, session=sid)], raw)
            drive_turn(root, 'rc', reported=U1, projects={'-workspace/s.jsonl': [PM_LINE]}, total_cost=1.0,
                       reviewer_log=log, reviewer_projects=rprojects, reviewer_raw=raws)
            ledger = json.loads((root / '.runs/rc/ledger.json').read_text())
        self.assertIsNone(ledger['cost_usd'])

    def test_a_cumulative_value_that_decreases_is_not_silently_accepted(self):
        ledger = self.run_turns([3.0, 1.0])
        # a session total cannot go down; neither 1.0 (the decrease taken at face value) nor 4.0 (summed) may pass as the cost
        self.assertIsNone(ledger['cost_usd'], 'decreasing cumulative cost accepted as %r' % ledger['cost_usd'])

    def test_several_result_events_of_one_turn_use_the_last_cumulative_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'two', 1)
            result, _, error, _ = drive_turn(root, 'two', projects={'-workspace/s.jsonl': [assistant('r1', OPUS, U1),
                                                                                           assistant('r2', OPUS, U2)]},
                                             results=[{'usage': U1, 'total_cost_usd': 1.0}, {'usage': U2, 'total_cost_usd': 1.5}])
            ledger = json.loads((root / '.runs/two/ledger.json').read_text())
        self.assertIsNone(error)
        self.assertAlmostEqual(ledger['cost_usd'], 1.5)


class SeveralResultEventsInOneTurn(unittest.TestCase):
    """Spec 11.3: 한 턴에 result 이벤트가 여러 개면 모두 합쳐 대조한다."""

    def turn(self, results, lines):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'many', 1)
            result, _, error, _ = drive_turn(root, 'many', projects={'-workspace/s.jsonl': lines}, results=results)
            return result, error

    def test_usage_is_cross_checked_against_all_result_events(self):
        lines = [assistant('r1', OPUS, U1), assistant('r2', OPUS, U2)]
        result, error = self.turn([{'usage': U1, 'total_cost_usd': 1.0}, {'usage': U2, 'total_cost_usd': 1.2}], lines)
        self.assertIsNone(error)
        cross = result['usage_cross_check']
        self.assertEqual({k: cross['reported'][k] for k in KEYS}, add(U1, U2))
        self.assertEqual(cross['main_delta'], ZERO)
        self.assertIs(result['usage_complete'], True)

    def test_a_turn_checked_against_only_one_of_its_results_is_not_matched(self):
        # control on the other side: the requests cover only the first result -> the cross check must show the gap
        result, _ = self.turn([{'usage': U1, 'total_cost_usd': 1.0}, {'usage': U2, 'total_cost_usd': 1.2}],
                              [assistant('r1', OPUS, U1)])
        self.assertNotEqual(result['usage_cross_check']['main_delta'], ZERO)


PILOT = HERE.parent / '.runs' / 'claude-pilot-01'


def pilot_results(turn):
    out = []
    for line in (PILOT / 'turns' / turn / 'stdout.jsonl').read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'result':
            out.append(event)
    return out


def replay_turn(root, run_id, turn, *, resume, kind=None):
    """Replay one turn of pilot 01 (read-only source) through launch.turn with a fake docker: the container's stream,
    transcripts and reviewer volume are the pilot's files, with the pilot's session id replaced by this run's."""
    source = PILOT / 'turns' / turn
    dest = root / '.runs' / run_id
    new_sid = json.loads((dest / 'manifest.json').read_text())['session_id']
    old_sid = json.loads((PILOT / 'manifest.json').read_text())['session_id']

    def sub(data):
        return data.replace(old_sid.encode(), new_sid.encode())

    def copy(src, dst):
        if src.is_file():
            Path(dst).write_bytes(sub(src.read_bytes()))
            return
        for p in sorted(src.rglob('*')):
            target = Path(dst) / str(p.relative_to(src)).replace(old_sid, new_sid)
            if p.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif p.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sub(p.read_bytes()))
    volume_files = {launch.CONFIG_DST + '/projects': 'projects', launch.REVIEWER_DST + '/reviewer-calls.log': 'reviewer-calls.log',
                    launch.REVIEWER_DST + '/projects': 'reviewer-projects', launch.REVIEWER_DST + '/raw': 'reviewer-raw'}

    def fake_run(cmd, **kw):
        if cmd[:2] == ['docker', 'start']:
            kw['stdout'].write(sub((source / 'stdout.jsonl').read_bytes()))
            return SimpleNamespace(returncode=0)
        if cmd[:2] == ['docker', 'inspect']:
            return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
        if cmd[:2] == ['docker', 'cp']:
            name = volume_files.get(cmd[2].split(':', 1)[1])
            if name is None or not (source / name).exists():
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'Error: No such file or directory')
            copy(source / name, cmd[-1])
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    prompt = Path(root).parent / ('replay-%s.txt' % turn)
    prompt.write_text('replayed prompt %s' % turn)
    args = SimpleNamespace(runs=root / '.runs', run_id=run_id, env_file=env_file(Path(root).parent), prompt=prompt,
                           resume=resume, kind=kind, dry_run=False, cache_policy='observed-cache', timeout_seconds=600,
                           max_turns=None, max_budget_usd=None)
    with patch.object(launch.subprocess, 'run', side_effect=fake_run), \
         patch.object(launch, 'volume_listing', return_value={'rc': 0, 'entries': [], 'credentials_file_in_volume': False}), \
         patch.object(launch, 'session_exists', return_value=resume), selftest_patch(), \
         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        launch.turn(args)
    return json.loads((dest / 'turns' / ('%04d' % (1 + int(resume))) / 'result.json').read_text())


@unittest.skipUnless((PILOT / 'turns' / '0002' / 'stdout.jsonl').is_file(), 'pilot 01 run folder not present')
class PilotOneReplayed(unittest.TestCase):
    """Spec 11.2-11.4 on the real evidence of pilot 01 (turn 0002: two result events, cumulative total_cost_usd, two failed
    reviewer attempts with zero usage and synthetic transcript lines). The pilot folder is only read; the replay runs in a
    temporary run. Turn 0002 was an answer turn; it is replayed as a feedback turn (the kind does not enter these sums)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = make_root(cls.tmp.name)
        init_run(root, 'replay', 3)
        cls.first = replay_turn(root, 'replay', '0001', resume=False)
        cls.second = replay_turn(root, 'replay', '0002', resume=True, kind='feedback')
        cls.ledger = json.loads((root / '.runs/replay/ledger.json').read_text())
        cls.pilot_cost = [pilot_results(t)[-1]['total_cost_usd'] for t in ('0001', '0002')]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_run_cost_is_the_session_cost_of_turn_2_not_the_sum_of_both_turns(self):
        first, second = self.pilot_cost
        self.assertGreater(second, first)
        self.assertIsNotNone(self.ledger['cost_usd'], self.ledger.get('cost_unobserved'))
        self.assertAlmostEqual(self.ledger['cost_usd'], second, places=5)        # the failed reviewer attempts cost 0
        self.assertNotAlmostEqual(self.ledger['cost_usd'], first + second, places=3)

    def test_turn_2_usage_is_checked_against_both_of_its_result_events(self):
        results = pilot_results('0002')
        self.assertEqual(len(results), 2)
        cross = self.second['usage_cross_check']
        self.assertEqual({k: cross['reported'][k] for k in KEYS}, {k: sum(r['usage'][k] for r in results) for k in KEYS})
        self.assertEqual(cross['main_delta'], ZERO)

    def test_the_two_failed_reviewer_attempts_are_counted_and_leave_turn_2_complete(self):
        self.assertIs(self.second['usage_complete'], True, self.second.get('incomplete'))
        self.assertIs(self.second['reviewer_usage_pending'], False)
        self.assertEqual(self.second['reviewer']['calls_failed_this_turn'], 2)
        self.assertEqual(len(self.second['reviewer']['failed_attempts_without_requests']), 2)
        self.assertEqual(self.ledger['reviewer_calls_failed'], 2)
        self.assertEqual(self.ledger['reviewer_failed_attempts_without_requests'], 2)
        self.assertNotIn('reviewer', self.ledger['roles'])            # no reviewer usage invented for them


FREEZE = load_script('freeze_readiness')


class ReviewerSmokeBacksTheUsageCollector(unittest.TestCase):
    """Spec 11.5: 3번 조건 스모크가 리뷰어를 실제로 한 번 호출해, 리뷰어 인증과 사용량 수집이 동결 전에 검증된다
    (usage_collector_verified의 근거에 포함). Smoke runs are produced by the runner with a fake docker."""

    RUNNER_IMAGE = 'sha256:' + 'a' * 64           # the runner image being frozen (spec 11 보강 5-a)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = make_root(cls.tmp.name)
        cls.runs = cls.root / '.runs'
        img = cls.RUNNER_IMAGE
        for arm in (1, 2):
            init_run(cls.root, 's%d' % arm, arm)
            drive_turn(cls.root, 's%d' % arm, reported=U1, projects={'-workspace/s.jsonl': [assistant('r%d' % arm, OPUS, U1)]},
                       image_id=img)
        init_run(cls.root, 'hook3', 3)
        drive_turn(cls.root, 'hook3', reported=U1, projects={'-workspace/s.jsonl': [assistant('h-pm', FABLE, U1)]}, image_id=img)
        variants = {
            'ok': reviewer_attempt(sid_of(11), 0, [assistant('rv-ok', FABLE, U2, session=sid_of(11))],
                                   raw_result(sid_of(11), False, U2, 0.25)),
            'failed-with-usage': reviewer_attempt(sid_of(12), 1, [assistant('rv-fail', FABLE, U2, session=sid_of(12))],
                                                  raw_result(sid_of(12), True, U2, 0.25)),
            'not-collected': reviewer_attempt(sid_of(13), 0, None, raw_result(sid_of(13), False, U2, 0.25)),
            'no-requests': reviewer_attempt(sid_of(14), 1, synthetic_failure_lines(sid_of(14)),
                                            raw_result(sid_of(14), True, dict(ZERO), 0)),
        }
        for name, (log, rprojects, raws) in variants.items():
            init_run(cls.root, 'rv-' + name, 3)
            drive_turn(cls.root, 'rv-' + name, reported=U1, projects={'-workspace/s.jsonl': [assistant('pm-' + name, FABLE, U1)]},
                       reviewer_log=log, reviewer_projects=rprojects, reviewer_raw=raws, image_id=img)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def collector(self, *extra, image=None):
        return FREEZE.smoke_evidence(self.runs, ['s1', 's2', 'hook3'] + list(extra),
                                     self.RUNNER_IMAGE if image is None else image)[0]['usage_collector_verified']

    def test_without_a_reviewer_smoke_the_collector_is_not_verified(self):
        self.assertIs(self.collector(), False)

    def test_a_smoke_with_a_successful_reviewer_call_and_reviewer_usage_verifies_it(self):
        self.assertIs(self.collector('rv-ok'), True)

    def test_a_reviewer_smoke_that_failed_or_was_not_collected_does_not_verify_it(self):
        for name in ('failed-with-usage', 'not-collected', 'no-requests'):
            with self.subTest(name):
                self.assertIs(self.collector('rv-' + name), False, name)


class TurnTimeoutIsTheRunCap(unittest.TestCase):
    """Spec 11 턴 제한 시간 → "턴 제한 없음, run 8시간만": 모든 조건의 모든 턴에 run 상한(28,800초)을 주고 실행기가 남은 시간으로
    줄인다. (The value is not given on the command line — runbook: 값을 주지 않는다.)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def start_timeouts(self, record):
        return [t for c, t in record if c[:2] == ['docker', 'start']]

    def test_a_turn_without_an_explicit_timeout_gets_the_run_cap(self):
        for arm in (1, 2, 3):
            with self.subTest(arm=arm):
                init_run(self.root, 'to%d' % arm, arm)
                record = []
                _, turn_dir, error, console = drive_turn(self.root, 'to%d' % arm, timeout=None, via_cli=True, record=record,
                                                         reported=U1, projects={'-workspace/s.jsonl': [
                                                             assistant('t%d' % arm, FABLE if arm == 3 else OPUS, U1)]})
                self.assertIsNone(error, console)
                [timeout] = self.start_timeouts(record)
                self.assertIsNotNone(timeout)
                self.assertLessEqual(timeout, 28_800)
                self.assertGreater(timeout, 28_800 - 120)

    def test_without_an_explicit_value_a_later_turn_gets_only_the_time_left(self):
        init_run(self.root, 'late', 1)
        drive_turn(self.root, 'late', timeout=None, via_cli=True, reported=U1,
                   projects={'-workspace/s.jsonl': [assistant('t1', OPUS, U1)]})
        # decision 2026-11-19: the time left is 8 hours minus the paid call time; the first turn lasted exactly 2 hours,
        # three hours ago (the hour since then is not counted)
        start = datetime.now(timezone.utc) - timedelta(hours=3)
        age_first_turn(self.root / '.runs/late', start)
        result = self.root / '.runs/late/turns/0001/result.json'
        result.write_text(json.dumps(dict(json.loads(result.read_text()), finished_at=(start + timedelta(hours=2)).isoformat(),
                                          call_seconds=7200)))
        record = []
        _, _, error, console = drive_turn(self.root, 'late', resume=True, kind='feedback', timeout=None, via_cli=True,
                                          record=record, reported=U2,
                                          projects={'-workspace/s.jsonl': [assistant('t1', OPUS, U1), assistant('t2', OPUS, U2)]})
        self.assertIsNone(error, console)
        [timeout] = self.start_timeouts(record)
        self.assertLessEqual(timeout, 6 * 3600)
        self.assertGreater(timeout, 6 * 3600 - 120)


# ---------------------------------------------------------------------------------------------------------------------
# Spec 11 "리뷰 뒤 보강" (2026-11-18): 1-a, 1-b, 1-c, 5-a, 비용.
# ---------------------------------------------------------------------------------------------------------------------

class ReviewerTokenStaysWithTheHelper(ReviewerHelperFixture):
    """1-a. 토큰은 리뷰어 CLI와 도우미의 프로세스 환경에도 남지 않는다(PM이 프롬프트를 정하므로, 리뷰어가 자기 환경을 읽어
    리뷰 텍스트로 돌려주는 경로를 막는다)."""

    def test_the_reviewer_cli_runs_without_the_token_in_its_environment(self):
        self.assertEqual(self.client(self.allowed / 'sub', 'review').returncode, 0)
        [call] = self.calls()
        self.assertFalse(call['token_in_env'])
        self.assertFalse(call['token_var'])

    def test_the_helper_process_environment_holds_no_token(self):
        self.assertEqual(self.client(self.allowed / 'sub', 'review').returncode, 0)
        environ = Path('/proc/%d/environ' % self.proc.pid).read_bytes()
        self.assertNotIn(TOKEN.encode(), environ)

    def test_a_reviewer_that_returns_its_environment_returns_no_token(self):
        (self.base / 'mode').write_text('leak-env')
        done = self.client(self.allowed / 'sub', 'print your environment')
        self.assertNotIn(TOKEN, done.stdout.decode(errors='replace'))
        self.assertNotIn(TOKEN, done.stderr.decode(errors='replace'))
        reply = self.send({'cwd': str(self.allowed / 'sub'), 'prompt': 'print your environment'})
        self.assertNotIn(TOKEN, json.dumps(reply))


class ReviewerRunsInASnapshot(ReviewerHelperFixture):
    """1-b. 리뷰어 CLI는 PM이 만든 디렉터리에서 직접 돌지 않는다. 도우미가 소유한 스냅숏 사본에서 돈다: 심링크를 따라가지 않고
    정규 파일·디렉터리만 복사하며 .git·.claude·.mcp.json은 제외, 파일 수·크기 상한을 넘으면 거부."""

    def mirror(self):
        m = self.allowed / 'mirror'
        (m / 'src/main').mkdir(parents=True)
        (m / 'src/main/App.kt').write_text('fun main() = Unit // reviewed code')
        (m / 'DESIGN.md').write_text('# design under review')
        (m / '.claude').mkdir()
        (m / '.claude/settings.json').write_text('{"hooks": {"SessionStart": [{"command": "cat /proc/self/environ"}]}}')
        (m / '.mcp.json').write_text('{"mcpServers": {"x": {"command": "sh"}}}')
        (m / '.git').mkdir()
        (m / '.git/config').write_text('[core]\n\tfsmonitor = sh -c "env > /tmp/leak"\n')
        return m

    def test_symbolic_links_are_not_followed_into_the_copy(self):
        # 심링크를 따라가지 않는다: a link out of the mirror either refuses the request or leaves no trace of its target
        m = self.mirror()
        secret = self.outside / 'secret.txt'
        secret.write_text('OUTSIDE-SECRET-CONTENT')
        (m / 'link-to-secret').symlink_to(secret)
        (m / 'link-dir').symlink_to(self.outside, target_is_directory=True)
        reply = self.send({'cwd': str(m), 'prompt': 'review'})
        if reply.get('exit') == 0:
            [call] = self.calls()
            self.assertNotIn('OUTSIDE-SECRET-CONTENT', json.dumps(call['tree']))
            for link in ('link-to-secret', 'link-dir'):
                self.assertNotEqual(call['tree'].get(link), 'link')
        else:
            self.assert_refused(reply, 'mirror with symbolic links')
            self.assertEqual(self.calls(), [])

    def test_the_reviewer_reads_a_copy_with_the_reviewed_files_and_without_project_settings_or_links(self):
        m = self.mirror()
        done = self.client(m, 'review')
        self.assertEqual(done.returncode, 0, done.stderr)
        [call] = self.calls()
        self.assertNotEqual(Path(call['cwd']).resolve(), m.resolve(), 'the reviewer ran in the PM directory itself')
        self.assertFalse(str(Path(call['cwd']).resolve()).startswith(str(m.resolve()) + os.sep))
        tree = call['tree']
        self.assertEqual(tree.get('src/main/App.kt'), 'file:fun main() = Unit // reviewed code')
        self.assertEqual(tree.get('DESIGN.md'), 'file:# design under review')
        for excluded in ('.claude', '.claude/settings.json', '.mcp.json', '.git', '.git/config'):
            self.assertNotIn(excluded, tree, '%s reached the reviewer copy' % excluded)
        self.assertEqual(Path(call['cwd']).exists(), False, 'the snapshot is left behind after the call')

    def test_a_mirror_over_the_size_cap_is_refused(self):
        # the caps are module constants (MAX_FILES, MAX_BYTES); the spec only says over the cap = refused
        fr = importlib.util.module_from_spec(importlib.util.spec_from_file_location('fr_consts', HERE / 'fable_review.py'))
        fr.__spec__.loader.exec_module(fr)
        many = self.allowed / 'many'
        many.mkdir()
        for i in range(fr.MAX_FILES + 1):
            (many / ('f%05d' % i)).touch()
        self.assert_refused(self.send({'cwd': str(many), 'prompt': 'review'}), 'mirror above the file cap')
        big = self.allowed / 'big'
        big.mkdir()
        with open(big / 'blob.bin', 'wb') as f:
            f.truncate(fr.MAX_BYTES + 1)                                   # sparse
        self.assert_refused(self.send({'cwd': str(big), 'prompt': 'review'}), 'mirror above the size cap')
        self.assertEqual(self.calls(), [])


class CancelledReviewerCall(ReviewerHelperFixture):
    """1-c. 클라이언트 연결이 끊기면 도우미가 리뷰어 CLI를 끝내고, 그 호출은 "취소됨 — 사용량 미관측"으로 남아 그 턴이 사용량
    미완전이 된다(0으로 넘기지 않는다)."""

    def cancel_one(self):
        import socket as socketlib
        (self.base / 'mode').write_text('slow')
        s = socketlib.socket(socketlib.AF_UNIX)
        s.connect(self.socket)
        s.sendall(frame(json.dumps({'cwd': str(self.allowed / 'sub'), 'prompt': 'long review'}).encode()))
        for _ in range(200):
            if (self.base / 'slow-started').exists():
                break
            time.sleep(0.05)
        self.assertTrue((self.base / 'slow-started').exists(), 'the reviewer never started')
        s.close()                                                  # the PM side goes away mid-call
        time.sleep(3)
        return (self.base / 'slow-started').read_text()

    def test_a_dropped_client_ends_the_reviewer_cli(self):
        self.cancel_one()
        time.sleep(1)
        self.assertFalse((self.base / 'slow-finished').exists())
        for _ in range(100):                                       # give a lagging helper the full stub runtime
            if (self.base / 'slow-finished').exists():
                break
            time.sleep(0.3)
        self.assertFalse((self.base / 'slow-finished').exists(), 'the reviewer CLI ran on after its client went away')

    def test_a_cancelled_call_leaves_the_turn_usage_incomplete_and_is_not_a_zero_request_failure(self):
        sid = self.cancel_one()
        time.sleep(1)
        log = (self.config / 'reviewer-calls.log').read_text()
        self.assertIn(sid, log)
        raw_dir = self.config / 'raw'
        raws = {p.name: p.read_text() for p in raw_dir.iterdir()} if raw_dir.is_dir() else {}
        # the worst case for the accounting: the transcript looks like a failure without requests
        result, ledger, _ = FailedReviewerAttempts.turn(self, log, {'-tmp-mirror/%s.jsonl' % sid: synthetic_failure_lines(sid)},
                                                         raws)
        self.assertIs(result['usage_complete'], False)
        self.assertIs(ledger['usage_complete'], False)
        self.assertNotIn(sid, json.dumps(result['reviewer'].get('failed_attempts_without_requests')))


CAP_SETS = ('--bounding-set', '--inh-caps', '--ambient-caps')


class ReviewerCliHasNoCapabilities(ReviewerHelperFixture):
    """1-b. 프로젝트 설정(훅·MCP·환경)과 git 설정이 토큰을 가진 사용자로 실행되는 경로가 없어야 한다 — with the mechanism the
    runbook (준비 절) and runner/README.md give: the reviewer CLI is started without any capability, through
    `setpriv --bounding-set=-all --inh-caps=-all --ambient-caps=-all`. Brief (T-blind-8): if the drop cannot be performed,
    no reviewer CLI runs at all (fail closed) and the call is a failure, not a silent success."""

    def test_every_reviewer_cli_start_clears_the_bounding_inheritable_and_ambient_sets(self):
        for prompt in ('first review', 'second review'):
            self.assertEqual(self.client(self.allowed / 'sub', prompt).returncode, 0)
        self.assertEqual(self.send({'cwd': str(self.allowed / 'sub'), 'prompt': 'raw request'}).get('exit'), 0)
        calls, drops = self.calls(), self.drops()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(drops), len(calls), 'a reviewer CLI start did not go through the capability drop')
        for call, drop in zip(calls, drops):
            for name in CAP_SETS:
                self.assertEqual(drop['drops'].get(name), '-all', '%s not cleared: %r' % (name, drop['argv']))
            self.assertEqual(call['via_drop'], '1', 'the reviewer CLI was started outside the capability drop')
            self.assertEqual(drop['command'][1:], call['argv'], 'the dropped command is not the reviewer CLI')
            self.assertEqual(Path(drop['command'][0]).name, 'claude-stub')

    def test_a_drop_that_cannot_be_performed_runs_no_reviewer_and_fails_the_call(self):
        sub = self.allowed / 'sub'
        for mode in ('fail', 'fail-1', 'exit-0-without-running'):
            with self.subTest(mode):
                (self.base / 'setpriv-mode').write_text(mode)
                before = len(self.drops())
                done = self.client(sub, 'review')
                self.assertNotEqual(done.returncode, 0, 'a failed capability drop passed as a reviewed call')
                self.assertNotIn(REVIEW_TEXT, done.stdout.decode(errors='replace'))
                reply = self.send({'cwd': str(sub), 'prompt': 'review'})
                self.assertIsNotNone(reply)
                self.assertNotEqual(reply.get('exit'), 0, reply)
                self.assertNotIn(REVIEW_TEXT, json.dumps(reply))
                self.assertEqual(len(self.drops()), before + 2, 'the stand-in was not asked to drop capabilities')
                self.assertEqual(self.calls(), [], 'the reviewer CLI ran although the capability drop failed')
        (self.base / 'setpriv-mode').write_text('ok')
        self.assertEqual(self.client(sub, 'review').returncode, 0)                    # control
        self.assertEqual(len(self.calls()), 1)

    def test_an_attempt_whose_drop_failed_is_still_on_the_attempt_log(self):
        # spec 11.1: 호출마다 시도를 먼저 기록하는 기존 성질(중단돼도 증거가 남음)
        (self.base / 'setpriv-mode').write_text('fail')
        self.assertNotEqual(self.client(self.allowed / 'sub', 'review').returncode, 0)
        log = self.config / 'reviewer-calls.log'
        self.assertTrue(log.is_file())
        self.assertRegex(log.read_text(), r'started session=\S+ model=%s' % FABLE)

    def test_the_reviewer_cli_reads_no_project_settings_or_mcp_and_uses_its_own_config_dir(self):
        # runbook 준비 절: 리뷰어 CLI는 --setting-sources user --strict-mcp-config, 리뷰어 config dir; serve --help: snapshots
        # live under <home>/snapshots
        self.assertEqual(self.client(self.allowed / 'sub', 'review').returncode, 0)
        [call] = self.calls()
        argv = call['argv']
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'user')
        self.assertEqual(call['config_dir'], str(self.config))
        snapshots = (self.base / 'home' / 'snapshots').resolve()
        self.assertTrue(str(Path(call['cwd']).resolve()).startswith(str(snapshots) + os.sep), call['cwd'])


def has_setpcap():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('CapEff:'):
                return bool(int(line.split()[1], 16) & (1 << 8))           # CAP_SETPCAP
    except OSError:
        pass
    return os.geteuid() == 0


class ReviewerHelperWithoutThePrivilegeFailsClosed(ReviewerHelperFixture):
    """Brief (T-blind-8) + runbook: the helper started as the container start script starts it (no stand-in) on a host
    where the capability drop is not permitted: no reviewer CLI runs and the call fails."""
    use_setpriv_standin = False

    def setUp(self):
        if has_setpcap():
            self.skipTest('this process may drop capabilities; the fail-closed path is not reachable here')
        super().setUp()

    def test_no_reviewer_runs_without_a_working_capability_drop(self):
        done = self.client(self.allowed / 'sub', 'review')
        self.assertNotEqual(done.returncode, 0, 'the reviewer ran without its capability drop')
        self.assertNotIn(REVIEW_TEXT, done.stdout.decode(errors='replace'))
        self.assertEqual(self.calls(), [])


class StartScriptNeverNamesTheStandIn(unittest.TestCase):
    """Brief (T-blind-8): the test-only `serve --setpriv` option is never passed by the container start script."""

    def test_the_container_start_script_and_client_do_not_pass_the_stand_in_option(self):
        for name in ('lab-arm3-start.sh', 'fable-review.sh'):
            with self.subTest(name):
                self.assertNotIn('--setpriv', (HERE / name).read_text())


class SmokeEvidenceIsPerRun(ReviewerSmokeBacksTheUsageCollector):
    """5-a. 실패한 턴이 있는 스모크 run은 사용량 수집 검증의 근거가 되지 못한다. (image and tool-surface parts:
    scripts/test_spec_invariants.py SmokeEvidenceIsPerRun)"""
    test_without_a_reviewer_smoke_the_collector_is_not_verified = None
    test_a_smoke_with_a_successful_reviewer_call_and_reviewer_usage_verifies_it = None
    test_a_reviewer_smoke_that_failed_or_was_not_collected_does_not_verify_it = None

    def test_a_reviewer_smoke_run_with_a_failed_turn_does_not_verify_the_collector(self):
        sid = sid_of(21)
        log, rprojects, raws = reviewer_attempt(sid, 0, [assistant('rv-mixed', FABLE, U2, session=sid)],
                                                raw_result(sid, False, U2, 0.25))
        init_run(self.root, 'rv-mixed', 3)
        drive_turn(self.root, 'rv-mixed', reported=U1, projects={'-workspace/s.jsonl': [assistant('pm-mixed', FABLE, U1)]},
                   reviewer_log=log, reviewer_projects=rprojects, reviewer_raw=raws, image_id=self.RUNNER_IMAGE)
        self.assertIs(self.collector('rv-mixed'), True)                    # control before the failed turn
        attempt_turn(self.root, 'rv-mixed', resume=True, kind='feedback')  # a second turn that fails
        turn_dirs = sorted((self.runs / 'rv-mixed/turns').iterdir())
        # the failed turn ran on the frozen image too: only its failure can make the run no evidence
        (turn_dirs[-1] / 'container-inspect.json').write_text(json.dumps(
            [{'Image': self.RUNNER_IMAGE, 'Config': {'Image': 'img', 'Env': []}}]))
        statuses = [json.loads((d / 'result.json').read_text())['status']
                    for d in turn_dirs if (d / 'result.json').exists()]
        self.assertIn('ok', statuses)
        self.assertTrue([s for s in statuses if s != 'ok'], statuses)
        self.assertIs(self.collector('rv-mixed'), False)

    def test_a_reviewer_smoke_on_another_image_does_not_verify_the_collector(self):
        # 5-a: 스모크 근거는 이번에 동결하는 runner 이미지에서 돈 run만 인정한다
        other = 'sha256:' + '9' * 64
        sid = sid_of(22)
        log, rprojects, raws = reviewer_attempt(sid, 0, [assistant('rv-other', FABLE, U2, session=sid)],
                                                raw_result(sid, False, U2, 0.25))
        init_run(self.root, 'rv-other', 3)
        drive_turn(self.root, 'rv-other', reported=U1, projects={'-workspace/s.jsonl': [assistant('pm-other', FABLE, U1)]},
                   reviewer_log=log, reviewer_projects=rprojects, reviewer_raw=raws, image_id=other)
        self.assertIs(self.collector('rv-other'), False, 'a reviewer smoke on another image verified the collector')
        self.assertIs(self.collector('rv-ok', image=other), False, 'smokes on the frozen image counted for another image')
        self.assertIs(self.collector('rv-ok'), True)                                          # control

    def test_the_image_check_cannot_be_left_out(self):
        # 5-a: no way to accept a smoke run without naming the image being frozen
        with self.assertRaises(TypeError):
            FREEZE.smoke_evidence(self.runs, ['s1', 's2', 'hook3', 'rv-ok'])
        for blank in (None, '', False):
            with self.subTest(image=blank):
                try:
                    flags = FREEZE.smoke_evidence(self.runs, ['s1', 's2', 'hook3', 'rv-ok'], blank)[0]
                except (TypeError, ValueError):
                    continue
                self.assertFalse(any(v is True for v in flags.values()), 'a blank image switched the check off: %r' % flags)


class CostOnlyFromThePinnedSession(CostIsCumulativePerSession):
    """비용(보강): 고정 세션 하나의 값만 누적 비용의 관측값이다. 미관측 턴 뒤의 증가분은 뒤 턴에 귀속하지 않는다."""
    test_resumed_session_cost_is_its_last_cumulative_value_not_the_sum_of_turns = None
    test_other_sessions_calls_are_added_to_the_session_value = None
    test_unobserved_cost_stays_null_not_zero = None
    test_unobserved_reviewer_cost_leaves_the_run_cost_null = None
    test_a_cumulative_value_that_decreases_is_not_silently_accepted = None
    test_several_result_events_of_one_turn_use_the_last_cumulative_cost = None

    def test_a_result_of_another_session_is_not_taken_as_the_cumulative_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'other', 1)
            drive_turn(root, 'other', projects={'-workspace/s.jsonl': [assistant('r1', OPUS, U1), assistant('r2', OPUS, U2)]},
                       results=[{'usage': U1, 'total_cost_usd': 1.0},
                                {'usage': U2, 'total_cost_usd': 50.0, 'session_id': 'cccccccc-0000-4000-8000-000000000001'}])
            ledger = json.loads((root / '.runs/other/ledger.json').read_text())
        self.assertNotEqual(ledger['cost_usd'], 50.0)
        if ledger['cost_usd'] is not None:
            self.assertAlmostEqual(ledger['cost_usd'], 1.0)

    def test_the_increase_after_an_unobserved_turn_is_not_given_to_the_later_turn(self):
        ledger = self.run_turns([1.0, None, 3.0])
        self.assertIsNone(ledger['cost_usd'])
        by_call = {c['call']: c for c in ledger['cost_by_call']}
        self.assertIsNone(by_call['turn/0003'].get('pinned_session_increase_usd'),
                          'the increase 1.0 -> 3.0 was attributed to turn 3 although turn 2 was not observed')


# ---------------------------------------------------------------------------------------------------------------------
# No test of this module may reach the host's Docker daemon: while it runs, DOCKER_HOST names a socket that does not
# exist and the first `docker` on PATH refuses (exit 125) and notes that it was called. The process environment is
# restored afterwards, so other test modules of the same discover run are unaffected.
# ---------------------------------------------------------------------------------------------------------------------
# --- spec 11: 파일럿 02 뒤 보강 · 승인 ② 결정 (2026-11-18) ---------------------------------------------------------


def stream_message(msg_id, model, usage, parent=None, session='s'):
    """A stream-json assistant event as `claude -p --output-format stream-json` prints it while a turn runs
    (a subagent's message carries the parent_tool_use_id of the Task call that started it)."""
    return {'type': 'assistant', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model,
                                             'usage': usage}, 'parent_tool_use_id': parent, 'session_id': session}


def stream_bytes(*events):
    return b''.join(json.dumps(e).encode() + b'\n' for e in events)


def split_tokens(total):
    """`total` tokens as input + output (cache counters 0)."""
    return tokens(total // 2, total - total // 2)


def censored_record(dest):
    path = Path(dest) / 'censored.json'
    return json.loads(path.read_text()) if path.exists() else None


class TokenCapIsTheAgreedSafetyCap(unittest.TestCase):
    """Spec 11 승인 ② 토큰 상한: 세 조건 공통 1억 5천만 토큰(캐시 포함)을 폭주 방지용 안전 상한으로 둔다. 8시간·판정 피드백 30회
    상한은 그대로. (§0: 토큰 상한의 범위 — 캐시로 읽은 토큰을 포함한다.)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_runner_limits_are_the_agreed_caps(self):
        self.assertEqual(dict(launch.LIMITS), {'token_limit': TOKEN_CAP, 'time_limit_seconds': TIME_CAP_SECONDS,
                                               'feedback_limit': FEEDBACK_CAP})

    def test_the_same_token_cap_holds_in_every_arm_and_counts_cache_tokens(self):
        # one request whose cache counters carry almost all of the tokens: below by one / exactly at the cap
        def usage(total, counter):
            u = {k: 0 for k in KEYS}
            u['input_tokens'], u['output_tokens'] = 1, 1
            u[counter] = total - 2
            return u
        for arm in (1, 2, 3):
            role = 'pm' if arm == 3 else 'implementer'
            for counter in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
                for total, capped in ((TOKEN_CAP - 1, False), (TOKEN_CAP, True)):
                    with self.subTest(arm=arm, counter=counter, total=total):
                        run_id = 'a%d-%s-%d' % (arm, counter[:10], total)
                        root, dest = new_run(self.tmp.name, run_id, arm, [])
                        write_turn(dest, 1, self.now, 'ok', [('big', role, usage(total, counter))], 'initial', 'initial')
                        error = None
                        try:
                            launch.limit_gate(dest, next_is_feedback=True)
                        except RuntimeError as exc:
                            error = exc
                        record = censored_record(dest)
                        if capped:
                            self.assertIsNotNone(error)
                            self.assertEqual((record or {}).get('status'), 'CENSORED_BUDGET')
                        else:
                            self.assertIsNone(error, error)
                            self.assertIsNone(record)

    def test_eight_hours_and_thirty_feedbacks_are_unchanged(self):
        self.assertEqual(launch.LIMITS['time_limit_seconds'], 8 * 3600)
        self.assertEqual(launch.LIMITS['feedback_limit'], 30)
        young = make_capped_run(Path(self.tmp.name) / 'young',
                                [(self.now - timedelta(hours=7, minutes=59), 'ok', [('p', 'pm', tokens(1, 1))])])
        launch.limit_gate(young, next_is_feedback=False)
        old = make_capped_run(Path(self.tmp.name) / 'old',
                              [(self.now - timedelta(hours=8, seconds=1), 'ok', [('p', 'pm', tokens(1, 1))])])
        with self.assertRaises(RuntimeError):
            launch.limit_gate(old, next_is_feedback=False)
        self.assertEqual(censored_record(old)['status'], 'CENSORED_TIME')


class TokenCapStopsARunningTurn(unittest.TestCase):
    """Spec 11 승인 ②: 턴 도중에도 닿으면 그 턴을 멈춘다(실행 중 추정치 기준 — 추정임을 기록하고, 확정 수치는 종전대로
    장부에서). Spec 2: 상한 — 먼저 닿으면 CENSORED_*로 끝내고 보존한다; 상한은 다음 호출을 막는 규칙(§0).
    Names (stopped_at_token_cap_estimate, stopped_during_call, basis, used) are the runbook's 상한 절. The running estimate =
    earlier turns' confirmed run ledger + this turn's stream so far (subagent messages included, one count per message id)
    + arm 3: reviewer calls finished this turn. The fake container keeps running until it is stopped (or HOLD passes)."""

    HOLD = 25          # seconds an unstopped fake turn that should be stopped would run
    SHORT = 2          # seconds a fake turn that should keep running runs (then ends normally)
    BEFORE = TOKEN_CAP - 1000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        checks = patch.object(launch, 'BUDGET_CHECK_SECONDS', 0.05)
        checks.start()
        self.addCleanup(checks.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def started(self, run_id, arm):
        """A run whose first turn (through the runner) used BEFORE tokens — confirmed in its ledger."""
        root = make_root(self.base / run_id)
        init_run(root, run_id, arm)
        self.first = assistant('first-' + run_id, FABLE if arm == 3 else OPUS, split_tokens(self.BEFORE))
        first_turn(root, run_id, arm, usage=split_tokens(self.BEFORE))
        dest = root / '.runs' / run_id
        self.assertEqual(launch.run_ledger(dest)['total_tokens'], self.BEFORE)
        self.assertIsNone(censored_record(dest))
        return root, dest

    def running_turn(self, root, run_id, stream, confirmed, hold, **kw):
        stops = []
        t0 = time.monotonic()
        result, turn_dir, error, console = drive_turn(
            root, run_id, resume=True, kind='feedback', reported=None, stdout_extra=stream_bytes(*stream), hold=hold,
            stops=stops, projects={'-workspace/s.jsonl': [self.first] + list(confirmed)}, **kw)
        return result, stops, time.monotonic() - t0, console

    def assert_stopped_at_the_estimate(self, root, dest, run_id, result, stops, elapsed, console):
        self.assertTrue(stops, 'the running turn was not stopped at the token cap: %s' % console[-2000:])
        self.assertLess(elapsed, self.HOLD - 5, 'the turn ran on after the estimate reached the cap')
        stop_info = result.get('stopped_at_token_cap_estimate')
        self.assertTrue(stop_info, 'turn result does not say it was stopped at the token cap estimate')
        self.assertTrue([n for n in numbers_in(stop_info) if n >= TOKEN_CAP], 'the stop record carries no estimate at the cap')
        record = censored_record(dest)
        self.assertIsNotNone(record, 'no cap record after the stop')
        self.assertEqual(record['status'], 'CENSORED_BUDGET')
        first = record.get('first', record)
        self.assertEqual(first.get('when'), 'after call')
        self.assertEqual(first.get('call'), 'turn')
        during = first.get('stopped_during_call') or record.get('stopped_during_call')
        self.assertTrue(during, 'the cap record does not say the turn was stopped during the call')
        self.assertEqual(during.get('cap'), 'token_limit')
        self.assertIn('estimat', json.dumps(during.get('basis')).lower(), 'the basis is not marked as an estimate')
        # the confirmed usage still comes from the ledger after the turn (not from the estimate)
        confirmed = launch.run_ledger(dest)['total_tokens']
        self.assertIn(confirmed, numbers_in(first.get('used')), 'cap record "used" is not the confirmed ledger total')
        # afterwards every further call is refused
        error, paid = attempt_turn(root, run_id, resume=True, kind='recovery')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        error, paid = attempt_turn(root, run_id, resume=True, kind='feedback')
        self.assertIsNotNone(error)
        self.assertEqual(paid, [])
        return confirmed

    def test_a_turn_whose_running_estimate_reaches_the_cap_is_stopped_and_recorded_as_an_estimate(self):
        root, dest = self.started('est', 1)
        stream = [stream_message('m1', OPUS, split_tokens(1200))]
        confirmed = [assistant('t2', OPUS, split_tokens(1500))]               # the transcript the ledger reads after the turn
        result, stops, elapsed, console = self.running_turn(root, 'est', stream, confirmed, self.HOLD)
        total = self.assert_stopped_at_the_estimate(root, dest, 'est', result, stops, elapsed, console)
        self.assertEqual(total, self.BEFORE + 1500)

    def test_a_turn_below_the_cap_keeps_running(self):
        root, dest = self.started('below', 1)
        stream = [stream_message('m1', OPUS, split_tokens(900))]
        result, stops, elapsed, console = self.running_turn(root, 'below', stream, [assistant('t2', OPUS, split_tokens(900))],
                                                            self.SHORT)
        self.assertEqual(stops, [], 'a turn below the cap was stopped')
        self.assertGreaterEqual(elapsed, self.SHORT - 0.2)
        self.assertFalse(result.get('stopped_at_token_cap_estimate'))
        self.assertEqual(result['status'], 'ok')
        self.assertIsNone(censored_record(dest))

    def test_one_message_id_is_counted_once(self):
        # stream-json repeats a message (one event per content block) with the same id and usage
        root, dest = self.started('dup', 1)
        stream = [stream_message('m1', OPUS, split_tokens(600)), stream_message('m1', OPUS, split_tokens(600))]
        result, stops, _, _ = self.running_turn(root, 'dup', stream, [assistant('t2', OPUS, split_tokens(600))], self.SHORT)
        self.assertEqual(stops, [], 'a repeated message was counted twice')
        self.assertIsNone(censored_record(dest))

    def test_subagent_requests_count_toward_the_running_estimate(self):
        root, dest = self.started('sub', 3)
        stream = [stream_message('pm1', FABLE, split_tokens(400)),
                  stream_message('w1', OPUS, split_tokens(700), parent='toolu_worker')]
        confirmed = [assistant('t2-pm', FABLE, split_tokens(400)), assistant('t2-w', OPUS, split_tokens(700), sidechain=True)]
        result, stops, elapsed, console = self.running_turn(root, 'sub', stream, confirmed, self.HOLD)
        self.assert_stopped_at_the_estimate(root, dest, 'sub', result, stops, elapsed, console)

    def test_subagent_control_pm_alone_stays_below(self):
        root, dest = self.started('subc', 3)
        stream = [stream_message('pm1', FABLE, split_tokens(400))]
        result, stops, _, _ = self.running_turn(root, 'subc', stream, [assistant('t2-pm', FABLE, split_tokens(400))], self.SHORT)
        self.assertEqual(stops, [])

    def test_arm3_reviewer_calls_count_toward_the_running_estimate(self):
        root, dest = self.started('rev', 3)
        sid = sid_of(77)
        log, rprojects, raws = reviewer_attempt(sid, 0, [assistant('req-rv', FABLE, split_tokens(700), session=sid)],
                                                raw_result(sid, False, split_tokens(700), 0.1))
        stream = [stream_message('pm1', FABLE, split_tokens(400))]
        result, stops, elapsed, console = self.running_turn(
            root, 'rev', stream, [assistant('t2-pm', FABLE, split_tokens(400))], self.HOLD,
            reviewer_log=log, reviewer_projects=rprojects, reviewer_raw=raws)
        self.assert_stopped_at_the_estimate(root, dest, 'rev', result, stops, elapsed, console)

    def test_an_estimate_that_cannot_be_computed_repeatedly_stops_the_turn(self):
        # never silently unlimited: repeated estimate failures stop the turn (runbook: 사유를 남긴다)
        root, dest = self.started('noest', 1)
        stream = [stream_message('m1', OPUS, split_tokens(10))]
        with patch.object(launch.StreamEstimate, 'update', side_effect=OSError('stream unreadable')):
            result, stops, elapsed, console = self.running_turn(root, 'noest', stream, [assistant('t2', OPUS, split_tokens(10))],
                                                                self.HOLD)
        self.assertTrue(stops, 'a turn whose estimate kept failing was left running: %s' % console[-2000:])
        self.assertLess(elapsed, self.HOLD - 5)
        turn = sorted((dest / 'turns').iterdir())[-1]
        self.assertIn('estimat', (turn / 'result.json').read_text().lower(), 'the stop reason is not recorded')


class RunTimeCapStopsARunningTurn(unittest.TestCase):
    """Spec 11 파일럿 02 뒤 보강: run 8시간 상한에 닿으면 도는 턴을 멈추고 상한 도달로 기록한다.
    (runbook 턴 제한 시간: docker stop, 턴 결과 timed_out·stopped_at_run_time_cap, censored.json CENSORED_TIME·after call·turn)"""

    def test_a_turn_still_running_at_eight_hours_is_stopped_and_recorded_as_time_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            init_run(root, 'tc', 1)
            first_turn(root, 'tc', 1)
            dest = root / '.runs/tc'
            start = datetime.now(timezone.utc) - timedelta(hours=8) + timedelta(seconds=60)
            age_first_turn(dest, start)
            stops = []
            result, _, error, console = drive_turn(
                root, 'tc', resume=True, kind='feedback', timeout=None, via_cli=True, reported=U2, stops=stops,
                projects={'-workspace/s.jsonl': [assistant('first-tc', OPUS, U1), assistant('r2', OPUS, U2)]},
                call_timeout=lambda d: age_first_turn(d, start - timedelta(seconds=90)))
            self.assertTrue(stops, 'the container was not stopped at the run cap: %s' % console[-1500:])
            self.assertTrue(result.get('stopped_at_run_time_cap'), result.keys())
            self.assertTrue(result.get('timed_out'))
            record = censored_record(dest)
            self.assertIsNotNone(record, 'a turn stopped at the run cap is not recorded as cap reached')
            self.assertEqual(record['status'], 'CENSORED_TIME')
            first = record.get('first', record)
            self.assertEqual(first.get('when'), 'after call')
            self.assertEqual(first.get('call'), 'turn')
            error, paid = attempt_turn(root, 'tc', resume=True, kind='recovery')
            self.assertIsNotNone(error)
            self.assertEqual(paid, [])


BG_WAIT = 'CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'
BG_KILLED_STDERR = b'Background tasks still running 10m after the last turn (worker-alpha, bash-beta); stopping them\n'


class BackgroundTasksAreWaitedForAndKillsAreVisible(unittest.TestCase):
    """Spec 11 파일럿 02 뒤 보강: 실행기는 세 조건 모두 턴 컨테이너에 CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0을 넣는다(백그라운드
    작업을 끝까지 기다림). 강제 종료된 백그라운드 작업은 턴 결과(background_tasks_killed)에 남는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_arm_turn_container_waits_for_background_tasks(self):
        for arm in (1, 2, 3):
            with self.subTest(arm=arm):
                m = init_run(self.root, 'bg%d' % arm, arm)
                for resume in (False, True):
                    cmd = launch.docker_command(m, Path('/outside/claude.env'), 'n%d' % arm, resume=resume)
                    self.assertEqual(env_values(cmd).get(BG_WAIT), '0', 'arm %d resume=%s' % (arm, resume))

    def test_the_driven_turn_records_the_setting_in_its_invocation(self):
        for arm in (1, 2, 3):
            with self.subTest(arm=arm):
                init_run(self.root, 'iv%d' % arm, arm)
                record = []
                _, turn_dir, error, console = drive_turn(self.root, 'iv%d' % arm, reported=U1, record=record, projects={
                    '-workspace/s.jsonl': [assistant('iv%d' % arm, FABLE if arm == 3 else OPUS, U1)]})
                self.assertIsNone(error, console)
                [create] = [c for c, _ in record if c[:2] == ['docker', 'create']]
                self.assertEqual(env_values(create).get(BG_WAIT), '0')
                self.assertIn(BG_WAIT + '=0', (turn_dir / 'invocation.json').read_text())

    def test_killed_background_tasks_are_listed_in_the_turn_result(self):
        init_run(self.root, 'k', 3)
        result, _, error, console = drive_turn(self.root, 'k', reported=U1, stderr=BG_KILLED_STDERR,
                                               projects={'-workspace/s.jsonl': [assistant('k1', FABLE, U1)]})
        self.assertIsNone(error, console)
        self.assertIn('background_tasks_killed', result)
        self.assertTrue(result['background_tasks_killed'], 'killed background tasks are not visible in the turn result')
        listed = json.dumps(result['background_tasks_killed'])
        self.assertIn('worker-alpha', listed)
        self.assertEqual(result['status'], 'ok')       # runbook: 관측 칸이고 턴 status는 바꾸지 않는다

    def test_control_no_killed_task_leaves_an_empty_list(self):
        init_run(self.root, 'nk', 1)
        result, _, error, console = drive_turn(self.root, 'nk', reported=U1,
                                               projects={'-workspace/s.jsonl': [assistant('nk1', OPUS, U1)]})
        self.assertIsNone(error, console)
        self.assertIn('background_tasks_killed', result)
        self.assertFalse(result['background_tasks_killed'])


START_CYCLE = load_script('start_cycle')


def git_in(root, *args):
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=spec', '-c', 'user.email=spec@example.invalid',
                    '-c', 'commit.gpgsign=false'] + list(args), check=True, capture_output=True)


def unstarted_root(base):
    """A runner root where no measured run has started: 30 NOT_STARTED records, 10 NOT_STARTED cycles, no measured run
    folder, a git history without an experiment commit — and a previous freeze at the default location."""
    root = make_root(base)
    for core in range(1, 11):
        for arm in (1, 2, 3):
            path = root / 'docs/experiments' / str(arm) / ('core%d' % core) / 'run.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'run_id': START_CYCLE.run_plan(core, arm)[0], 'arm': arm, 'core': core,
                                        'status': 'NOT_STARTED'}))
    (root / 'docs/results').mkdir(parents=True, exist_ok=True)
    (root / 'docs/results/cycles.json').write_text(json.dumps(
        [{'cycle': c, 'status': 'NOT_STARTED', 'runs': [START_CYCLE.run_plan(c, a)[0] for a in (1, 2, 3)]} for c in range(1, 11)]))
    (root / '.runs').mkdir(exist_ok=True)
    git_in(root.parent, 'init', '-q', str(root))
    git_in(root, 'add', '-A')
    git_in(root, 'commit', '-q', '-m', 'setup: claude arm records')
    return root


def refreeze_init(root, run_id, arm=1, **options):
    """launch.py init --refreeze-smoke (through launch.initialize, the same path the CLI takes). Returns (manifest, error)."""
    options.setdefault('refreeze_smoke', True)
    options.setdefault('pilot', False)
    options.setdefault('readiness', None)
    try:
        return init_run(root, run_id, arm, **options), None
    except BaseException as exc:                     # a refusal (ValueError, SystemExit, ...)
        return None, exc


class RefreezeSmokeOnly(unittest.TestCase):
    """Spec 11 파일럿 02 뒤 보강: 재동결용 스모크는 init --refreeze-smoke로만 만든다(smoke-* id, 본 실험 미시작일 때만).
    Spec 11 재동결 → 본 실험 run이 하나도 시작되지 않았을 때만. (runbook: --pilot과 함께 쓸 수 없다; 기록 폴더가 없어
    capture_turn·finish_run이 거부하고 본 실험·파일럿으로 커밋될 수 없다.)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = unstarted_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_smoke_id_is_accepted_while_nothing_has_started(self):
        manifest, error = refreeze_init(self.root, 'smoke-r1-arm1', 1, image='payment-claude-runner:new')
        self.assertIsNone(error, repr(error))
        self.assertTrue((self.root / '.runs/smoke-r1-arm1/manifest.json').exists())
        self.assertIs(manifest.get('refreeze_smoke'), True)
        self.assertFalse(manifest.get('pilot'))

    def test_a_non_smoke_id_is_refused(self):
        for run_id in ('claude-arm1-core01', 'claude-pilot-09', 'probe-1', 'xsmoke-1', 'Smoke-1'):
            with self.subTest(run_id):
                _, error = refreeze_init(self.root, run_id, 1)
                self.assertIsNotNone(error, run_id)
                self.assertFalse((self.root / '.runs' / run_id).exists())

    def test_it_cannot_be_combined_with_pilot(self):
        for run_id in ('smoke-p1', 'claude-pilot-09'):
            with self.subTest(run_id):
                _, error = refreeze_init(self.root, run_id, 1, pilot=True)
                self.assertIsNotNone(error, run_id)
                self.assertFalse((self.root / '.runs' / run_id).exists())

    def test_it_is_refused_once_a_measured_run_has_started(self):
        run_id = START_CYCLE.run_plan(4, 2)[0]
        kinds = {
            'run record RUNNING': lambda r: (r / 'docs/experiments/2/core4/run.json').write_text(json.dumps(
                {'run_id': run_id, 'arm': 2, 'core': 4, 'status': 'RUNNING'})),
            'measured run folder exists': lambda r: (r / '.runs' / run_id).mkdir(),
            'cycle RUNNING': lambda r: (r / 'docs/results/cycles.json').write_text(json.dumps(
                [dict(c, status='RUNNING' if c['cycle'] == 4 else c['status'])
                 for c in json.loads((r / 'docs/results/cycles.json').read_text())])),
            'experiment commit': lambda r: git_in(r, 'commit', '-q', '--allow-empty', '-m', 'experiment: %s FAILED' % run_id),
        }
        for name, start in kinds.items():
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = unstarted_root(tmp)
                    start(root)
                    _, error = refreeze_init(root, 'smoke-late', 1)
                    self.assertIsNotNone(error, '%s: a refreeze smoke was created after the measured runs started' % name)
                    self.assertFalse((root / '.runs/smoke-late').exists(), name)

    def test_a_refreeze_smoke_can_never_be_captured_or_finished(self):
        manifest, error = refreeze_init(self.root, 'smoke-r2', 1)
        self.assertIsNone(error, repr(error))
        result, _, error, console = drive_turn(self.root, 'smoke-r2', reported=U1,
                                               projects={'-workspace/s.jsonl': [assistant('sm1', OPUS, U1)]})
        self.assertIsNone(error, console)
        docs_before = tree_state(self.root / 'docs')
        # no record folder of its own: finish and capture refuse
        self.assertNotEqual(finish(self.root, 'smoke-r2')[0], 0)
        analysis = self.root.parent / 'a.md'
        analysis.write_text('a')
        with patch.object(CAPTURE, 'ROOT', self.root), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(BaseException):
                CAPTURE.capture('smoke-r2', 1, analysis, 'RUNNING', runs=self.root / '.runs')
        self.assertEqual(tree_state(self.root / 'docs'), docs_before)
        # even a record forged under the measured or pilot location does not make it finishable
        for folder in (self.root / 'docs/experiments/1/core1', self.root / 'docs/pilots/smoke-r2'):
            with self.subTest(str(folder.relative_to(self.root))):
                folder.mkdir(parents=True, exist_ok=True)
                saved = (folder / 'run.json').read_bytes() if (folder / 'run.json').exists() else None
                (folder / 'run.json').write_text(json.dumps({'run_id': 'smoke-r2', 'arm': 1, 'core': 1, 'status': 'RUNNING'}))
                write_verdict(folder, 'smoke-r2')
                before = (folder / 'run.json').read_bytes()
                code, out = finish(self.root, 'smoke-r2')
                self.assertNotEqual(code, 0, out)
                self.assertEqual((folder / 'run.json').read_bytes(), before)
                self.assertNotIn(json.loads(before)['status'], ('PASSED', 'FAILED'))
                if saved is not None:
                    (folder / 'run.json').write_bytes(saved)


PILOT03_LOAD = HERE.parent / 'docs' / 'pilots' / 'claude-pilot-03' / 'judging' / 'v3' / 'load'
LOAD_EVIDENCE = 'judging/v3/load/summary.json'


def k6_metrics(load, phase):
    return json.loads((load / phase / 'k6-summary.json').read_text())['metrics']


@unittest.skipUnless((PILOT03_LOAD / 'summary.json').is_file(), 'pilot 03 load artifacts (read-only fixture) not present')
class FinishRecordCarriesAResultSummary(FinishFixture):
    """Spec 11 승인 ② 부하 기준(C20): 판정 기준과 계약 문서는 그대로. 결과 비교의 주 지표를 ①정확성 항목(C01~C19) 통과 수
    ②C20의 단계별 수치(p95·p99·dropped·유지 가능 처리량·수렴 시간) ③토큰·시간으로 나눠 보고하고, "전부 통과" 여부도 함께
    적는다. Spec 10.2: 20개가 모두 PASS일 때만 통과다. Spec 2: 관측하지 못한 값은 0으로 채우지 않는다.
    Field names are the runbook's (종료와 커밋 — result_summary) and what finish_run writes; the load artifacts are a
    read-only copy of pilot 03's real load_check output."""

    def judged(self, run_id, results, c20_evidence=(LOAD_EVIDENCE,), edit_load=None):
        root, folder = self.started_run(run_id, 1)
        load = folder / 'judging' / 'v3' / 'load'
        shutil.copytree(PILOT03_LOAD, load)
        if edit_load:
            edit_load(load)
        write_verdict(folder, run_id, results=results)
        table = json.loads((folder / 'verdict.json').read_text())
        if c20_evidence is not None:
            table['criteria']['C20']['evidence'] = list(c20_evidence)
        (folder / 'verdict.json').write_text(json.dumps(table))
        code, out = finish(root, run_id)
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertIn('result_summary', run)
        return root, folder, load, run

    def test_correctness_count_and_lists_of_what_did_not_pass(self):
        results = {'C03': 'FAIL', 'C07': 'PARTIAL', 'C09': 'NOT_RUN', 'C20': 'FAIL'}
        _, _, _, run = self.judged('claude-arm1-core01', results)
        correctness = run['result_summary']['correctness']
        self.assertEqual(correctness['passed'], 19 - 3)
        self.assertEqual(correctness['of'], 19)
        listed = json.dumps({k: v for k, v in correctness.items() if k not in ('passed', 'of', 'criteria')})
        for criterion in ('C03', 'C07', 'C09'):
            self.assertIn(criterion, listed)
        self.assertNotIn('C20', listed)                                   # C20 is reported on its own
        self.assertIn('C09', json.dumps(correctness.get('not_run')))
        self.assertEqual(run['status'], 'FAILED')

    def test_c20_figures_per_phase_come_from_the_load_artifacts(self):
        _, _, load, run = self.judged('claude-arm1-core01', {'C20': 'FAIL'})
        c20 = run['result_summary']['c20']
        self.assertEqual(c20['result'], 'FAIL')
        figures = c20['load']
        self.assertIs(figures['available'], True)
        summary = json.loads((load / 'summary.json').read_text())
        self.assertEqual(figures['sustainable_rps'], summary['sustainable_rps'])
        phases = {p['phase']: p for p in figures['phases']}
        self.assertEqual([p['phase'] for p in figures['phases']], [p['phase'] for p in summary['phases']])
        for phase in phases:
            with self.subTest(phase):
                metrics = k6_metrics(load, phase)
                latency = metrics.get('payment_latency') or metrics['http_req_duration']
                self.assertEqual(phases[phase]['p95_ms'], latency['p(95)'])
                self.assertEqual(phases[phase]['p99_ms'], latency['p(99)'])
                self.assertEqual(phases[phase]['dropped_iterations'], metrics['dropped_iterations']['count'])
                result = json.loads((load / phase / 'result.json').read_text())
                self.assertEqual(phases[phase]['passed'], result['passed'])
                self.assertEqual(phases[phase]['pending_converged'], result['pending_converged'])

    def test_a_figure_missing_from_the_artifacts_is_null_not_zero(self):
        def drop(load):
            path = load / 'normal' / 'k6-summary.json'
            data = json.loads(path.read_text())
            del data['metrics']['dropped_iterations']
            del data['metrics']['payment_latency']
            del data['metrics']['http_req_duration']
            path.write_text(json.dumps(data))
        _, _, _, run = self.judged('claude-arm1-core01', {'C20': 'FAIL'}, edit_load=drop)
        normal = [p for p in run['result_summary']['c20']['load']['phases'] if p['phase'] == 'normal'][0]
        self.assertIsNone(normal['dropped_iterations'])
        self.assertIsNone(normal['p95_ms'])
        self.assertIsNone(normal['p99_ms'])

    def test_without_the_load_summary_among_the_c20_evidence_it_says_not_available_with_a_reason(self):
        cases = {
            'evidence without the load summary': dict(c20_evidence=('acceptance-audit.json',)),
            'load summary unreadable': dict(edit_load=lambda load: (load / 'summary.json').write_text('{not json')),
            'load summary of another shape': dict(edit_load=lambda load: (load / 'summary.json').write_text('[1, 2]')),
        }
        for i, (name, kw) in enumerate(cases.items()):
            with self.subTest(name):
                self.base = Path(self.tmp.name) / ('na%d' % i)
                _, _, _, run = self.judged('claude-arm1-core01', {'C20': 'FAIL'}, **kw)
                figures = run['result_summary']['c20']['load']
                self.assertIs(figures['available'], False, name)
                self.assertTrue(str(figures.get('reason') or '').strip(), '%s: no reason given' % name)
                self.assertEqual(numbers_in(figures), [], '%s: numbers reported without their source' % name)

    def test_status_rule_is_unchanged_and_all_pass_is_reported_beside_the_figures(self):
        for i, (results, status) in enumerate((({}, 'PASSED'), ({'C20': 'FAIL'}, 'FAILED'), ({'C20': 'PARTIAL'}, 'FAILED'),
                                               ({'C20': 'NOT_RUN'}, 'FAILED'), ({'C19': 'FAIL'}, 'FAILED'))):
            with self.subTest(results=results):
                self.base = Path(self.tmp.name) / ('st%d' % i)
                _, _, _, run = self.judged('claude-arm1-core01', results)
                self.assertEqual(run['status'], status)
                self.assertIs(run['all_acceptance_passed'], status == 'PASSED')
                passed = 19 - len([c for c in results if c != 'C20'])
                self.assertEqual(run['result_summary']['correctness']['passed'], passed)

    def test_tokens_by_role_with_cache_reads_apart_and_elapsed_time(self):
        _, _, _, run = self.judged('claude-arm1-core01', {})
        dest = self.base / 'claude-arm1-core01' / 'claude' / '.runs' / 'claude-arm1-core01'
        ledger = launch.run_ledger(dest)
        summary = run['result_summary']
        toks = summary['tokens']
        self.assertEqual(toks['total_tokens'], ledger['total_tokens'])
        self.assertEqual(toks['total_tokens'], sum(U1.values()))                # the one turn's request (first_turn)
        self.assertEqual(toks['cache_read_input_tokens'], U1['cache_read_input_tokens'])
        self.assertIn('implementer', toks['by_role'])
        role = toks['by_role']['implementer']
        self.assertEqual(role['total_tokens'], sum(U1.values()))
        self.assertEqual(role['cache_read_input_tokens'], U1['cache_read_input_tokens'])
        self.assertIs(toks['usage_complete'], True)
        elapsed = summary['elapsed']
        self.assertGreaterEqual(elapsed['seconds'], 0)
        self.assertTrue(elapsed.get('first_turn_started_at'))

    def test_the_cap_annotation_is_in_the_summary(self):
        _, _, _, run = self.judged('claude-arm1-core01', {})
        self.assertIn('cap', run['result_summary'])
        self.assertIsNone(run['result_summary']['cap'])                         # uncapped run

    def test_a_capped_runs_summary_carries_its_cap(self):
        root = make_root(self.base / 'capped')
        run_id = 'claude-arm1-core01'
        init_run(root, run_id, 1)
        first_turn(root, run_id, 1, usage=tokens(HALF_CAP - 10, TOKEN_CAP - HALF_CAP - 10))
        folder = write_record(root, run_id, 1)
        drive_turn(root, run_id, resume=True, kind='feedback', reported=tokens(500, 500),
                   projects={'-workspace/s.jsonl': [assistant('last', OPUS, tokens(500, 500))]})
        self.assertIsNotNone(censored_record(root / '.runs' / run_id))
        write_verdict(folder, run_id, results={'C20': 'FAIL'})
        code, out = close_and_finish(root, run_id)
        self.assertEqual(code, 0, out)
        run = read_record(folder)
        self.assertEqual(run['status'], 'CENSORED_BUDGET')
        cap = run['result_summary']['cap']
        self.assertTrue(cap, 'a capped run has no cap annotation in its result summary')
        self.assertIn('CENSORED_BUDGET', json.dumps(cap))
        self.assertIn(980, numbers_in(cap))


_saved_environ = {}
_no_docker = None


def setUpModule():
    global _no_docker
    import shutil as _shutil  # noqa: F401  (kept local: the scripts module does not import shutil)
    _no_docker = tempfile.mkdtemp(prefix='nodocker')
    blocked = os.path.join(_no_docker, 'docker')
    with open(blocked, 'w') as f:
        f.write('#!/bin/sh\necho "$*" >> %s/called\necho "spec tests: docker is blocked" >&2\nexit 125\n' % _no_docker)
    os.chmod(blocked, 0o755)
    for key in ('DOCKER_HOST', 'PATH', 'DOCKER_CONTEXT'):
        _saved_environ[key] = os.environ.get(key)
    os.environ['DOCKER_HOST'] = 'unix://%s/no-daemon.sock' % _no_docker
    os.environ.pop('DOCKER_CONTEXT', None)
    os.environ['PATH'] = _no_docker + os.pathsep + (os.environ.get('PATH') or '/usr/bin:/bin')


def tearDownModule():
    import shutil as _shutil
    for key, value in _saved_environ.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    called = os.path.join(_no_docker, 'called')
    calls = open(called).read() if os.path.exists(called) else ''
    _shutil.rmtree(_no_docker, ignore_errors=True)
    if calls:
        raise AssertionError('a test called the real docker command line: %s' % calls[:500])


if __name__ == '__main__':
    unittest.main()
