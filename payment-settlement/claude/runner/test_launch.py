import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
import contextlib
from datetime import timedelta
import io
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('launch', Path(__file__).with_name('launch.py'))
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
USAGE = {'input_tokens': 1, 'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 3, 'output_tokens': 4, 'extra': 9}
SELFTEST_OK = {'passed': True, 'selftest_failed': '0'}
# docker cp's stderr for a missing source path: Docker 29.1.3 (observed in the 2026-11-17 arm 3 smoke) and older clients
DOCKER29_MISSING = 'Error response from daemon: Could not find the file %s in container payment-claude-call-0'
OLD_MISSING = b'Error: No such container:path: payment-claude-call-0:/home/node/.fable-review/x: No such file or directory'

def manifest(arm=1, **extra):
    data = {'run_id': 'r', 'arm': arm, 'workspace': '/tmp/run/workspace', 'config_volume': 'cfg-vol', 'image': 'runner:test',
            'requested_model': 'model-x', 'requested_worker_model': 'worker-y' if arm == 3 else None,
            'session_id': '11111111-2222-4333-8444-555555555555', 'reviewer_volume': 'rev-vol' if arm == 3 else None,
            'requested_reviewer_model': 'reviewer-z' if arm == 3 else None, 'harness': '/h' if arm == 3 else None}
    data.update(extra)
    return data

def env_file(root, content='CLAUDE_CODE_OAUTH_TOKEN=secret-value\n', mode=0o600):
    path = Path(root) / 'claude.env'
    path.write_text(content)
    os.chmod(path, mode)
    return path

class RunnerTest(unittest.TestCase):
    def test_commands_isolate_state_and_token(self):
        cmd = launch.docker_command(manifest(), Path('/private/claude.env'), 'name', resume=False)
        mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertEqual(len(mounts), 2)
        self.assertIn('type=volume,src=cfg-vol,dst=/home/node/.claude', mounts)
        self.assertNotIn('docker.sock', ' '.join(cmd))
        self.assertIn('--env-file', cmd)
        self.assertIn('HOME=/home/node', cmd)
        self.assertIn('CLAUDE_CONFIG_DIR=/home/node/.claude', cmd)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', ' '.join(cmd))
        self.assertNotIn('CLAUDE_CODE_SUBAGENT', ' '.join(cmd))
        self.assertIn('--session-id', cmd)
        self.assertNotIn('--resume', cmd)
        self.assertNotIn('--bare', cmd)
        self.assertNotIn('--permission-mode', cmd)
        self.assertIn('--dangerously-skip-permissions', cmd)
        self.assertIn('--verbose', cmd)
        self.assertEqual(cmd[cmd.index('--effort') + 1], 'high')
        allowed = cmd[cmd.index('--tools') + 1].split(',')
        self.assertNotIn('--disallowedTools', cmd)
        self.assertTrue(set(allowed) <= set(launch.ALLOWED_TOOLS_ARM12))
        self.assertFalse(set(allowed) & set(launch.NO_SUBAGENT_TOOLS))
        for name in ('Skill', 'SendMessage', 'CronCreate', 'EnterWorktree'):
            self.assertNotIn(name, allowed)
        resumed = launch.docker_command(manifest(), Path('/private/claude.env'), 'name', resume=True, max_turns=3, max_budget_usd=1.5)
        self.assertEqual(resumed[resumed.index('--resume') + 1], manifest()['session_id'])
        self.assertNotIn('--session-id', resumed)
        self.assertEqual(resumed[resumed.index('--max-turns') + 1], '3')
        self.assertEqual(resumed[resumed.index('--max-budget-usd') + 1], '1.5')

    def test_every_arm_waits_for_its_background_tasks(self):
        # pilot 02: -p stopped a running subagent 10 min after the last answer; the variable lifts that ceiling
        for arm in (1, 2, 3):
            cmd = launch.docker_command(manifest(arm), Path('/e'), 'n', resume=False)
            self.assertIn(launch.BG_WAIT_ENV + '=0', cmd)
            self.assertLess(cmd.index(launch.BG_WAIT_ENV + '=0'), cmd.index('runner:test'))   # container env, kept by lab-arm3-start
        self.assertNotIn('unset ' + launch.BG_WAIT_ENV, (Path(launch.__file__).with_name('lab-arm3-start.sh')).read_text())

    def test_killed_background_tasks_are_visible_in_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'stdout.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in (
                {'type': 'system', 'subtype': 'task_updated', 'task_id': 't1', 'patch': {'status': 'killed'}},
                {'type': 'system', 'subtype': 'task_updated', 'task_id': 't2', 'patch': {'status': 'completed'}},
                {'type': 'result', 'session_id': 's', 'usage': {k: 0 for k in launch.USAGE_KEYS}})))
            (out / 'stderr.txt').write_text('Background tasks still running 10m after the last turn (subagent "T01 scaffold"); '
                                            'stopping them. Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely.\n')
            killed = launch.background_tasks_killed(out, launch.collect(out / 'stdout.jsonl')['background_tasks_killed'])
            self.assertEqual(killed, [{'source': 'stream task_updated', 'task_id': 't1', 'description': None},
                                      {'source': 'stderr', 'tasks': 'subagent "T01 scaffold"'}])
            (out / 'stderr.txt').write_text('')
            self.assertEqual(launch.background_tasks_killed(out, []), [])

    def test_arm3_reviewer_volume_worker_model_no_socket(self):
        with self.assertRaises(ValueError):
            launch.docker_command(manifest(3, reviewer_volume=None), Path('/e'), 'n', resume=False)
        with self.assertRaises(ValueError):
            launch.docker_command(manifest(3, requested_reviewer_model=None), Path('/e'), 'n', resume=False)
        cmd = launch.docker_command(manifest(3), Path('/e'), 'n', resume=False)
        mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertEqual(len(mounts), 3)
        self.assertIn('type=volume,src=rev-vol,dst=/home/node/.fable-review', mounts)
        self.assertIn('LAB_REVIEWER_MODEL=reviewer-z', cmd)
        self.assertIn('LAB_REVIEWER_CONFIG_DIR=/home/node/.fable-review', cmd)
        self.assertNotIn('codex', ' '.join(cmd))
        self.assertNotIn('bind,src=', ' '.join(m for m in mounts if 'workspace' not in m))   # no credential file mounted
        self.assertIn('CLAUDE_CODE_SUBAGENT_MODEL=worker-y', cmd)
        self.assertIn('CLAUDE_CODE_SUBAGENT_EFFORT=high', cmd)
        tools = cmd[cmd.index('--tools') + 1].split(',')              # arm 3 = arms 1/2 list + subagent surface
        self.assertEqual(set(tools) - set(launch.SUBAGENT_TOOLS), set(launch.ALLOWED_TOOLS_ARM12))
        self.assertEqual(set(tools) & set(launch.SUBAGENT_TOOLS), set(launch.SUBAGENT_TOOLS))
        self.assertNotIn('docker.sock', ' '.join(cmd))
        # arm 3 starts as root with three capabilities only, through lab-arm3-start (helper first, then the PM as node)
        self.assertEqual(cmd[cmd.index('--user') + 1], '0:0')
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == '--cap-add'], list(launch.ARM3_CAPS))
        self.assertLess(cmd.index('--cap-drop=ALL'), cmd.index('--cap-add'))
        self.assertIn('--security-opt=no-new-privileges', cmd)
        image = cmd.index('runner:test')
        self.assertEqual(cmd[image + 1:image + 3], [launch.ARM3_START, 'claude'])
        # arms 1/2 never receive the reviewer switch, run as the image user and start claude directly (unchanged)
        for arm in (1, 2):
            plain = launch.docker_command(manifest(arm), Path('/e'), 'n', resume=False)
            self.assertNotIn('LAB_REVIEWER', ' '.join(plain))
            self.assertNotIn('--user', plain)
            self.assertNotIn('--cap-add', plain)
            self.assertEqual(plain[plain.index('runner:test') + 1], 'claude')

    def test_initialization_distinct_no_overwrite_git_init_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / '1/core1'
            source.mkdir(parents=True)
            (source / 'build').mkdir()
            (source / 'build/leak.txt').write_text('exclude')
            (source / 'starter.kt').write_text('starter')
            (source / 'experiment').mkdir()
            (source / 'experiment/private.txt').write_text('do not expose evaluator records')
            args = SimpleNamespace(runs=root / '.runs', run_id='a', arm=1, core=1, image='image', harness=None, network=None)
            with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
                 patch.object(launch, 'selftest', return_value=SELFTEST_OK):
                launch.initialize(args)
                with self.assertRaises(ValueError):
                    launch.initialize(args)
                args.run_id = 'b'
                launch.initialize(args)
                args.run_id = 'c'
                with patch.object(launch, 'selftest', return_value={'passed': False}):
                    with self.assertRaises(RuntimeError):
                        launch.initialize(args)
                self.assertFalse((root / '.runs/c').exists())
                self.assertEqual([p.name for p in (root / '.runs').iterdir() if p.name.startswith('.staging')], [])
            a = json.loads((root / '.runs/a/manifest.json').read_text())
            b = json.loads((root / '.runs/b/manifest.json').read_text())
            self.assertNotEqual(a['config_volume'], b['config_volume'])
            self.assertNotEqual(a['session_id'], b['session_id'])
            self.assertEqual(a['requested_model'], 'claude-opus-5-5')
            self.assertEqual(a['subagents'], 'disabled')
            self.assertEqual(a['workspace'], str(root / '.runs/a/workspace'))
            self.assertFalse((root / '.runs/a/workspace/build').exists())
            self.assertFalse((root / '.runs/a/workspace/experiment').exists())
            self.assertTrue((root / '.runs/a/workspace/.git').is_dir())

    def test_after_the_freeze_init_uses_only_the_frozen_runner_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '1/core1').mkdir(parents=True)
            (root / '1/core1/starter.kt').write_text('starter')
            def init(run_id, image=None, readiness=None):
                args = SimpleNamespace(runs=root / '.runs', run_id=run_id, arm=1, core=1, image=image, harness=None,
                                       network=None, readiness=readiness)
                with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
                     patch.object(launch, 'selftest', return_value=SELFTEST_OK) as st, contextlib.redirect_stdout(io.StringIO()):
                    launch.initialize(args)
                    self.assertEqual(st.call_args[0][0]['image'], json.loads((root / '.runs' / run_id / 'manifest.json').read_text())['image'])
                return json.loads((root / '.runs' / run_id / 'manifest.json').read_text())
            smoke = init('smoke')                                       # before the freeze: the CLI-version tag
            self.assertEqual((smoke['image'], smoke['image_frozen']), ('payment-claude-runner:' + launch.CLI_VERSION, False))
            readiness = root / 'docs/results/readiness.json'
            readiness.parent.mkdir(parents=True)
            readiness.write_text(json.dumps({'image_digests': {'runner': 'sha256:frozen'}, 'contract_sha256': 'c' * 64}))
            pilot = init('pilot')                                       # after the freeze: the frozen digest by default
            self.assertEqual((pilot['image'], pilot['image_frozen'], pilot['readiness']), ('sha256:frozen', True, str(readiness.resolve())))
            self.assertEqual((smoke['contract_sha256'], pilot['contract_sha256']), (None, 'c' * 64))   # the freeze the run ran under
            self.assertEqual(init('same', image='sha256:frozen')['image'], 'sha256:frozen')
            for other in ('payment-claude-runner:' + launch.CLI_VERSION, 'sha256:other'):
                with self.assertRaises(ValueError) as ctx:
                    init('x', image=other)
                self.assertIn('frozen runner digest', str(ctx.exception))
            self.assertFalse((root / '.runs/x').exists())
            readiness.write_text(json.dumps({'image_digests': {}}))
            with self.assertRaises(ValueError):
                init('y')

    def test_refreeze_smoke_runs_outside_the_freeze_only_while_nothing_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '1/core1').mkdir(parents=True)
            (root / '1/core1/starter.kt').write_text('starter')
            for arm in (1, 2, 3):                                 # the evidence experiment_started reads: nothing started
                for core in range(1, 11):
                    rec = root / 'docs/experiments' / str(arm) / ('core%d' % core)
                    rec.mkdir(parents=True)
                    (rec / 'run.json').write_text(json.dumps({'status': 'NOT_STARTED'}))
            (root / 'docs/results').mkdir(parents=True, exist_ok=True)
            (root / 'docs/results/cycles.json').write_text(json.dumps([{'cycle': c, 'status': 'NOT_STARTED'} for c in range(1, 11)]))
            git = ['git', '-C', str(root), '-c', 'user.email=a@b', '-c', 'user.name=a']
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', 'setup'], check=True)
            readiness = root / 'docs/results/readiness.json'
            readiness.write_text(json.dumps({'image_digests': {'runner': 'sha256:frozen'}, 'contract_sha256': 'c' * 64}))
            def init(run_id, image=None, refreeze=True, pilot=False, explicit=None):
                args = SimpleNamespace(runs=root / '.runs', run_id=run_id, arm=1, core=1, image=image, harness=None,
                                       network=None, readiness=explicit, pilot=pilot, refreeze_smoke=refreeze)
                with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
                     patch.object(launch, 'selftest', return_value=SELFTEST_OK), contextlib.redirect_stdout(io.StringIO()):
                    launch.initialize(args)
                return json.loads((root / '.runs' / run_id / 'manifest.json').read_text())
            new = init('smoke-v3r3-arm1', image='sha256:new')     # a previous freeze is in place: the new image anyway
            self.assertEqual((new['image'], new['image_frozen'], new['readiness'], new['contract_sha256'], new['refreeze_smoke']),
                             ('sha256:new', False, None, None, True))
            self.assertEqual(init('smoke-v3r3-arm2')['image'], 'payment-claude-runner:' + launch.CLI_VERSION)
            plain = init('plain', refreeze=False)                 # without the flag nothing changes: the frozen digest
            self.assertEqual((plain['image'], plain['image_frozen'], plain['refreeze_smoke']), ('sha256:frozen', True, False))
            with self.assertRaisesRegex(ValueError, 'frozen runner digest'):
                init('plain2', image='sha256:new', refreeze=False)
            refusals = {'not a smoke id': dict(run_id='claude-arm1-core01'), 'pilot': dict(run_id='smoke-p', pilot=True),
                        'with --readiness': dict(run_id='smoke-r', explicit=readiness)}
            for name, kw in refusals.items():
                with self.subTest(name), self.assertRaises(ValueError):
                    init(**kw)
            with self.assertRaises(ValueError):                    # a pilot id is refused by its own rule as well
                init('claude-pilot-09', pilot=True)
            (root / 'docs/experiments/2/core1/run.json').write_text(json.dumps({'status': 'RUNNING'}))   # a measured run started
            with self.assertRaisesRegex(ValueError, 'measured run has started'):
                init('smoke-late')
            self.assertFalse((root / '.runs/smoke-late').exists())
            with self.assertRaisesRegex(ValueError, 'refreeze smoke'):  # never finished or committed as a run or pilot
                launch.open_record(new, 'smoke-v3r3-arm1', root)

    def test_arm3_seeds_harness_and_records_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '3/core1').mkdir(parents=True)
            (root / '3/core1/starter.kt').write_text('starter')
            harness = root / 'harness-source'
            for rel in ('src', 'hooks', 'playbooks', 'templates', 'agents'):
                (harness / rel).mkdir(parents=True)
            (harness / 'src/core.md').write_text('core')
            (harness / 'HISTORY.md').write_text('history')
            (harness / 'agents/explorer.md').write_text('explorer')
            (harness / 'hooks/gate-guard.sh').write_text('#!/bin/bash')
            (harness / 'playbooks/review.md').write_text('review')
            (harness / 'templates/log.md').write_text('log')
            (root / 'runner').mkdir()
            (root / 'runner/harness-bootstrap-CLAUDE.md').write_text('@core.md')
            (root / 'runner/harness-settings.json').write_text('{}')
            args = SimpleNamespace(runs=root / '.runs', run_id='c', arm=3, core=1, image='image', harness=harness, network=None)
            with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
                 patch.object(launch, 'seed_harness', return_value='listing') as seed, \
                 patch.object(launch, 'selftest', return_value=SELFTEST_OK):
                launch.initialize(args)
                seed.assert_called_once()
            c = json.loads((root / '.runs/c/manifest.json').read_text())
            self.assertEqual(c['requested_model'], 'claude-fable-5-2')
            self.assertEqual(c['requested_worker_model'], 'claude-opus-5-5')
            self.assertEqual(c['subagents'], 'opus-high')
            self.assertEqual(c['requested_reviewer_model'], 'claude-fable-5-2')
            self.assertTrue(c['reviewer_volume'].startswith('payment-claude-reviewer-'))
            self.assertNotIn('codex_volume', c)
            self.assertIn('core.md', c['harness_seed']['sources'])
            flat = launch.expected_harness_hashes(c)
            self.assertIn('agents/explorer.md', flat)                    # v4.3 subagent definition is seeded and hash-checked
            self.assertIn('HISTORY.md', flat)
            self.assertIn('settings.json', c['harness_seed']['runner'])
            args2 = SimpleNamespace(runs=root / '.runs', run_id='d', arm=3, core=1, image='image', harness=None, network=None)
            with patch.object(launch, 'ROOT', root), self.assertRaises(ValueError):
                launch.initialize(args2)

    def test_env_file_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = env_file(tmp, mode=0o644)
            with self.assertRaises(ValueError):
                launch.read_token(env)
            os.chmod(env, 0o600)
            self.assertEqual(launch.read_token(env), 'secret-value')
            env.write_text('CLAUDE_CODE_OAUTH_TOKEN=x\nOTHER=y\n')
            with self.assertRaises(ValueError):
                launch.read_token(env)
            env.write_text('# comment\nCLAUDE_CODE_OAUTH_TOKEN=\n')
            with self.assertRaises(ValueError):
                launch.read_token(env)
            env.write_text('CLAUDE_CODE_OAUTH_TOKEN=a\nCLAUDE_CODE_OAUTH_TOKEN=b\n')
            with self.assertRaises(ValueError):
                launch.read_token(env)
            fake_repo = Path(tmp) / 'repo'
            fake_repo.mkdir()
            inside = fake_repo / 'claude.env'
            inside.write_text('CLAUDE_CODE_OAUTH_TOKEN=x\n')
            os.chmod(inside, 0o600)
            with patch.object(launch, 'REPO', fake_repo.resolve()), self.assertRaises(ValueError):
                launch.read_token(inside)

    def test_collect_parses_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / 'stdout.jsonl'
            lines = [{'type': 'system', 'subtype': 'init'},
                     {'type': 'assistant', 'message': {'model': 'm1', 'usage': {}}},
                     'not json',
                     {'type': 'result', 'subtype': 'success', 'session_id': 's', 'total_cost_usd': 0.5,
                      'usage': {k: USAGE[k] for k in launch.USAGE_KEYS}, 'modelUsage': {'m1': {}}, 'num_turns': 2, 'duration_ms': 10}]
            raw.write_text('\n'.join(l if isinstance(l, str) else json.dumps(l) for l in lines) + '\n')
            summary = launch.collect(raw)
            self.assertEqual(summary['result']['total_cost_usd'], 0.5)
            self.assertEqual(summary['assistant_messages'], 1)
            self.assertEqual(summary['unparsed_lines'], 1)
            self.assertEqual(summary['stream_models'], ['m1'])

    def test_events_dedupe_copies_roles_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / 'projects/-workspace'
            (projects / 'sub').mkdir(parents=True)
            other = dict(USAGE, output_tokens=99)
            main = [{'type': 'user', 'requestId': 'ignored'},
                    {'type': 'assistant', 'requestId': 'req-1', 'sessionId': 's', 'message': {'model': 'm1', 'usage': USAGE}},
                    {'type': 'assistant', 'requestId': 'req-1', 'sessionId': 's', 'message': {'model': 'm1', 'usage': USAGE}},
                    {'type': 'assistant', 'requestId': 'req-old', 'sessionId': 's', 'message': {'model': 'm1', 'usage': USAGE}},
                    {'type': 'assistant', 'requestId': 'req-bad', 'sessionId': 's', 'message': {'model': 'm1', 'usage': {'input_tokens': 1}}},
                    {'type': 'assistant', 'requestId': 'req-conflict', 'sessionId': 's', 'message': {'model': 'm1', 'usage': USAGE}},
                    {'type': 'assistant', 'sessionId': 's', 'message': {'model': 'm1', 'usage': USAGE}}]
            side = [{'type': 'assistant', 'requestId': 'req-2', 'isSidechain': True, 'sessionId': 's2', 'message': {'model': 'm2', 'usage': USAGE}},
                    {'type': 'assistant', 'requestId': 'req-conflict', 'sessionId': 's2', 'message': {'model': 'm1', 'usage': other}}]
            copy = [{'type': 'assistant', 'requestId': 'req-2', 'sessionId': 's3', 'message': {'model': 'm2', 'usage': USAGE}}]
            (projects / 'session.jsonl').write_text('\n'.join(json.dumps(l) for l in main) + '\nbroken\n')
            (projects / 'sub/agent.jsonl').write_text('\n'.join(json.dumps(l) for l in side) + '\n')
            (projects / 'sub/copy.jsonl').write_text('\n'.join(json.dumps(l) for l in copy) + '\n')
            events, ids, incomplete, stats = launch.build_events(root / 'projects', {'req-old'}, 'run', '0002', arm=3)
            self.assertEqual(ids, ['req-1', 'req-2', 'req-bad', 'req-conflict'])
            self.assertEqual([e['id'] for e in events], ['req-1', 'req-2'])
            by_id = {e['id']: e for e in events}
            self.assertEqual(by_id['req-1']['role'], 'pm')
            self.assertEqual(by_id['req-2']['role'], 'worker')       # sidechain in any copy wins
            self.assertEqual(by_id['req-2']['copies'], 2)
            self.assertEqual(by_id['req-1']['usage'], {k: USAGE[k] for k in launch.USAGE_KEYS})
            self.assertEqual({i['requestId']: i['reason'] for i in incomplete},
                             {'req-bad': 'usage or cache counters missing', 'req-conflict': 'usage differs between transcript copies'})
            self.assertEqual(stats['unparsable_lines'], 1)
            self.assertEqual(stats['assistant_without_requestId'], 1)
            self.assertEqual(stats['files'], 3)
            self.assertEqual(launch.sum_usage(events), {'input_tokens': 2, 'cache_creation_input_tokens': 4, 'cache_read_input_tokens': 6, 'output_tokens': 8})
            events1, _, _, _ = launch.build_events(root / 'projects', set(), 'run', '0001', arm=1)
            self.assertEqual({e['role'] for e in events1}, {'implementer', 'worker'})
            out = root / 'out'
            out.mkdir()
            mapping = launch.write_role_files(out, events)
            self.assertEqual(sorted(mapping), ['pm', 'worker'])
            self.assertEqual(len((out / mapping['worker']).read_text().splitlines()), 1)

    def test_web_tool_calls_and_effort_are_observed_per_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / 'projects/-workspace'
            (projects / 's/subagents').mkdir(parents=True)
            web = {'type': 'tool_use', 'id': 'tu-1', 'name': 'WebSearch', 'input': {}}
            fetch = {'type': 'tool_use', 'id': 'tu-2', 'name': 'WebFetch', 'input': {}}
            main = [{'type': 'assistant', 'requestId': 'r-pm', 'sessionId': 's', 'perTurnEffort': 'high',
                     'message': {'model': 'p', 'usage': USAGE, 'content': [{'type': 'text', 'text': 'x'}]}},
                    {'type': 'assistant', 'requestId': 'r-pm', 'sessionId': 's', 'perTurnEffort': 'high',
                     'message': {'model': 'p', 'usage': USAGE, 'content': [web]}}]
            worker = [{'type': 'assistant', 'requestId': 'r-w', 'sessionId': 's', 'isSidechain': True,
                       'message': {'model': 'w', 'usage': USAGE, 'content': [fetch, web | {'id': 'tu-3'}]}}]
            (projects / 's.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in main))
            (projects / 's/subagents/a.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in worker * 2))  # copies
            events, _, _, _ = launch.build_events(Path(tmp) / 'projects', set(), 'r', '0001', 3)
            seen = launch.observations(events, True)
            self.assertEqual(seen['by_role'], {'pm': {'web_tool_uses': 1, 'efforts': ['high']},
                                               'worker': {'web_tool_uses': 2, 'efforts': ['unobserved']}})
            self.assertFalse(launch.observations(events, False)['web_tool_uses_complete'])

    def test_subagent_type_comes_from_the_transcript_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / 'projects/-workspace/s/subagents'
            sub.mkdir(parents=True)
            for name, agent, model in (('agent-a1', 'explorer', 'claude-sonnet-5'), ('agent-a2', 'general-purpose', 'claude-opus-5-5'),
                                       ('agent-a3', None, 'claude-opus-5-5')):
                (sub / (name + '.jsonl')).write_text(json.dumps({'type': 'assistant', 'requestId': 'r-' + name, 'isSidechain': True,
                                                                 'sessionId': 's', 'message': {'model': model, 'usage': USAGE}}) + '\n')
                if agent:
                    (sub / (name + '.meta.json')).write_text(json.dumps({'agentType': agent}))
            events, _, _, _ = launch.build_events(Path(tmp) / 'projects', set(), 'r', '0001', 3)
            self.assertEqual({e['id']: (e['role'], e['agent_type']) for e in events},
                             {'r-agent-a1': ('worker', 'explorer'), 'r-agent-a2': ('worker', 'general-purpose'),
                              'r-agent-a3': ('worker', None)})
            self.assertEqual(launch.USER_PROXY_MODEL, 'claude-opus-5-5')

    def test_inspect_redaction_and_scrub(self):
        raw = json.dumps([{'Config': {'Env': ['CLAUDE_CODE_OAUTH_TOKEN=secret-value', 'PATH=/bin']}}]).encode()
        redacted = launch.redact_inspect(raw)
        self.assertNotIn(b'secret-value', redacted)
        self.assertIn(b'CLAUDE_CODE_OAUTH_TOKEN=<redacted>', redacted)
        self.assertNotIn(b'secret', launch.redact_inspect(b'garbage secret-value'))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'workspace').mkdir()
            (Path(tmp) / 'workspace/notes.txt').write_bytes(b'x secret-value y')
            (Path(tmp) / 'clean.txt').write_bytes(b'nothing')
            (Path(tmp) / 'workspace/dir-secret-value').mkdir()
            (Path(tmp) / 'workspace/dir-secret-value/f-secret-value.txt').write_bytes(b'clean')
            hits = launch.scrub(tmp, 'secret-value')
            self.assertEqual(sorted(hits), ['workspace/dir-<REDACTED-SECRET>', 'workspace/dir-<REDACTED-SECRET>/f-<REDACTED-SECRET>.txt', 'workspace/notes.txt'])
            self.assertFalse(any('secret-value' in h for h in hits))
            self.assertEqual((Path(tmp) / 'workspace/notes.txt').read_bytes(), b'x <REDACTED-SECRET> y')
            self.assertTrue((Path(tmp) / 'workspace/dir-REDACTED-SECRET/f-REDACTED-SECRET.txt').exists())
            self.assertEqual([p for p in Path(tmp).rglob('*') if 'secret-value' in p.name], [])

    def test_scrub_needle_set_and_streaming(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / 'big.bin'
            big.write_bytes(b'x' * (launch.SCAN_CHUNK - 3) + b'other-secret-value-1234567890' + b'y' * 10)   # straddles a chunk boundary
            (Path(tmp) / 'clean.bin').write_bytes(b'z' * (2 * launch.SCAN_CHUNK + 7))
            hits = launch.scrub(tmp, ['secret-value', 'other-secret-value-1234567890'])
            self.assertEqual(hits, ['big.bin'])
            self.assertNotIn(b'other-secret-value-1234567890', big.read_bytes())

    def test_dry_run_validates_env_and_never_calls_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            env = env_file(tmp, 'CLAUDE_CODE_OAUTH_TOKEN=DO-NOT-PRINT\n')
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, prompt=prompt, resume=False,
                                   dry_run=True, cache_policy=None, timeout_seconds=1, max_turns=None, max_budget_usd=None)
            with patch.object(launch.subprocess, 'run') as run:
                launch.turn(args)
                run.assert_not_called()
            args.resume, args.kind = True, 'feedback'               # no session yet: resume is refused
            with self.assertRaises(ValueError):
                launch.turn(args)
            args.kind = None
            os.chmod(env, 0o644)
            args.resume = False
            with self.assertRaises(ValueError):
                launch.turn(args)

    def test_leak_in_preserved_files_fails_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            env = env_file(tmp)
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, prompt=prompt, resume=False,
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=1, max_turns=None, max_budget_usd=None)
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'create']:
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                if cmd[:2] == ['docker', 'start']:
                    kw['stdout'].write(b'{"type":"result","subtype":"success","session_id":"11111111-2222-4333-8444-555555555555","usage":{},"total_cost_usd":0}\n')
                    kw['stderr'].write(b'leaked secret-value here\n')
                    return SimpleNamespace(returncode=0)
                if cmd[:2] == ['docker', 'inspect']:
                    return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'skipped')
            with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'session_exists', return_value=False):
                with self.assertRaises(RuntimeError) as ctx:
                    launch.turn(args)
            self.assertIn('leaked', str(ctx.exception))
            self.assertTrue((dest / 'turns/0001/SECRET-LEAK.txt').exists())
            self.assertNotIn(b'secret-value', (dest / 'turns/0001/stderr.txt').read_bytes())
            result = json.loads((dest / 'turns/0001/result.json').read_text())
            self.assertFalse(result['usage_complete'])
            self.assertEqual(result['cli_status'], 'ok')
            self.assertEqual(result['status'], 'failed')          # a detected leak fails the turn, whatever the CLI said
            self.assertEqual(result['secret_redactions'], ['turns/0001/stderr.txt'])
            self.assertTrue(result['session_id_confirmed'])
            self.assertIn('transcript copy failed', result['incomplete'][0]['reason'])
            cross = result['usage_cross_check']                     # unread transcripts give unknown sums, not zeros
            self.assertIsNone(cross['main_sum'])
            self.assertIsNone(cross['all_sum'])
            self.assertIsNone(cross['reviewer_sum'])
            self.assertTrue(cross['sums_are_lower_bounds'])
            self.assertFalse((dest / 'request-ids.txt').exists() and (dest / 'request-ids.txt').read_text().strip())

    def test_failed_turn_leaves_stub_and_next_turn_gets_new_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            env = env_file(tmp)
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, prompt=prompt, resume=False,
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=1, max_turns=None, max_budget_usd=None)
            def failing_create(cmd, **kw):
                return SimpleNamespace(returncode=125, stdout=b'', stderr=b'image not found')
            with patch.object(launch.subprocess, 'run', side_effect=failing_create), patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'session_exists', return_value=False):
                with self.assertRaises(RuntimeError):
                    launch.turn(args)
            stub = json.loads((dest / 'turns/0001/result.json').read_text())
            self.assertEqual(stub['status'], 'failed')
            # no session was persisted: a retry is still a first turn and must not collide with the failed directory
            with patch.object(launch.subprocess, 'run', side_effect=failing_create), patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'session_exists', return_value=False):
                with self.assertRaises(RuntimeError):
                    launch.turn(args)
            self.assertTrue((dest / 'turns/0002/result.json').exists())
            # the session DID get persisted by a failed call: the next attempt must resume, a first call is refused
            args.resume = False
            with patch.object(launch, 'session_exists', return_value=True), self.assertRaises(ValueError):
                launch.turn(args)
            args.resume, args.kind = True, 'recovery'               # continues the failed first turn
            with patch.object(launch.subprocess, 'run', side_effect=failing_create), patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'session_exists', return_value=True):
                with self.assertRaises(RuntimeError):
                    launch.turn(args)
            self.assertTrue((dest / 'turns/0003/result.json').exists())
            self.assertEqual(json.loads((dest / 'turns/0003/invocation.json').read_text())['turn_kind'], 'recovery')
            self.assertEqual(launch.limit_state(dest)['turn_kinds']['declared'], {'initial': 2, 'feedback': 0, 'answer': 0, 'recovery': 1})
            self.assertFalse((dest / 'request-ids.txt').exists())

    def arm3_turn(self, root, resume, pm_requests, reviewer_log, reviewer_sessions, reviewer_raw=None, pm_cost=0.1, kind='feedback',
                  wording=None, reviewer_dir_readable=True, pm_results=None):
        """Drive one arm 3 turn with a fake docker: PM transcript, cumulative reviewer call log and reviewer transcripts.
        wording: docker's stderr for a missing path (default: the Docker 29 text); reviewer_dir_readable=False makes the
        copy of the reviewer directory itself fail, so absence cannot be established."""
        dest = root / 'a'
        reported = pm_requests[-1]['message']['usage']
        def missing(cmd):
            text = wording if wording is not None else (DOCKER29_MISSING % cmd[2].split(':', 1)[1]).encode()
            return SimpleNamespace(returncode=1, stdout=b'', stderr=text)
        def fake_run(cmd, **kw):
            if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/.fable-review/.'):
                # the reviewer directory (volume mount point): it holds exactly what the reviewer left
                if not reviewer_dir_readable:
                    return SimpleNamespace(returncode=1, stdout=b'', stderr=b'Error response from daemon: unexpected failure')
                target = Path(cmd[-1])
                if reviewer_log is not None:
                    (target / 'reviewer-calls.log').write_text('' if reviewer_log in ('UNREADABLE', 'NOT_COPIED') else reviewer_log)
                if reviewer_sessions:
                    (target / 'projects').mkdir()
                if reviewer_raw:
                    (target / 'raw').mkdir()
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'create']:
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'start']:
                for usage in pm_results or [reported]:           # pm_results: several result events in one turn
                    kw['stdout'].write(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': manifest()['session_id'],
                                                   'usage': {k: usage[k] for k in launch.USAGE_KEYS}, 'total_cost_usd': pm_cost}).encode() + b'\n')
                return SimpleNamespace(returncode=0)
            if cmd[:2] == ['docker', 'inspect']:
                return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
            if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/.fable-review/raw'):
                if not reviewer_raw:
                    return missing(cmd)
                Path(cmd[-1]).mkdir(parents=True)
                for sid, body in reviewer_raw.items():
                    (Path(cmd[-1]) / (sid + '.json')).write_text(body if isinstance(body, str) else json.dumps(body))
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/.fable-review/reviewer-calls.log'):
                if reviewer_log is None or reviewer_log == 'NOT_COPIED':      # NOT_COPIED: present, yet the copy says missing
                    return missing(cmd)
                if reviewer_log == 'UNREADABLE':
                    return SimpleNamespace(returncode=1, stdout=b'', stderr=b'permission denied')
                Path(cmd[-1]).write_text(reviewer_log)
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/.fable-review/projects'):
                if not reviewer_sessions:
                    return missing(cmd)
                target = Path(cmd[-1]) / '-tmp-mirror'
                target.mkdir(parents=True)
                for sid, lines in reviewer_sessions.items():
                    (target / (sid + '.jsonl')).write_text(''.join(json.dumps(l) + '\n' for l in lines))
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/.claude/projects'):
                target = Path(cmd[-1]) / '-workspace'
                target.mkdir(parents=True)
                (target / 's.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in pm_requests))
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=resume,
                               kind=kind if resume else None,
                               dry_run=False, cache_policy='observed-cache', timeout_seconds=1, max_turns=None, max_budget_usd=None)
        with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'volume_listing', return_value={}), \
             patch.object(launch, 'session_exists', return_value=resume), patch.object(launch, 'selftest', return_value=SELFTEST_OK):
            launch.turn(args)
        turns = sorted((dest / 'turns').iterdir())
        return json.loads((turns[-1] / 'result.json').read_text()), turns[-1]

    def test_arm3_reviewer_usage_is_separate_complete_and_not_recounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            (root / 'prompt').write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(3, run_id='a', workspace=str(dest / 'workspace')))
            rev_usage = dict(USAGE, output_tokens=50)
            sid_a, sid_b = 'aaaaaaaa-0000-4000-8000-000000000001', 'bbbbbbbb-0000-4000-8000-000000000002'
            pm1 = [{'type': 'assistant', 'requestId': 'req-pm1', 'sessionId': manifest()['session_id'], 'message': {'model': 'pm-model', 'usage': USAGE}}]
            rev_a = [{'type': 'assistant', 'requestId': 'req-rev-a', 'sessionId': sid_a, 'message': {'model': 'reviewer-model', 'usage': rev_usage}}]
            log1 = 'T1-10 started session=%s model=reviewer-z\nT1-10 exit=0\n' % sid_a
            result, out = self.arm3_turn(root, False, pm1, log1, {sid_a: rev_a})
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['incomplete'], [])
            self.assertTrue(result['usage_complete'])                       # reviewer usage observed per request
            self.assertFalse(result['reviewer_usage_pending'])
            self.assertEqual(result['roles'], ['pm', 'reviewer'])
            self.assertEqual(result['reviewer']['calls_this_turn'], 1)
            cross = result['usage_cross_check']
            self.assertEqual(cross['main_delta'], {k: 0 for k in launch.USAGE_KEYS})   # reviewer never folded into PM sums
            self.assertEqual(cross['reviewer_sum'], {k: rev_usage[k] for k in launch.USAGE_KEYS})
            self.assertIn('reviewer', result['role_files'])
            rows = [json.loads(l) for l in (out / 'events.role-reviewer.jsonl').read_text().splitlines()]
            self.assertEqual([(r['id'], r['model'], r['role']) for r in rows], [('req-rev-a', 'reviewer-model', 'reviewer')])
            written = json.loads((dest / 'manifest.json').read_text())
            self.assertEqual(written['actual_role_models'], {'pm': ['pm-model'], 'reviewer': ['reviewer-model']})
            self.assertEqual(written['actual_model'], 'pm-model')
            self.assertEqual(json.loads((dest / 'reviewer-calls.json').read_text())['calls'], ['T1-10'])
            # turn 2: the volume still holds call A (already accounted) plus call B that started but left nothing
            pm2 = pm1 + [{'type': 'assistant', 'requestId': 'req-pm2', 'sessionId': manifest()['session_id'], 'message': {'model': 'pm-model', 'usage': USAGE}}]
            log2 = log1 + 'T2-20 started session=%s model=reviewer-z\n' % sid_b
            result2, _ = self.arm3_turn(root, True, pm2, log2, {sid_a: rev_a})
            self.assertEqual(result2['request_ids'], ['req-pm2'])               # req-rev-a is not counted twice
            self.assertEqual(result2['reviewer']['calls_this_turn'], 1)
            reasons = sorted(i['reason'] for i in result2['incomplete'])
            self.assertEqual(reasons, ['reviewer attempt without transcript (usage unobservable)', 'reviewer call did not finish'])
            self.assertTrue(result2['reviewer_usage_pending'])
            self.assertFalse(result2['usage_complete'])
            self.assertEqual(result2['status'], 'ok')
            events, conflicts = launch.reconcile_events(dest)
            self.assertEqual(sorted((e['id'], e['role']) for e in events), [('req-pm1', 'pm'), ('req-pm2', 'pm'), ('req-rev-a', 'reviewer')])
            self.assertEqual(conflicts, [])

    def arm3_dest(self, root):
        dest = root / 'a'
        (dest / 'turns').mkdir(parents=True)
        (dest / 'workspace').mkdir()
        (root / 'prompt').write_text('hello')
        launch.write_json(dest / 'manifest.json', manifest(3, run_id='a', workspace=str(dest / 'workspace')))
        return dest

    def test_reviewer_cost_is_recovered_and_the_run_ledger_covers_every_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.arm3_dest(root)
            sid = 'aaaaaaaa-0000-4000-8000-000000000011'
            pm = [{'type': 'assistant', 'requestId': 'req-pm', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': USAGE}}]
            rev = [{'type': 'assistant', 'requestId': 'req-rv', 'sessionId': sid, 'message': {'model': 'r', 'usage': USAGE}}]
            log = 'T1-1 started session=%s model=reviewer-z\nT1-1 exit=0\n' % sid
            result, _ = self.arm3_turn(root, False, pm, log, {sid: rev}, reviewer_raw={sid: {'result': 'x', 'total_cost_usd': 0.25}})
            self.assertEqual(result['reviewer']['cost_usd'], 0.25)
            self.assertEqual(result['cost'], {'pinned_session_usd': 0.1, 'pinned_session_usd_semantics': 'cumulative',
                                              'reviewer_usd': 0.25, 'reviewer_applicable': True, 'cost_complete': True})
            # a user-proxy consultation of the same run lands in the same ledger
            consult = dest / 'consults/0001'
            consult.mkdir(parents=True)
            (consult / 'events.jsonl').write_text(json.dumps({'type': 'message', 'id': 'req-px', 'model': 'x', 'role': 'user-proxy',
                                                             'turn': 'proxy', 'usage': {k: USAGE[k] for k in launch.USAGE_KEYS}}) + '\n')
            (consult / 'usage.json').write_text(json.dumps({'usage_complete': True, 'cost_usd': 0.05}))
            ledger = launch.write_ledger(dest)
            self.assertEqual(sorted(ledger['roles']), ['pm', 'reviewer', 'user-proxy'])
            self.assertEqual(ledger['total_tokens'], 3 * 10)
            self.assertEqual(ledger['cost_usd'], 0.4)
            self.assertTrue(ledger['usage_complete'])
            self.assertEqual(json.loads((dest / 'ledger.json').read_text())['roles']['user-proxy']['requests'], 1)

    def test_several_result_events_in_one_turn_are_combined_for_the_cross_check(self):
        # pilot 01 turn 0002: the session resumed after a background worker and reported a second result event
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / 'stdout.jsonl'
            u1 = {'input_tokens': 22, 'cache_creation_input_tokens': 5, 'cache_read_input_tokens': 7, 'output_tokens': 9,
                  'server_tool_use': {'web_search_requests': 1, 'web_fetch_requests': 0}}
            u2 = {'input_tokens': 8, 'cache_creation_input_tokens': 1, 'cache_read_input_tokens': 2, 'output_tokens': 3,
                  'server_tool_use': {'web_search_requests': 0, 'web_fetch_requests': 2}}
            raw.write_text(''.join(json.dumps(e) + '\n' for e in (
                {'type': 'result', 'session_id': 's', 'total_cost_usd': 7.8, 'usage': u1, 'modelUsage': {'m': {'costUSD': 7.8}}},
                {'type': 'result', 'session_id': 's', 'total_cost_usd': 7.8, 'usage': u2, 'modelUsage': {'m': {'costUSD': 7.8}}})))
            summary = launch.collect(raw)
            self.assertEqual(summary['result_count'], 2)
            self.assertEqual(len(summary['results']), 2)
            combined = summary['result']
            self.assertEqual({k: combined['usage'][k] for k in launch.USAGE_KEYS},
                             {'input_tokens': 30, 'cache_creation_input_tokens': 6, 'cache_read_input_tokens': 9, 'output_tokens': 12})
            self.assertEqual(combined['usage']['server_tool_use'], {'web_fetch_requests': 2, 'web_search_requests': 1})
            self.assertEqual(combined['total_cost_usd'], 7.8)                    # cumulative: the last value, never added
            self.assertEqual(combined['session_ids'], ['s'])
            # one event without the four counters: the combined usage is unknown, not a partial sum
            raw.write_text(json.dumps({'type': 'result', 'session_id': 's', 'usage': u1}) + '\n' +
                           json.dumps({'type': 'result', 'session_id': 's', 'usage': {}}) + '\n')
            self.assertIsNone(launch.collect(raw)['result']['usage'])
            # results of two different sessions never stand for the pinned one
            raw.write_text(json.dumps({'type': 'result', 'session_id': 's', 'usage': u1}) + '\n' +
                           json.dumps({'type': 'result', 'session_id': 't', 'usage': u2}) + '\n')
            self.assertIsNone(launch.collect(raw)['result']['session_id'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.arm3_dest(root)
            first = dict(USAGE, input_tokens=5)
            pm = [{'type': 'assistant', 'requestId': 'req-1', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': first}},
                  {'type': 'assistant', 'requestId': 'req-2', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': USAGE}}]
            result, _ = self.arm3_turn(root, False, pm, None, {}, pm_results=[first, USAGE])
            cross = result['usage_cross_check']
            self.assertEqual(cross['result_events'], 2)
            self.assertEqual(cross['main_delta'], {k: 0 for k in launch.USAGE_KEYS})
            self.assertTrue(result['usage_complete'])
            self.assertTrue(result['session_id_confirmed'])
            # the former behaviour (last event only) would have left a delta equal to the first event's usage
            self.assertNotEqual(cross['reported']['input_tokens'], USAGE['input_tokens'])

    def test_run_ledger_cost_follows_the_cumulative_session_value(self):
        # pilot 01: turn 1 reported 1.9925, turn 2 reported 7.8483 (cumulative); the ledger must not add them
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a'
            def turn_result(n, cumulative, reviewer=0.0, failed=()):
                t = dest / 'turns' / ('%04d' % n)
                t.mkdir(parents=True, exist_ok=True)
                (t / 'result.json').write_text(json.dumps({
                    'status': 'ok', 'usage_complete': True,
                    'reviewer': {'calls_failed_this_turn': len(failed), 'failed_attempts_without_requests': list(failed)},
                    'cost': {'pinned_session_usd': cumulative, 'reviewer_usd': reviewer, 'reviewer_applicable': True}}))
                (t / 'invocation.json').write_text(json.dumps({'turn_kind': 'initial' if n == 1 else 'answer'}))
            turn_result(1, 1.9925464)
            turn_result(2, 7.8483476, reviewer=0.25, failed=[{'call': 'c1', 'session': 's1'}, {'call': 'c2', 'session': 's2'}])
            consult = dest / 'consults/0001'
            consult.mkdir(parents=True)
            (consult / 'usage.json').write_text(json.dumps({'usage_complete': True, 'cost_usd': 0.5}))
            ledger = launch.run_ledger(dest)
            self.assertEqual(ledger['cost_usd'], round(7.8483476 + 0.25 + 0.5, 6))
            self.assertEqual([r.get('pinned_session_increase_usd') for r in ledger['cost_by_call']], [1.992546, 5.855801, None])
            self.assertIn('cumulative', ledger['cost_semantics'])
            self.assertEqual(ledger['reviewer_calls_failed'], 2)
            self.assertEqual(ledger['reviewer_failed_attempts_without_requests'], 2)
            # an unobserved cumulative value: the run cost is unknown (null), never a partial number
            turn_result(3, None)
            ledger = launch.run_ledger(dest)
            self.assertIsNone(ledger['cost_usd'])
            self.assertEqual(ledger['cost_unobserved'], ['turn/0003'])
            # a cumulative value that went down breaks the semantics: flagged and unobserved, not a negative cost
            turn_result(3, 1.0)
            ledger = launch.run_ledger(dest)
            self.assertIsNone(ledger['cost_usd'])
            self.assertIn('decreased', ledger['cost_by_call'][2]['anomaly'])

    def test_reviewer_attempt_failed_before_any_request_is_usage_zero_not_incomplete(self):
        # pilot 01 turn 0002: two "Not logged in" attempts left transcripts with synthetic assistant lines only
        sids = ('e3e55516-d389-4a9c-9949-3d21f7cdcad2', '5846a3e0-94da-4ba4-a798-452809d5cb06')
        zero = {k: 0 for k in launch.USAGE_KEYS}
        def synthetic(sid):
            return [{'type': 'api-request', 'sessionId': sid, 'params': {'model': 'claude-fable-5-2'}},
                    {'type': 'assistant', 'sessionId': sid, 'message': {'model': '<synthetic>', 'usage': zero,
                                                                         'content': [{'type': 'text', 'text': 'Not logged in'}]}}]
        failed_raw = {'is_error': True, 'result': 'Not logged in', 'total_cost_usd': 0, 'usage': dict(zero, service_tier='standard')}
        log = ''.join('T%d started session=%s model=claude-fable-5-2\nT%d exit=1\n' % (i, sid, i) for i, sid in enumerate(sids))
        pm = [{'type': 'assistant', 'requestId': 'req-pm', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': USAGE}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.arm3_dest(root)
            result, _ = self.arm3_turn(root, False, pm, log, {sid: synthetic(sid) for sid in sids},
                                       reviewer_raw={sid: failed_raw for sid in sids})
            self.assertEqual(result['incomplete'], [])
            self.assertTrue(result['usage_complete'])
            self.assertFalse(result['reviewer_usage_pending'])
            self.assertEqual(result['reviewer']['calls_failed_this_turn'], 2)
            self.assertEqual(result['reviewer']['calls_succeeded_this_turn'], 0)
            self.assertEqual([a['session'] for a in result['reviewer']['failed_attempts_without_requests']], list(sids))
            self.assertEqual(result['reviewer']['cost_usd'], 0)
            ledger = json.loads((dest / 'ledger.json').read_text())
            self.assertEqual(ledger['reviewer_failed_attempts_without_requests'], 2)
            self.assertEqual(ledger['reviewer_calls_failed'], 2)
            # the next turn: the excused transcripts are still in the cumulative tree and still excused
            result2, _ = self.arm3_turn(root, True, pm + [dict(pm[0], requestId='req-pm2')], log,
                                        {sid: synthetic(sid) for sid in sids}, reviewer_raw={sid: failed_raw for sid in sids})
            self.assertEqual(result2['incomplete'], [])
            self.assertEqual(result2['reviewer']['calls_this_turn'], 0)
        # not excused: no raw result, a raw result with usage, or a transcript with a real (uncollected) request
        real_line = {'type': 'assistant', 'sessionId': sids[0], 'message': {'model': 'claude-fable-5-2', 'usage': USAGE}}
        cases = {'raw missing': (None, synthetic(sids[0])),
                 'raw with usage': ({sids[0]: dict(failed_raw, usage=dict(zero, output_tokens=5))}, synthetic(sids[0])),
                 'real request without requestId': ({sids[0]: failed_raw}, synthetic(sids[0]) + [real_line])}
        for name, (raw, lines) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.arm3_dest(root)
                one = 'T0 started session=%s model=claude-fable-5-2\nT0 exit=1\n' % sids[0]
                result, _ = self.arm3_turn(root, False, pm, one, {sids[0]: lines}, reviewer_raw=raw)
                self.assertFalse(result['usage_complete'])
                self.assertIn('reviewer transcript lines without parseable usage or requestId', [i['reason'] for i in result['incomplete']])
                self.assertEqual(result['reviewer']['failed_attempts_without_requests'], [])

    def test_a_cancelled_reviewer_call_keeps_the_turn_incomplete_and_is_never_a_zero_attempt(self):
        sid = 'f0f0f0f0-0000-4000-8000-000000000031'
        zero = {k: 0 for k in launch.USAGE_KEYS}
        synthetic = [{'type': 'assistant', 'sessionId': sid, 'message': {'model': '<synthetic>', 'usage': zero}}]
        raw = {'is_error': True, 'result': 'x', 'total_cost_usd': 0, 'usage': zero}   # even a zero-looking raw result
        log = 'T1 started session=%s model=claude-fable-5-2\nT1 cancelled session=%s reason=client-disconnected\n' % (sid, sid)
        pm = [{'type': 'assistant', 'requestId': 'req-pm', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': USAGE}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.arm3_dest(root)
            result, _ = self.arm3_turn(root, False, pm, log, {sid: synthetic}, reviewer_raw={sid: raw})
            self.assertFalse(result['usage_complete'])
            self.assertTrue(result['reviewer_usage_pending'])
            self.assertIn('reviewer call cancelled (client-disconnected); usage unobserved',
                          [i['reason'] for i in result['incomplete']])
            self.assertNotIn('reviewer call did not finish', [i['reason'] for i in result['incomplete']])
            self.assertEqual(result['reviewer']['failed_attempts_without_requests'], [])
            self.assertEqual(result['reviewer']['calls_cancelled_this_turn'],
                             [{'call': 'T1', 'session': sid, 'reason': 'client-disconnected'}])
            self.assertEqual(result['reviewer']['calls_failed_this_turn'], 1)
            self.assertFalse(json.loads((dest / 'ledger.json').read_text())['usage_complete'])

    def test_refused_reviewer_requests_are_counted_and_do_not_make_the_turn_incomplete(self):
        pm = [{'type': 'assistant', 'requestId': 'req-pm', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': USAGE}}]
        log = 'R1 refused reason=snapshot: symbolic link in the review directory: x\nR2 refused reason=a request carries exactly "cwd" and "prompt"\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.arm3_dest(root)
            result, _ = self.arm3_turn(root, False, pm, log, {})
            self.assertTrue(result['usage_complete'])
            self.assertEqual(result['incomplete'], [])
            self.assertEqual([r['call'] for r in result['reviewer']['requests_refused_this_turn']], ['R1', 'R2'])
            self.assertEqual(result['reviewer']['calls_this_turn'], 0)
            self.assertEqual(json.loads((dest / 'ledger.json').read_text())['reviewer_requests_refused'], 2)
            result2, _ = self.arm3_turn(root, True, pm + [dict(pm[0], requestId='req-pm2')], log + 'R3 refused reason=y\n', {})
            self.assertEqual([r['call'] for r in result2['reviewer']['requests_refused_this_turn']], ['R3'])   # not recounted
            self.assertEqual(json.loads((dest / 'ledger.json').read_text())['reviewer_requests_refused'], 3)

    def test_cost_of_another_session_or_after_a_gap_is_not_attributed(self):
        reported = {'total_cost_usd': 3.0, 'session_ids': ['other']}
        cost = launch.turn_cost(reported, None, 'pinned')
        self.assertIsNone(cost['pinned_session_usd'])                         # another cumulative series
        self.assertIn('other', cost['pinned_session_unobserved'])
        self.assertEqual(launch.turn_cost({'total_cost_usd': 3.0, 'session_ids': ['pinned']}, None, 'pinned')['pinned_session_usd'], 3.0)
        self.assertIsNone(launch.turn_cost({'total_cost_usd': 3.0, 'session_ids': ['pinned', 'other']}, None, 'pinned')['pinned_session_usd'])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a'
            for n, cumulative in ((1, 1.0), (2, None), (3, 4.0), (4, 4.5)):
                t = dest / 'turns' / ('%04d' % n)
                t.mkdir(parents=True)
                (t / 'result.json').write_text(json.dumps({'status': 'ok', 'usage_complete': True,
                                                           'cost': {'pinned_session_usd': cumulative, 'reviewer_applicable': False}}))
                (t / 'invocation.json').write_text(json.dumps({'turn_kind': 'initial' if n == 1 else 'feedback'}))
            ledger = launch.run_ledger(dest)
            rows = ledger['cost_by_call']
            self.assertEqual([r['pinned_session_increase_usd'] for r in rows], [1.0, None, None, 0.5])
            self.assertEqual(rows[2]['unassigned_interval'], {'from_after': 'turn/0002', 'usd': 3.0})   # turns 2+3 together
            self.assertNotIn('unassigned_interval', rows[3])
            self.assertIsNone(ledger['cost_usd'])
            self.assertEqual(ledger['cost_unobserved'], ['turn/0002'])

    def test_unrecovered_reviewer_cost_is_flagged_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.arm3_dest(root)
            sid = 'aaaaaaaa-0000-4000-8000-000000000012'
            pm = [{'type': 'assistant', 'requestId': 'req-pm', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': USAGE}}]
            rev = [{'type': 'assistant', 'requestId': 'req-rv', 'sessionId': sid, 'message': {'model': 'r', 'usage': USAGE}}]
            log = 'T1-1 started session=%s model=reviewer-z\nT1-1 exit=0\n' % sid
            for raw in (None, {sid: 'not json'}, {sid: {'result': 'x'}}):
                for p in list((dest / 'turns').iterdir()):
                    shutil.rmtree(p)
                for name in ('request-ids.txt', 'reviewer-calls.json'):
                    if (dest / name).exists():
                        (dest / name).unlink()
                result, _ = self.arm3_turn(root, False, pm, log, {sid: rev}, reviewer_raw=raw)
                self.assertIsNone(result['reviewer']['cost_usd'])
                self.assertEqual(result['reviewer']['cost_unobserved'], [sid])
                self.assertFalse(result['cost']['cost_complete'])
                self.assertTrue(result['usage_complete'])                  # tokens were observed; only cost is missing
                ledger = json.loads((dest / 'ledger.json').read_text())
                self.assertIsNone(ledger['cost_usd'])
                self.assertEqual(ledger['cost_unobserved'], ['turn/0001'])

    def test_arm3_reviewer_log_unreadable_or_orphan_transcript_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            (root / 'prompt').write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(3, run_id='a', workspace=str(dest / 'workspace')))
            sid = 'cccccccc-0000-4000-8000-000000000003'
            pm1 = [{'type': 'assistant', 'requestId': 'req-pm1', 'sessionId': manifest()['session_id'], 'message': {'model': 'pm-model', 'usage': USAGE}}]
            rev = [{'type': 'assistant', 'requestId': 'req-rev', 'sessionId': sid, 'message': {'model': 'reviewer-model', 'usage': USAGE}}]
            result, _ = self.arm3_turn(root, False, pm1, None, {sid: rev})         # transcript but no call log at all
            self.assertIn('reviewer transcripts exist without any logged reviewer call', [i['reason'] for i in result['incomplete']])
            self.assertEqual(result['usage_cross_check']['reviewer_sum'], {k: USAGE[k] for k in launch.USAGE_KEYS})
            self.assertFalse(result['usage_complete'])
            # an unreadable call log: reviewer usage is unknown, so its sum is None rather than zero
            result_u, _ = self.arm3_turn(root, True, pm1 + [dict(pm1[0], requestId='req-pmu')], 'UNREADABLE', {})
            self.assertIsNone(result_u['usage_cross_check']['reviewer_sum'])
            self.assertFalse(result_u['usage_complete'])
            pm1 = pm1 + [dict(pm1[0], requestId='req-pmu')]
            # no reviewer activity at all: nothing to account for, the turn can be complete
            result2, _ = self.arm3_turn(root, True, pm1 + [dict(pm1[0], requestId='req-pm2')], None, {})
            self.assertEqual(result2['incomplete'], [])
            self.assertTrue(result2['usage_complete'])
            self.assertTrue(result2['reviewer']['log_absent'])

    def test_arm3_reviewer_absence_is_read_from_the_directory_not_from_the_error_text(self):
        sid = 'dddddddd-0000-4000-8000-000000000004'
        pm = [{'type': 'assistant', 'requestId': 'req-pm', 'sessionId': manifest()['session_id'], 'message': {'model': 'p', 'usage': USAGE}}]
        rev = [{'type': 'assistant', 'requestId': 'req-rv', 'sessionId': sid, 'message': {'model': 'r', 'usage': USAGE}}]
        log = 'T1-1 started session=%s model=reviewer-z\nT1-1 exit=0\n' % sid
        no_transcript = 'reviewer attempt without transcript (usage unobservable)'
        log_unknown = 'reviewer call log copy failed; reviewer attempts unknown (docker cp rc=1)'
        copy_failed = 'reviewer transcript copy failed (docker cp rc=1)'
        # (reviewer log, reviewer transcripts, reviewer directory readable) -> (usage_complete, incomplete reasons)
        cases = {
            'no reviewer call': (None, {}, True, True, []),
            'reviewer called and collected': (log, {sid: rev}, True, True, []),
            'reviewer called, transcript missing': (log, {}, True, False, [no_transcript]),
            'no reviewer call, directory unreadable': (None, {}, False, False, [copy_failed, log_unknown]),
        }
        for wording in (None, OLD_MISSING, b''):                 # Docker 29 text, older text, no text at all
            for name, (rlog, sessions, readable, complete, reasons) in cases.items():
                with self.subTest(case=name, wording=wording), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.arm3_dest(root)
                    result, out = self.arm3_turn(root, False, pm, rlog, sessions, wording=wording, reviewer_dir_readable=readable)
                    self.assertEqual(result['usage_complete'], complete)
                    self.assertEqual(sorted(i['reason'] for i in result['incomplete']), sorted(reasons))
                    self.assertEqual(result['reviewer_usage_pending'], not complete)
                    self.assertEqual(result['reviewer']['log_absent'], rlog is None and readable)
                    self.assertEqual(result['reviewer']['log_unknown'], rlog is None and not readable)
                    self.assertEqual(result['status'], 'ok')
                    self.assertFalse([p for p in out.iterdir() if p.name.startswith('.reviewer-dir-')])   # probe removed
                    self.assertIsNone(result['reviewer']['reviewer_dir_leftover'])
        # the temporary copy cannot be removed: never hidden — the turn is incomplete and names the leftover path
        with tempfile.TemporaryDirectory() as tmp, patch.object(launch.shutil, 'rmtree', side_effect=OSError('busy')):
            root = Path(tmp)
            self.arm3_dest(root)
            result, out = self.arm3_turn(root, False, pm, None, {})
            leftover = result['reviewer']['reviewer_dir_leftover']
            self.assertTrue(leftover and leftover.startswith('.reviewer-dir-') and (out / leftover).is_dir())
            self.assertIn({'reason': 'temporary copy of the reviewer directory could not be removed from the turn folder',
                           'path': leftover}, result['incomplete'])
            self.assertFalse(result['usage_complete'])
            self.assertTrue(result['reviewer_usage_pending'])
        # the "No such file" text no longer decides anything: a log the directory does hold is unknown, not absent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.arm3_dest(root)
            result, _ = self.arm3_turn(root, False, pm, 'NOT_COPIED', {}, wording=OLD_MISSING)
            self.assertTrue(result['reviewer']['log_unknown'])
            self.assertIn(log_unknown, [i['reason'] for i in result['incomplete']])
            self.assertFalse(result['usage_complete'])

    def test_forked_session_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            env = env_file(tmp)
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, prompt=prompt, resume=False,
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=1, max_turns=None, max_budget_usd=None)
            good = {'type': 'assistant', 'requestId': 'req-ok', 'sessionId': 'other', 'message': {'model': 'm', 'usage': USAGE}}
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'create']:
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                if cmd[:2] == ['docker', 'start']:
                    kw['stdout'].write(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': 'ffffffff-0000-4000-8000-000000000000',
                                                   'usage': {k: USAGE[k] for k in launch.USAGE_KEYS}, 'total_cost_usd': 0.1}).encode() + b'\n')
                    return SimpleNamespace(returncode=0)
                if cmd[:2] == ['docker', 'inspect']:
                    return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
                if cmd[:2] == ['docker', 'cp']:
                    target = Path(cmd[-1]) / '-workspace'
                    target.mkdir(parents=True)
                    (target / 's.jsonl').write_text(json.dumps(good) + '\n')
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'session_exists', return_value=False):
                launch.turn(args)
            result = json.loads((dest / 'turns/0001/result.json').read_text())
            self.assertFalse(result['session_id_confirmed'])
            self.assertTrue(any('pinned session' in i['reason'] for i in result['incomplete']))
            self.assertFalse(result['usage_complete'])

    def test_scan_failure_and_masked_error_fail_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            env = env_file(tmp)
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, prompt=prompt, resume=False,
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=1, max_turns=None, max_budget_usd=None)
            def failing_create(cmd, **kw):
                return SimpleNamespace(returncode=125, stdout=b'', stderr=b'image not found for secret-value')
            with patch.object(launch.subprocess, 'run', side_effect=failing_create), patch.object(launch, 'volume_listing', return_value={}), \
                 patch.object(launch, 'session_exists', return_value=False), patch.object(launch, 'scrub', side_effect=OSError('disk gone secret-value')):
                with self.assertRaises(RuntimeError):
                    launch.turn(args)
            stub = json.loads((dest / 'turns/0001/result.json').read_text())
            self.assertEqual(stub['status'], 'failed')
            self.assertIn('secret scan failed', stub['error'])
            self.assertNotIn('secret-value', json.dumps(stub))
            self.assertEqual(launch.mask_text('x secret-value y', ['secret-value']), 'x <REDACTED-SECRET> y')

    def test_reconcile_events_across_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            u = {k: USAGE[k] for k in launch.USAGE_KEYS}
            other = dict(u, output_tokens=99)
            t1 = [{'id': 'r1', 'model': 'm', 'usage': u, 'role': 'pm', 'turn': '0001'},
                  {'id': 'r2', 'model': 'm', 'usage': u, 'role': 'pm', 'turn': '0001'},
                  {'id': 'r3', 'model': 'm', 'usage': u, 'role': 'pm', 'turn': '0001'}]
            t2 = [{'id': 'r1', 'model': 'm', 'usage': u, 'role': 'pm', 'turn': '0002'},          # recollected duplicate
                  {'id': 'r2', 'model': 'm', 'usage': u, 'role': 'worker', 'turn': '0002'},      # sidechain copy surfaced later
                  {'id': 'r3', 'model': 'm', 'usage': other, 'role': 'pm', 'turn': '0002'},      # conflicting usage
                  {'id': 'r4', 'model': 'm', 'usage': u, 'role': 'worker', 'turn': '0002'}]
            for name, rows in (('0001', t1), ('0002', t2)):
                (dest / 'turns' / name).mkdir(parents=True)
                (dest / 'turns' / name / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            events, conflicts = launch.reconcile_events(dest)
            self.assertEqual(sorted(e['id'] for e in events), ['r1', 'r2', 'r4'])
            self.assertEqual({e['id']: e['role'] for e in events}, {'r1': 'pm', 'r2': 'worker', 'r4': 'worker'})
            self.assertEqual(conflicts, [{'requestId': 'r3', 'turns': ['0001', '0002']}])

    def test_volume_listing_is_tristate(self):
        with patch.object(launch, 'helper', return_value=(1, '', 'docker down')):
            self.assertIsNone(launch.volume_listing(manifest())['credentials_file_in_volume'])
        with patch.object(launch, 'helper', return_value=(0, 'f ./.claude.json\nf ./.credentials.json\n', '')):
            self.assertTrue(launch.volume_listing(manifest())['credentials_file_in_volume'])

    def test_selftest_requires_web_tools_on_every_arm(self):
        for arm in (1, 2, 3):
            with tempfile.TemporaryDirectory() as tmp:
                captured = {}
                def fake_helper(m, script, mounts, network_none=True):
                    captured['script'] = script
                    return 0, 'cli_tools=Bash,Read\nweb_tool_missing=WebSearch\nweb_tool_missing=WebFetch\nselftest_failed=1\n', ''
                with patch.object(launch, 'helper', side_effect=fake_helper):
                    report = launch.selftest(manifest(arm, workspace=str(Path(tmp) / 'runs/r/workspace')))
                self.assertIn('WEB_TOOLS="WebSearch WebFetch"', captured['script'])
                self.assertIn('web_tool_missing', captured['script'])
                self.assertEqual(report['web_tool_missing'], ['WebSearch', 'WebFetch'])
                self.assertFalse(report['passed'])
        self.assertTrue(set(launch.WEB_TOOLS) <= set(launch.ALLOWED_TOOLS_ARM12))

    def test_selftest_script_holds_arm3_to_base_plus_subagents(self):
        """Run the selftest's tool checks locally against a fake claude that reports a chosen init tool surface."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'bin').mkdir()
            fake = base / 'bin/claude'
            fake.write_text('#!/usr/bin/env python3\nimport json, os\n'
                            'print(json.dumps({"type": "system", "subtype": "init", "tools": os.environ["FAKE_TOOLS"].split(",")}))\n')
            os.chmod(fake, 0o755)
            captured = {}
            def fake_helper(m, script, mounts, network_none=True):
                captured['script'] = script
                return 0, 'selftest_failed=0\n', ''
            with patch.object(launch, 'helper', side_effect=fake_helper):
                launch.selftest(manifest(3, workspace=str(base / 'runs/r/workspace')))
            script = captured['script']
            checks = script[:script.index('# Workspace must be')].replace('/tmp', str(base)) + '\nreport selftest_failed "$fail"\n'
            def run(tools):
                env = dict(os.environ, PATH=str(base / 'bin') + ':' + os.environ['PATH'], FAKE_TOOLS=','.join(tools))
                out = subprocess.run(['bash', '-c', checks], capture_output=True, text=True, env=env).stdout
                return dict(l.split('=', 1) for l in out.splitlines() if '=' in l)
            self.assertEqual(run(launch.ALLOWED_TOOLS_ARM3)['selftest_failed'], '0')
            self.assertEqual(run(launch.ALLOWED_TOOLS_ARM12)['subagent_tool_missing'], 'Task')
            self.assertEqual(run(launch.ALLOWED_TOOLS_ARM3 + ('Skill',))['tool_outside_allowlist'], 'Skill')

    def test_expected_harness_hashes_flatten(self):
        m = {'harness_seed': {'sources': {'core.md': {'core.md': 'a'}, 'hooks': {'gate-guard.sh': 'b', 'tests/lib.sh': 'c'}},
                              'runner': {'CLAUDE.md': {'harness-bootstrap-CLAUDE.md': 'd'}, 'settings.json': {'harness-settings.json': 'e'}}}}
        self.assertEqual(launch.expected_harness_hashes(m), {'core.md': 'a', 'hooks/gate-guard.sh': 'b', 'hooks/tests/lib.sh': 'c', 'CLAUDE.md': 'd', 'settings.json': 'e'})

    def test_successful_turn_ledger_and_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            env = env_file(tmp)
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, prompt=prompt, resume=False,
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=1, max_turns=None, max_budget_usd=None)
            good = {'type': 'assistant', 'requestId': 'req-ok', 'sessionId': 's', 'message': {'model': 'm', 'usage': USAGE}}
            bad = {'type': 'assistant', 'requestId': 'req-bad', 'sessionId': 's', 'message': {'model': 'm', 'usage': {'input_tokens': 1}}}
            side = {'type': 'assistant', 'requestId': 'req-side', 'sessionId': 's', 'isSidechain': True, 'message': {'model': 'm', 'usage': USAGE}}
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'create']:
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                if cmd[:2] == ['docker', 'start']:
                    kw['stdout'].write(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': manifest()['session_id'],
                                                   'usage': {k: USAGE[k] for k in launch.USAGE_KEYS}, 'total_cost_usd': 0.1}).encode() + b'\n')
                    return SimpleNamespace(returncode=0)
                if cmd[:2] == ['docker', 'inspect']:
                    return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
                if cmd[:2] == ['docker', 'cp']:
                    target = Path(cmd[-1]) / '-workspace'
                    target.mkdir(parents=True)
                    (target / 's.jsonl').write_text(json.dumps(good) + '\n' + json.dumps(bad) + '\n' + json.dumps(side) + '\n')
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'session_exists', return_value=False):
                launch.turn(args)
            result = json.loads((dest / 'turns/0001/result.json').read_text())
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['request_ids'], ['req-bad', 'req-ok', 'req-side'])
            self.assertTrue(any('forbids subagents' in i['reason'] and i['requestIds'] == ['req-side'] for i in result['incomplete']))
            self.assertEqual((dest / 'request-ids.txt').read_text().split(), ['req-ok', 'req-side'])   # incomplete requests stay open
            written = json.loads((dest / 'manifest.json').read_text())
            self.assertEqual(written['actual_role_models'], {'implementer': ['m'], 'worker': ['m']})
            self.assertEqual(written['actual_model'], 'm')
            self.assertFalse(result['usage_complete'])                                     # req-bad lacks counters
            self.assertEqual(result['usage_cross_check']['main_delta'], {k: 0 for k in launch.USAGE_KEYS})
            self.assertEqual(result['role_files'], {'implementer': 'events.role-implementer.jsonl', 'worker': 'events.role-worker.jsonl'})

class RunLimitTest(unittest.TestCase):
    """One gate before every paid call: tokens of all roles, wall clock since the first call, evaluator feedback deliveries."""

    def make_run(self, root, ok_turns=1, tokens_per_turn=10, started=None, arm=1):
        dest = root / 'a'
        (dest / 'workspace').mkdir(parents=True)
        launch.write_json(dest / 'manifest.json', manifest(arm, run_id='a', workspace=str(dest / 'workspace')))
        start = started or launch.datetime.now(launch.timezone.utc)
        (dest / 'turns').mkdir()
        for n in range(1, ok_turns + 1):
            t = dest / 'turns' / ('%04d' % n)
            t.mkdir()
            (t / 'invocation.json').write_text(json.dumps({'started_at': start.isoformat(), 'turn_kind': 'initial' if n == 1 else 'feedback'}))
            (t / 'result.json').write_text(json.dumps({'status': 'ok', 'usage_complete': True,
                                                       'cost': {'pinned_session_usd': 0.1, 'reviewer_applicable': False}}))
            usage = {'input_tokens': tokens_per_turn, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 0}
            (t / 'events.jsonl').write_text(json.dumps({'type': 'message', 'id': 'r%d' % n, 'model': 'm', 'role': 'implementer',
                                                       'turn': t.name, 'usage': usage}) + '\n')
        (root / 'prompt').write_text('next')
        return dest

    def call(self, root, resume=True, dry_run=False, kind='feedback'):
        args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=resume, kind=kind,
                               dry_run=dry_run, cache_policy='observed-cache', timeout_seconds=7200, max_turns=None, max_budget_usd=None)
        with patch.object(launch.subprocess, 'run') as run, patch.object(launch, 'session_exists', return_value=resume), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                launch.turn(args)
                error = None
            except RuntimeError as exc:
                error = exc
            return error, run

    def test_token_cap_counts_every_role_and_censors_before_the_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, tokens_per_turn=launch.LIMITS['token_limit'] - 5_000_000)
            consult = dest / 'consults/0001'                         # a user-proxy call pushes the run over the cap
            consult.mkdir(parents=True)
            (consult / 'events.jsonl').write_text(json.dumps({'type': 'message', 'id': 'px', 'model': 'x', 'role': 'user-proxy', 'turn': 'p',
                'usage': {'input_tokens': 5_000_000, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 0}}) + '\n')
            (consult / 'usage.json').write_text(json.dumps({'usage_complete': True, 'cost_usd': 0.0}))
            error, run = self.call(root, dry_run=True)                 # dry run reports, never writes
            self.assertIsNotNone(error)
            self.assertFalse((dest / 'censored.json').exists())
            error, run = self.call(root)
            self.assertIn('CENSORED_BUDGET', str(error))
            run.assert_not_called()
            record = json.loads((dest / 'censored.json').read_text())
            self.assertEqual(record['status'], 'CENSORED_BUDGET')
            self.assertEqual(record['first']['used']['token_limit'], launch.LIMITS['token_limit'])
            self.assertEqual(sorted(p.name for p in (dest / 'turns').iterdir()), ['0001'])   # artifacts kept, no new turn
            error, _ = self.call(root)
            self.assertIn('no further paid call', str(error))

    def test_time_cap_from_first_call_and_timeout_never_exceeds_the_remainder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = launch.datetime.now(launch.timezone.utc)
            dest = self.make_run(root, started=now - timedelta(hours=8, seconds=1))
            error, run = self.call(root)
            self.assertIn('CENSORED_TIME', str(error))
            run.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, started=launch.datetime.now(launch.timezone.utc) - timedelta(seconds=28790))
            state = launch.limit_state(dest)
            self.assertEqual(state['reached'], [])
            self.assertLessEqual(launch.LIMITS['time_limit_seconds'] - state['used']['time_limit_seconds'], 11)

    def test_a_turn_still_running_at_the_run_cap_is_stopped_and_the_run_censored(self):
        # no per-turn limit (decision 2026-11-18): only the 8-hour run cap ends a turn, and it does end it
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, started=launch.datetime.now(launch.timezone.utc) - timedelta(seconds=28800 - 40))
            seen = {}
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'start']:
                    seen['timeout'] = kw.get('timeout')
                    raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
                if cmd[:2] == ['docker', 'stop']:
                    seen['stopped'] = True
                return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
            args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=True, kind='feedback',
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=28800, max_turns=None, max_budget_usd=None)
            with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'session_exists', return_value=True), \
                 patch.object(launch, 'volume_listing', return_value={}), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(RuntimeError):
                launch.turn(args)
            self.assertLessEqual(seen['timeout'], 41)                          # the remaining run time, not 28800
            self.assertTrue(seen.get('stopped'))                               # the container was stopped
            result = json.loads((dest / 'turns/0002/result.json').read_text())
            self.assertEqual((result['stopped_at_run_time_cap'], result['timed_out'], result['status']), (True, True, 'failed'))
            censored = json.loads((dest / 'censored.json').read_text())
            self.assertEqual((censored['status'], censored['first']['when'], censored['first']['call']), ('CENSORED_TIME', 'after call', 'turn'))

    @staticmethod
    def assistant(mid, tokens, parent=None):
        return json.dumps({'type': 'assistant', 'parent_tool_use_id': parent, 'message': {'id': mid, 'usage': {
            'input_tokens': tokens, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 0}}}) + '\n'

    def test_stream_estimate_counts_each_message_once_and_waits_for_whole_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stdout.jsonl'
            est = launch.StreamEstimate(path)
            self.assertEqual(est.update(), 0)                                   # no file yet
            path.write_text(self.assistant('m1', 10) + self.assistant('m1', 10) + self.assistant('s1', 5, parent='toolu_1'))
            self.assertEqual(est.update(), 15)                                  # repeated lines of one message, subagent too
            with path.open('a') as stream:
                stream.write(self.assistant('m2', 7)[:-20])                      # a line still being written
            self.assertEqual(est.update(), 15)
            with path.open('a') as stream:
                stream.write(self.assistant('m2', 7)[-20:])
            self.assertEqual(est.update(), 22)

    def test_reviewer_estimate_counts_only_calls_not_yet_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            for sid, n in (('old', 100), ('new', 7)):
                (raw / (sid + '.json')).write_text(json.dumps({'usage': {k: n for k in launch.USAGE_KEYS}}))
            self.assertEqual(launch.reviewer_estimate(raw, {'old'}), (28, 1))

    def run_monitor(self, lines, limit, failing=False, arm3=False):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            out.mkdir()
            (out / 'stdout.jsonl').write_text(''.join(lines))
            stops = []
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'stop']:
                    stops.append(cmd)
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'')
            monitor = launch.TurnBudgetMonitor('c', out, Path(tmp), base=100, arm3=arm3, limit=limit, interval=0.02)
            with patch.object(launch.subprocess, 'run', side_effect=fake_run):
                if failing:
                    with patch.object(launch.StreamEstimate, 'update', side_effect=OSError('unreadable')):
                        monitor.start()
                        monitor.join(5)
                else:
                    monitor.start()
                    time.sleep(0.3)
                    monitor.done.set()
                    monitor.join(5)
            return monitor, stops

    def test_turn_budget_monitor_stops_at_the_cap_and_only_then(self):
        monitor, stops = self.run_monitor([self.assistant('m1', 900)], limit=1000)
        self.assertEqual(len(stops), 1)                                         # 100 confirmed + 900 in the stream
        self.assertEqual((monitor.stop['tokens'], monitor.stop['confirmed_before_turn'], monitor.stop['turn_stream']), (1000, 100, 900))
        self.assertIn('estimated run tokens reached the token cap', monitor.stop['reason'])
        self.assertIn('lower bound', monitor.stop['method'])
        monitor, stops = self.run_monitor([self.assistant('m1', 899)], limit=1000)
        self.assertEqual((stops, monitor.stop), ([], None))                     # below the cap: the turn goes on
        self.assertEqual(monitor.last['tokens'], 999)
        monitor, stops = self.run_monitor([], limit=10 ** 12, failing=True)     # estimates fail: stopped, never unlimited
        self.assertEqual(len(stops), 1)
        self.assertIn('unavailable %d times' % launch.BUDGET_ESTIMATE_FAILURES, monitor.stop['reason'])
        monitor, stops = self.run_monitor([self.assistant('m1', 900)], limit=1000, arm3=True)   # reviewer copy failed:
        self.assertEqual(len(stops), 1)                                         # still the stream's lower bound
        self.assertEqual((monitor.stop['reviewer_finished_calls'], monitor.stop['reviewer_unobserved']), (None, True))
        self.assertGreaterEqual(monitor.stop['reviewer_copy_failures_in_a_row'], 1)

    def test_a_stream_file_missing_after_the_grace_is_an_estimate_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            est = launch.StreamEstimate(Path(tmp) / 'stdout.jsonl', grace=60)
            self.assertEqual(est.update(), 0)                                   # right after the start: normal
            est.started -= 61
            with self.assertRaises(FileNotFoundError):
                est.update()
            monitor = launch.TurnBudgetMonitor('c', Path(tmp), Path(tmp), base=0, arm3=False, limit=10 ** 12, interval=0.02)
            monitor.stream.started -= launch.STREAM_GRACE_SECONDS + 1
            stops = []
            with patch.object(launch.subprocess, 'run', side_effect=lambda cmd, **kw: stops.append(cmd) or SimpleNamespace(returncode=0)):
                monitor.start()
                monitor.join(5)
            self.assertIn('unavailable', monitor.stop['reason'])
            self.assertFalse(monitor.stop['cap_reached'])                      # a stopped turn, not a capped run
            self.assertEqual(stops[0][:2], ['docker', 'stop'])

    def test_a_hung_reviewer_copy_is_an_estimate_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'stdout.jsonl').write_text(self.assistant('m1', 1))
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'cp']:
                    self.assertTrue(kw.get('timeout'))                          # every monitor command has a limit
                    raise subprocess.TimeoutExpired(cmd, kw['timeout'])
                return SimpleNamespace(returncode=0)
            monitor = launch.TurnBudgetMonitor('c', out, out, base=0, arm3=True, limit=10 ** 12, interval=0.02)
            with patch.object(launch.subprocess, 'run', side_effect=fake_run):
                monitor.start()
                monitor.join(5)
            self.assertIn('unavailable %d times' % launch.BUDGET_ESTIMATE_FAILURES, monitor.stop['reason'])
            self.assertEqual(monitor.stop['container']['stopped_by'], 'stop')

    def test_the_turn_is_stopped_when_the_token_monitor_dies_or_stalls(self):
        class Dead(threading.Thread):
            def __init__(self, stall=False):
                super().__init__(daemon=True)
                self.done, self.stop, self.crash, self.interval = threading.Event(), None, None, 0.01
                self.heartbeat, self.stall = time.monotonic(), stall
            def run(self):
                if self.stall:
                    self.done.wait(5)                                          # alive, but no heartbeat
                else:
                    self.crash = 'RuntimeError: boom'                           # dies at once
        for monitor, word in ((Dead(), 'stopped (RuntimeError: boom)'), (Dead(stall=True), 'gave no heartbeat')):
            stopped = threading.Event()
            calls = []
            def fake_run(cmd, **kw):
                calls.append(cmd[:2])
                if cmd[:2] == ['docker', 'start']:
                    stopped.wait(5)
                    return SimpleNamespace(returncode=143)
                if cmd[:2] == ['docker', 'stop']:
                    stopped.set()
                    return SimpleNamespace(returncode=0)
                return SimpleNamespace(returncode=0)
            with patch.object(launch.subprocess, 'run', side_effect=fake_run):
                proc, timed_out, watchdog = launch.watch_turn('c', b'p', None, None, 100, monitor, poll=0.02)
            self.assertTrue(stopped.is_set())                                   # never a turn without the monitor
            self.assertIn(word, watchdog['reason'])
            self.assertEqual((proc.returncode, timed_out), (143, None))
            monitor.done.set()

    def test_stop_escalates_to_kill_and_reports_a_failure(self):
        outcomes = {'stop': SimpleNamespace(returncode=1), 'kill': SimpleNamespace(returncode=0)}
        with patch.object(launch.subprocess, 'run', side_effect=lambda cmd, **kw: outcomes[cmd[1]]):
            self.assertEqual(launch.stop_container('c')['stopped_by'], 'kill')
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw['timeout'])
        with patch.object(launch.subprocess, 'run', side_effect=hang):
            result = launch.stop_container('c')
        self.assertIn('could not be stopped', result['failed'])
        self.assertTrue(result['stop_timed_out'] and result['kill_timed_out'])

    def test_stops_are_classified_after_the_call(self):
        t0 = launch.datetime(2026, 11, 18, 3, 0, tzinfo=launch.timezone.utc)
        def at(seconds):
            return (t0 + timedelta(seconds=seconds)).isoformat()
        token = {'cap_reached': True, 'tokens': 151, 'container': {'requested_at': at(10), 'stopped_by': 'stop'}}
        # the container had ended on its own (exit 0) before the stop request: a normal turn, no mid-turn stop
        out = launch.classify_stops(0, t0 + timedelta(seconds=5), token, None, False)
        self.assertIsNone(out['cap_stop'])
        self.assertEqual(out['stop_requests_after_normal_end'][0]['cap'], 'token_limit')
        # really stopped: a token cap stop
        out = launch.classify_stops(143, t0 + timedelta(seconds=12), token, None, False)
        self.assertEqual(out['cap_stop']['cap'], 'token_limit')
        # both caps: the earlier one is the cap, the other an annotation
        timeout = {'at': at(4), 'container': {'requested_at': at(4), 'stopped_by': 'stop'}}
        out = launch.classify_stops(124, None, token, timeout, True)
        self.assertEqual(out['cap_stop']['cap'], 'time_limit_seconds')
        self.assertEqual(out['cap_stop']['also'][0]['cap'], 'token_limit')
        # a stop for another reason caps nothing; a requested timeout below the run cap is not the run cap either
        unavailable = {'cap_reached': False, 'reason': 'token estimate unavailable 3 times in a row', 'container': {'requested_at': at(1)}}
        out = launch.classify_stops(143, None, unavailable, None, False)
        self.assertIsNone(out['cap_stop'])
        self.assertEqual(len(out['other_stops']), 1)
        self.assertIsNone(launch.classify_stops(124, None, None, timeout, False)['cap_stop'])
        self.assertEqual(launch.container_finished_at(json.dumps([{'State': {'FinishedAt': '2026-11-18T03:00:05.123456789Z'}}])),
                         t0 + timedelta(seconds=5, microseconds=123456))

    def test_a_turn_that_ended_before_the_token_stop_is_a_normal_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, tokens_per_turn=1000)
            finished = launch.datetime.now(launch.timezone.utc)
            class LateStop(threading.Thread):                                  # the monitor decides only after the end
                def __init__(self, *a, **kw):
                    super().__init__(daemon=True)
                    self.done, self.crash, self.interval, self.failures = threading.Event(), None, 30, 0
                    self.heartbeat, self.last = time.monotonic(), None
                    self.stop = {'cap_reached': True, 'tokens': 10 ** 9,
                                 'container': {'requested_at': (finished + timedelta(seconds=30)).isoformat(), 'stopped_by': 'stop'}}
                def run(self):
                    pass
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'start']:
                    kw['stdout'].write(json.dumps({'type': 'result', 'session_id': manifest()['session_id'],
                                                   'usage': {k: USAGE[k] for k in launch.USAGE_KEYS}}).encode() + b'\n')
                    return SimpleNamespace(returncode=0)
                if cmd[:2] == ['docker', 'inspect']:
                    return SimpleNamespace(returncode=0, stdout=json.dumps([{'State': {'FinishedAt': finished.isoformat().replace('+00:00', 'Z')}}]).encode(), stderr=b'')
                if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/.claude/projects'):
                    target = Path(cmd[-1]) / '-workspace'
                    target.mkdir(parents=True)
                    (target / 's.jsonl').write_text(json.dumps({'type': 'assistant', 'requestId': 'req-x', 'sessionId': manifest()['session_id'],
                                                                'message': {'model': 'm', 'usage': USAGE}}) + '\n')
                return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
            args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=True, kind='feedback',
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=28800, max_turns=None, max_budget_usd=None)
            with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'session_exists', return_value=True), \
                 patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'TurnBudgetMonitor', LateStop), \
                 contextlib.redirect_stdout(io.StringIO()):
                launch.turn(args)
            result = json.loads((dest / 'turns/0002/result.json').read_text())
            self.assertEqual(result['status'], 'ok')
            self.assertIsNone(result['stopped_at_token_cap_estimate'])
            self.assertEqual(result['runner_stops']['stop_requests_after_normal_end'][0]['cap'], 'token_limit')
            self.assertFalse((dest / 'censored.json').exists())                # the cap is left to the ledger

    def test_a_turn_stopped_on_the_token_estimate_is_collected_and_the_run_censored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, tokens_per_turn=1000)
            stopped = threading.Event()
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'start']:
                    kw['stdout'].write(self.assistant('m1', launch.LIMITS['token_limit']).encode())
                    kw['stdout'].flush()
                    stopped.wait(10)                                            # the call runs until the runner stops it
                    return SimpleNamespace(returncode=143)
                if cmd[:2] == ['docker', 'stop']:
                    stopped.set()
                if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/.claude/projects'):
                    target = Path(cmd[-1]) / '-workspace'
                    target.mkdir(parents=True)
                    (target / 's.jsonl').write_text(json.dumps({'type': 'assistant', 'requestId': 'req-x', 'sessionId': manifest()['session_id'],
                                                                'message': {'model': 'm', 'usage': USAGE}}) + '\n')
                return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
            args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=True, kind='feedback',
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=28800, max_turns=None, max_budget_usd=None)
            with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'session_exists', return_value=True), \
                 patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'BUDGET_CHECK_SECONDS', 0.05), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                launch.turn(args)
            self.assertTrue(stopped.is_set())
            result = json.loads((dest / 'turns/0002/result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['stopped_at_token_cap_estimate']['turn_stream'], launch.LIMITS['token_limit'])
            self.assertEqual(result['event_count'], 1)                          # collected like any other turn
            censored = json.loads((dest / 'censored.json').read_text())
            self.assertEqual((censored['status'], censored['first']['when'], censored['first']['call']), ('CENSORED_BUDGET', 'after call', 'turn'))
            stop = censored['first']['stopped_during_call']
            self.assertEqual(stop['cap'], 'token_limit')
            self.assertEqual(stop['basis']['tokens'], 1000 + launch.LIMITS['token_limit'])   # estimate, with its method
            self.assertIn('method', stop['basis'])
            self.assertLess(censored['first']['used']['token_limit'], launch.LIMITS['token_limit'])   # confirmed: the ledger
            error, _ = self.call(root, kind='recovery')
            self.assertIn('no further paid call', str(error))

    def test_remaining_time_is_recomputed_after_the_arm3_selftest(self):
        for selftest_seconds, expect_call in ((90, True), (200, False)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                now = launch.datetime.now(launch.timezone.utc)
                dest = self.make_run(root, started=now - timedelta(seconds=28800 - 120), arm=3)
                first = dest / 'turns/0001/invocation.json'
                def slow_selftest(m):                                    # the selftest consumes wall-clock time
                    data = json.loads(first.read_text())
                    data['started_at'] = (launch.datetime.fromisoformat(data['started_at']) - timedelta(seconds=selftest_seconds)).isoformat()
                    first.write_text(json.dumps(data))
                    return SELFTEST_OK
                calls = []
                def fake_run(cmd, **kw):
                    calls.append((cmd[:2], kw.get('timeout')))
                    return SimpleNamespace(returncode=125, stdout=b'', stderr=b'stop here')
                args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=True, kind='feedback',
                                       dry_run=False, cache_policy='observed-cache', timeout_seconds=7200, max_turns=None, max_budget_usd=None)
                with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'session_exists', return_value=True), \
                     patch.object(launch, 'selftest', side_effect=slow_selftest), patch.object(launch, 'volume_listing', return_value={}), \
                     self.assertRaises(RuntimeError) as ctx:
                    launch.turn(args)
                if expect_call:
                    invocation = json.loads((dest / 'turns/0002/invocation.json').read_text())
                    self.assertLessEqual(invocation['effective_timeout_seconds'], 30)
                    self.assertIn(['docker', 'create'], [c for c, _ in calls])
                else:                                                    # the selftest ran past the cap: censored, no call
                    self.assertIn('CENSORED_TIME', str(ctx.exception))
                    self.assertNotIn(['docker', 'create'], [c for c, _ in calls])
                    self.assertFalse((dest / 'turns/0002').exists())

    def test_failed_precall_selftest_is_recorded_without_a_turn_and_the_next_attempt_is_a_normal_turn(self):
        for failure in ({'passed': False, 'selftest_failed': '1', 'harness_changed': 'hooks/gate-guard.sh'},
                        OSError('docker daemon unreachable')):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest = self.make_run(root, arm=3)
                calls = []
                def fake_run(cmd, **kw):
                    calls.append(cmd[:2])
                    return SimpleNamespace(returncode=125, stdout=b'', stderr=b'stop here')
                args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=True,
                                       kind='feedback', dry_run=False, cache_policy='observed-cache', timeout_seconds=7200,
                                       max_turns=None, max_budget_usd=None)
                st = dict(side_effect=failure) if isinstance(failure, Exception) else dict(return_value=failure)
                with patch.object(launch.subprocess, 'run', side_effect=fake_run), \
                     patch.object(launch, 'session_exists', return_value=True), patch.object(launch, 'selftest', **st), \
                     self.assertRaises((RuntimeError, OSError)):
                    launch.turn(args)
                self.assertEqual(calls, [])                                      # no paid call went out
                self.assertEqual(sorted(p.name for p in (dest / 'turns').iterdir()), ['0001'])   # no turn directory
                failures = launch.precall_selftest_failures(dest)
                self.assertEqual(len(failures), 1)
                self.assertIs(failures[0]['paid_call'], False)
                self.assertEqual(failures[0]['turn_kind_attempted'], 'feedback')
                self.assertIs(failures[0]['hook_selftest_before_call']['passed'], False)
                # counting and the limit state still work; the record is no turn and no delivery
                self.assertEqual([r[0] for r in launch.turn_records(dest)], ['0001'])
                self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 0)
                self.assertTrue(launch.run_ledger(dest)['usage_complete'])
                # the next attempt is a normal feedback turn (not a recovery) and takes the next free number
                self.assertEqual(launch.next_turn_kind(manifest(3), dest, launch.turn_records(dest), True, 'feedback'),
                                 ('feedback', 'feedback'))
                with self.assertRaises(ValueError):
                    launch.next_turn_kind(manifest(3), dest, launch.turn_records(dest), True, 'recovery')
                with patch.object(launch.subprocess, 'run', side_effect=fake_run), \
                     patch.object(launch, 'session_exists', return_value=True), \
                     patch.object(launch, 'selftest', return_value=SELFTEST_OK), \
                     patch.object(launch, 'volume_listing', return_value={}), self.assertRaises(RuntimeError):
                    launch.turn(args)                                            # accepted; the fake docker then fails
                invocation = json.loads((dest / 'turns/0002/invocation.json').read_text())
                self.assertEqual((invocation['turn_kind'], invocation['counts_as']), ('feedback', 'feedback'))

    def test_feedback_cap_is_thirty_follow_up_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, ok_turns=30)                      # initial + 29 feedback turns
            self.assertIsNone(self.call(root, dry_run=True)[0])          # the 30th feedback is allowed
            self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 29)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, ok_turns=31)                      # 30 feedbacks delivered
            error, run = self.call(root)
            self.assertIn('CENSORED_TURNS', str(error))
            run.assert_not_called()
            self.assertEqual(launch.limit_state(dest)['reached'], [])    # a non-feedback call (proxy) is not a 31st prompt

    def test_overshoot_during_a_call_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make_run(root, tokens_per_turn=launch.LIMITS['token_limit'] + 123)
            record = launch.record_overshoot(dest, 'turn')
            self.assertEqual(record['status'], 'CENSORED_BUDGET')
            self.assertEqual(record['first']['overshoot'], {'token_limit': 123})
            self.assertEqual(record['first']['when'], 'after call')
            self.assertEqual(record['first']['call'], 'turn')              # which kind of call went past the cap
            (root / 'b').mkdir()
            self.assertIsNone(launch.record_overshoot(self.make_run(root / 'b', tokens_per_turn=5), 'turn'))
            with self.assertRaises(ValueError):                            # the call kind is never left out
                launch.censor(dest, launch.limit_state(dest), 'after call', None)

class TurnKindTest(unittest.TestCase):
    """Only evaluator feedback deliveries count toward the 30 cap; arm 3 answer turns are counted separately."""

    def make(self, root, turns, arm=3, consults=0):
        """turns: [(turn_kind or None, final status or None)]; one tiny implementer request per turn."""
        dest = root / 'a'
        (dest / 'workspace').mkdir(parents=True)
        launch.write_json(dest / 'manifest.json', manifest(arm, run_id='a', workspace=str(dest / 'workspace')))
        (dest / 'turns').mkdir()
        now = launch.datetime.now(launch.timezone.utc).isoformat()
        for n, (kind, status) in enumerate(turns, 1):
            t = dest / 'turns' / ('%04d' % n)
            t.mkdir()
            invocation = {'started_at': now, 'prompt_sha256': launch.hashlib.sha256(b'next').hexdigest()}
            if kind:
                invocation['turn_kind'] = kind
            (t / 'invocation.json').write_text(json.dumps(invocation))
            if status:
                (t / 'result.json').write_text(json.dumps({'status': status, 'usage_complete': True,
                                                           'cost': {'pinned_session_usd': 0.1, 'reviewer_applicable': False}}))
        for n in range(1, consults + 1):
            c = dest / 'consults' / ('%04d' % n)
            c.mkdir(parents=True)
            (c / 'usage.json').write_text(json.dumps({'usage_complete': True, 'cost_usd': 0.0, 'exit_code': 0}))
            (c / 'response.txt').write_text('답변 %d' % n)
        (root / 'prompt').write_text('next')
        return dest

    def test_only_a_completed_consultation_permits_an_answer_turn(self):
        broken = {'exit 124 (timeout)': ({'exit_code': 124}, '부분 답'), 'empty response': ({'exit_code': 0}, '  \n'),
                  'recorded error': ({'exit_code': 0, 'error': 'docker cp failed'}, '답'),
                  'no exit code (interrupted)': ({}, '답'), 'credential leak': ({'exit_code': 0}, '답')}
        for name, (usage, response) in broken.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest = self.make(root, [('initial', 'ok')], consults=1)
                c = dest / 'consults/0001'
                (c / 'usage.json').write_text(json.dumps(dict(usage, usage_complete=False, cost_usd=None)))
                (c / 'response.txt').write_text(response)
                if name == 'credential leak':
                    (c / 'SECRET-LEAK.txt').write_text('x')
                self.assertFalse(launch.consult_completed(c))
                error, run, _, _ = self.call(root, 'answer', dry_run=False)
                self.assertIn('completed user-proxy consultation', str(error))
                run.assert_not_called()
                ledger = launch.run_ledger(dest)
                self.assertEqual((ledger['consults'], ledger['consults_completed'], ledger['consults_not_completed']), (1, 0, ['0001']))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make(root, [('initial', 'ok')], consults=1)
            self.assertTrue(launch.consult_completed(dest / 'consults/0001'))
            self.assertIsNone(self.call(root, 'answer')[0])
            self.assertEqual(launch.run_ledger(dest)['consults_completed'], 1)

    def test_consultation_without_usage_json_is_an_incomplete_call_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self.make(Path(tmp), [('initial', 'ok')], consults=1)
            (dest / 'consults/0002').mkdir()                            # cut short before usage.json was written
            ledger = launch.run_ledger(dest)
            self.assertIn('consults/0002', ledger['incomplete_calls'])
            self.assertFalse(ledger['usage_complete'])
            self.assertEqual((ledger['consults'], ledger['consults_completed']), (2, 1))

    def call(self, root, kind, resume=True, dry_run=True):
        args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=resume, kind=kind,
                               dry_run=dry_run, cache_policy='observed-cache', timeout_seconds=60, max_turns=None, max_budget_usd=None)
        out = io.StringIO()
        with patch.object(launch.subprocess, 'run') as run, patch.object(launch, 'session_exists', return_value=resume) as exists, \
             contextlib.redirect_stdout(out):
            try:
                launch.turn(args)
                error = None
            except (RuntimeError, ValueError) as exc:
                error = exc
        return error, run, exists, out.getvalue()

    def test_resume_without_kind_is_refused_before_any_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root, [('initial', 'ok')])
            for dry in (True, False):
                error, run, exists, _ = self.call(root, None, dry_run=dry)
                self.assertIn('--kind', str(error))
                run.assert_not_called()
                exists.assert_not_called()                              # refused before even inspecting the volume
            error, *_ = self.call(root, 'feedback', resume=False)        # a first turn never carries a kind
            self.assertIn('first turn is always initial', str(error))

    def test_answer_is_refused_in_arms_1_and_2_and_without_a_consultation(self):
        for arm in (1, 2):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make(root, [('initial', 'ok')], arm=arm, consults=1)
                error, run, _, _ = self.call(root, 'answer', dry_run=False)
                self.assertIn('only in arm 3', str(error))
                run.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root, [('initial', 'ok'), ('answer', 'ok')], consults=1)   # the only consultation was delivered
            error, run, _, _ = self.call(root, 'answer', dry_run=False)
            self.assertIn('undelivered completed user-proxy consultation', str(error))
            run.assert_not_called()

    def test_answer_turns_do_not_count_and_are_reported_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turns = [('initial', 'ok')] + [('answer', 'ok')] * 5 + [('feedback', 'ok')] * 30
            dest = self.make(root, turns, consults=6)
            state = launch.limit_state(dest)
            self.assertEqual(state['used']['feedback_limit'], 30)
            self.assertEqual(state['answer_turns'], 5)
            self.assertEqual(launch.run_ledger(dest)['turn_kinds']['delivered'], {'initial': 1, 'feedback': 30, 'answer': 5})
            error, _, _, printed = self.call(root, 'answer')             # an answer after 30 feedbacks is not capped
            self.assertIsNone(error)
            self.assertEqual(json.loads(printed)['counts_as'], 'answer')
            error, run, _, _ = self.call(root, 'feedback', dry_run=False)   # the 31st feedback is censored, no call
            self.assertIn('CENSORED_TURNS', str(error))
            run.assert_not_called()
            self.assertEqual(json.loads((dest / 'censored.json').read_text())['status'], 'CENSORED_TURNS')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root, [('initial', 'ok')] + [('feedback', 'ok')] * 29)
            self.assertIsNone(self.call(root, 'feedback')[0])           # the 30th feedback is allowed

    def test_recovery_counts_as_the_turn_it_continues_and_only_after_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turns = [('initial', 'failed'), ('recovery', 'ok')] + [('feedback', 'ok')] * 28 + [('feedback', 'failed'), ('recovery', None)]
            dest = self.make(root, turns)
            counts = launch.limit_state(dest)['turn_kinds']
            self.assertEqual(counts['delivered'], {'initial': 1, 'feedback': 28, 'answer': 0})
            self.assertEqual(counts['declared'], {'initial': 1, 'feedback': 29, 'answer': 0, 'recovery': 2})
            error, _, _, printed = self.call(root, 'recovery')           # the 29th feedback delivery is still open
            self.assertIsNone(error)
            self.assertEqual(json.loads(printed)['counts_as'], 'feedback')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make(root, [('initial', 'ok')] + [('feedback', 'ok')] * 29 + [('feedback', 'failed'), ('recovery', 'ok')])
            self.assertEqual(launch.limit_state(dest)['used']['feedback_limit'], 30)   # recovered delivery counted once
            error, run, _, _ = self.call(root, 'recovery', dry_run=False)            # last turn ended ok: nothing to recover
            self.assertIn('recovery continues only', str(error))
            run.assert_not_called()
            self.assertIn('CENSORED_TURNS', str(self.call(root, 'feedback', dry_run=False)[0]))

    def test_after_a_turn_that_did_not_end_ok_only_recovery_is_accepted(self):
        for last in ('failed', None):                                    # None: cut short, no result.json
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest = self.make(root, [('initial', 'ok'), ('feedback', last)], consults=1)
                for kind in ('feedback', 'answer'):
                    error, run, _, _ = self.call(root, kind, dry_run=False)
                    self.assertIn('must be --kind recovery', str(error), (last, kind))
                    run.assert_not_called()
                error, _, _, printed = self.call(root, 'recovery')
                self.assertIsNone(error)
                self.assertEqual(json.loads(printed)['counts_as'], 'feedback')   # the failed feedback is counted once recovered
                self.assertFalse((dest / 'censored.json').exists())

    def test_failed_first_turn_restarts_without_session_or_recovers_with_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make(root, [('initial', 'failed')], arm=1)
            error, _, _, printed = self.call(root, None, resume=False)   # no session left: a new first turn
            self.assertIsNone(error)
            self.assertEqual(json.loads(printed)['turn_kind'], 'initial')
            self.assertIn('must be --kind recovery', str(self.call(root, 'feedback', dry_run=False)[0]))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=True,
                                   kind='recovery', dry_run=False, cache_policy='observed-cache', timeout_seconds=60,
                                   max_turns=None, max_budget_usd=None)
            no_image = lambda cmd, **kw: SimpleNamespace(returncode=125, stdout=b'', stderr=b'no image')
            with patch.object(launch.subprocess, 'run', side_effect=no_image), patch.object(launch, 'session_exists', return_value=True), \
                 patch.object(launch, 'volume_listing', return_value={}), self.assertRaises(RuntimeError):   # accepted, then docker fails
                launch.turn(args)                                           # session left: resume as a recovery
            invocation = json.loads((dest / 'turns/0002/invocation.json').read_text())
            self.assertEqual((invocation['turn_kind'], invocation['counts_as']), ('recovery', 'initial'))

    def test_turn_record_without_kind_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make(root, [('initial', 'ok'), (None, 'ok')])
            with self.assertRaises(RuntimeError) as ctx:
                launch.limit_state(dest)
            self.assertIn('turn_kind', str(ctx.exception))
            ledger = launch.run_ledger(dest)
            self.assertIsNone(ledger['turn_kinds'])                      # never a zero count
            self.assertIn('0002', ledger['turn_kinds_error'])
            error, run, _, _ = self.call(root, 'feedback', dry_run=False)
            self.assertIn('turn_kind', str(error))
            run.assert_not_called()
            self.assertFalse((dest / 'censored.json').exists())
            shutil.rmtree(dest / 'turns/0002')
            (dest / 'turns/0002').mkdir()                                # a turn directory without invocation.json
            with self.assertRaises(RuntimeError):
                launch.limit_state(dest)

    def test_kind_is_stored_with_the_turn_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self.make(root, [('initial', 'ok')], consults=1)
            def failing_create(cmd, **kw):
                return SimpleNamespace(returncode=125, stdout=b'', stderr=b'no image')
            args = SimpleNamespace(runs=root, run_id='a', env_file=env_file(root), prompt=root / 'prompt', resume=True, kind='answer',
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=60, max_turns=None, max_budget_usd=None)
            with patch.object(launch.subprocess, 'run', side_effect=failing_create), patch.object(launch, 'session_exists', return_value=True), \
                 patch.object(launch, 'selftest', return_value=SELFTEST_OK), patch.object(launch, 'volume_listing', return_value={}):
                with self.assertRaises(RuntimeError):
                    launch.turn(args)
            invocation = json.loads((dest / 'turns/0002/invocation.json').read_text())
            self.assertEqual((invocation['turn_kind'], invocation['counts_as']), ('answer', 'answer'))
            self.assertEqual(launch.limit_state(dest)['turn_kinds']['declared']['answer'], 1)
            self.assertEqual(launch.limit_state(dest)['answer_turns'], 0)  # failed delivery: not delivered yet

class CloseoutRunnerTest(unittest.TestCase):
    """Spec 10.2-10.5 on the runner side: cap call kind, pilot location, no run-level evaluation cost, U1/U9/U6."""

    def init(self, root, run_id, pilot=False, readiness=None):
        (root / '3/core1').mkdir(parents=True, exist_ok=True)
        (root / '3/core1/starter.kt').write_text('starter')
        args = SimpleNamespace(runs=root / '.runs', run_id=run_id, arm=3, core=1, image=None, harness=None, network=None,
                               readiness=readiness, pilot=pilot)
        with patch.object(launch, 'ROOT', root), patch.object(launch, 'EXPECTED_UID', os.getuid()), \
             patch.object(launch, 'selftest', return_value=SELFTEST_OK), patch.object(launch, 'seed_harness', return_value=''), \
             contextlib.redirect_stdout(io.StringIO()):
            args.harness = root / 'harness-source'
            (root / 'harness-source').mkdir(exist_ok=True)
            launch.initialize(args)
        return json.loads((root / '.runs' / run_id / 'manifest.json').read_text())

    def test_named_readiness_that_does_not_exist_is_refused(self):           # U1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError) as ctx:
                self.init(root, 'x', readiness=root / 'docs/results/readiness.json')
            self.assertIn('does not exist', str(ctx.exception))
            self.assertFalse((root / '.runs/x').exists())
            self.assertFalse(self.init(root, 'smoke')['image_frozen'])       # default location absent: before the freeze

    def test_pilot_run_id_location_and_record_owner(self):                     # 10.3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for run_id, pilot in (('claude-pilot-1', True), ('pilot', True), ('claude-pilot-01', False)):
                with self.assertRaises(ValueError):
                    self.init(root, run_id, pilot=pilot)
            m = self.init(root, 'claude-pilot-01', pilot=True)
            self.assertIs(m['pilot'], True)
            self.assertEqual(launch.record_folder(m, root), root / 'docs/pilots/claude-pilot-01')
            folder = launch.open_record(m, 'claude-pilot-01', root)            # created on first use
            self.assertEqual(json.loads((folder / 'run.json').read_text())['run_id'], 'claude-pilot-01')
            measured = dict(m, pilot=False)
            self.assertEqual(launch.record_folder(measured, root), root / 'docs/experiments/3/core1')
            with self.assertRaises(ValueError):                                # a measured run's record must exist
                launch.open_record(measured, 'claude-pilot-01', root)
            (root / 'docs/experiments/3/core1').mkdir(parents=True)
            (root / 'docs/experiments/3/core1/run.json').write_text(json.dumps({'run_id': 'claude-arm3-core01'}))
            with self.assertRaises(ValueError) as ctx:                         # the folder of another run is refused
                launch.open_record(measured, 'claude-pilot-01', root)
            self.assertIn('belongs to claude-arm3-core01', str(ctx.exception))
            (folder / 'run.json').write_text(json.dumps({'run_id': 'claude-pilot-02'}))
            with self.assertRaises(ValueError):
                launch.open_record(m, 'claude-pilot-01', root)
            with self.assertRaises(ValueError):                                # manifest of another run
                launch.open_record(m, 'claude-pilot-02', root)

    def test_cap_record_names_the_call_kind_before_and_after(self):           # 10.2
        with tempfile.TemporaryDirectory() as tmp:
            dest = RunLimitTest().make_run(Path(tmp), tokens_per_turn=launch.LIMITS['token_limit'])
            with self.assertRaises(RuntimeError):
                launch.limit_gate(dest, next_is_feedback=False, call='user-proxy')
            first = json.loads((dest / 'censored.json').read_text())['first']
            self.assertEqual((first['when'], first['call']), ('before call', 'user-proxy'))

    def test_run_ledger_carries_no_evaluation_cost(self):                     # 10.4
        with tempfile.TemporaryDirectory() as tmp:
            dest = RunLimitTest().make_run(Path(tmp))
            self.assertNotIn('evaluation_cost', launch.run_ledger(dest))
            self.assertFalse(hasattr(launch, 'evaluation_cost'))

    def test_recovery_sends_exactly_the_prompt_of_the_turn_it_continues(self):  # U9
        for dry in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest = TurnKindTest().make(root, [('initial', 'ok'), ('feedback', 'failed')])
                (root / 'prompt').write_text('a different prompt')
                error, run, _, _ = TurnKindTest().call(root, 'recovery', dry_run=dry)
                self.assertIn('prompt of the turn it continues', str(error))
                run.assert_not_called()
                self.assertEqual(sorted(p.name for p in (dest / 'turns').iterdir()), ['0001', '0002'])
                (root / 'prompt').write_text('next')                           # the same bytes: accepted
                self.assertIsNone(TurnKindTest().call(root, 'recovery')[0])
                invocation = dest / 'turns/0002/invocation.json'
                invocation.write_text(json.dumps({'turn_kind': 'feedback', 'started_at': launch.utc()}))
                self.assertIn('prompt', str(TurnKindTest().call(root, 'recovery')[0]))   # no recorded hash: refused

    def test_cost_arguments_are_finite_and_not_negative(self):                # U6
        for good in ('0', '1.5', 3):
            self.assertEqual(launch.cost_value(good), float(good))
        for bad in ('nan', 'inf', '-inf', '-0.01', 'x', None):
            with self.assertRaises(Exception):
                launch.cost_value(bad)
        for bad in ('nan', 'inf', '-1'):
            argv = ['launch.py', 'turn', '--run-id', 'a', '--prompt', '/p', '--env-file', '/e', '--max-budget-usd', bad]
            with patch.object(launch.sys, 'argv', argv), contextlib.redirect_stderr(io.StringIO()), \
                 patch.object(launch, 'turn') as turn, self.assertRaises(SystemExit):
                launch.main()
            turn.assert_not_called()

class TurnTimeoutDefaultTest(unittest.TestCase):
    def test_turn_timeout_defaults_to_the_run_cap(self):
        # decision 2026-11-18: no per-turn limit, only the 8-hour run cap (the runner reduces it to what is left)
        argv = ['launch.py', 'turn', '--run-id', 'a', '--prompt', '/p', '--env-file', '/e']
        with patch.object(launch.sys, 'argv', argv), patch.object(launch, 'turn') as turn:
            launch.main()
        self.assertEqual(turn.call_args[0][0].timeout_seconds, 28800)
        self.assertEqual(launch.LIMITS['time_limit_seconds'], 28800)
        self.assertEqual(launch.call_timeout(28800, {'used': {'time_limit_seconds': 28000}}), 800)

class DiscardTest(unittest.TestCase):
    def run_discard(self, dest, reviewer_entries, rm_rc=0):
        def fake_helper(m, script, mounts, network_none=True):
            if 'rev-vol' in mounts[0]:
                return 0, ''.join(e + '\n' for e in reviewer_entries), ''
            return 0, 'd /vol\n', ''
        args = SimpleNamespace(runs=dest.parent, run_id=dest.name)
        with patch.object(launch, 'helper', side_effect=fake_helper), \
             patch.object(launch.subprocess, 'run', return_value=SimpleNamespace(returncode=rm_rc, stderr='busy')) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                launch.discard(args)
                error = None
            except RuntimeError as exc:
                error = exc
        return error, run

    def test_reviewer_results_must_be_recovered_before_volumes_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a'
            (dest / 'turns/0001/reviewer-raw').mkdir(parents=True)
            launch.write_json(dest / 'manifest.json', manifest(3, run_id='a'))
            sid = 'aaaaaaaa-0000-4000-8000-000000000021'
            entries = ['d /vol', 'd /vol/raw', 'f /vol/raw/%s.json' % sid, 'f /vol/reviewer-calls.log']
            error, run = self.run_discard(dest, entries)
            self.assertIn('not recovered', str(error))
            run.assert_not_called()                                    # nothing removed
            self.assertFalse(launch.discard_complete(dest))
            (dest / 'turns/0001/reviewer-raw' / (sid + '.json')).write_text('{}')
            error, run = self.run_discard(dest, entries, rm_rc=1)       # removal failure is loud
            self.assertIn('could not be removed', str(error))
            self.assertFalse(launch.discard_complete(dest))
            error, run = self.run_discard(dest, entries)
            self.assertIsNone(error)
            self.assertEqual(run.call_count, 2)                        # config + reviewer volume
            self.assertTrue(launch.discard_complete(dest))

class FableReviewHelperTest(unittest.TestCase):
    """runner/fable-review.sh (PM-side client) against runner/fable_review.py serve (reviewer helper) with a fake
    `claude` (no model call). The helper runs as this user here; the separate account is checked in a real container."""
    HERE = Path(__file__).resolve().parent
    WRAPPER = HERE / 'fable-review.sh'
    TOKEN = 'secret-value-123'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = self.base / 'reviewer-cfg'
        self.cfg.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.out = self.base / 'fake-out'
        self.out.mkdir()
        self.root = self.base / 'root'                                        # stands for /tmp and /workspace
        self.mirror = self.root / 'mirror'
        (self.mirror / '_packet').mkdir(parents=True)
        (self.mirror / 'a.txt').write_text('hello\n')
        (self.mirror / '_packet' / 'spec.md').write_text('spec\n')
        for name, content in (('.git/config', '[core]\n\tfsmonitor = touch MARK\n'), ('.claude/settings.json', '{}'),
                              ('.mcp.json', '{}')):
            (self.mirror / name).parent.mkdir(parents=True, exist_ok=True)
            (self.mirror / name).write_text(content)
        self.fake = self.base / 'claude'
        self.reply('{"type":"result","result":"REVIEW TEXT","total_cost_usd":0.2}')
        # stand-in for setpriv (dropping capabilities needs SETPCAP, absent here): records its options, runs the rest
        self.setpriv = self.base / 'setpriv'
        self.setpriv.write_text('#!/bin/bash\nwhile [ "${1#--}" != "$1" ]; do echo "$1" >> "' + str(self.out) +
                                '/setpriv"; shift; done\nexec "$@"\n')
        os.chmod(self.setpriv, 0o755)
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            proc.kill()
            proc.wait()
        self.tmp.cleanup()

    def reply(self, text, rc=0):
        o = str(self.out)
        self.fake.write_text('#!/bin/bash\n'
                             'printf "%s\\n" "$@" > "' + o + '/args"\n'
                             'printf "%s" "$CLAUDE_CONFIG_DIR" > "' + o + '/config"\n'
                             'printf "%s" "${CLAUDE_CODE_OAUTH_TOKEN:-absent}" > "' + o + '/token-env"\n'
                             'IFS= read -r tok < "/proc/self/fd/$CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"; printf "%s" "$tok" > "' + o + '/token-fd"\n'
                             'pwd > "' + o + '/cwd"\n'
                             '(cd . && find . | sort) > "' + o + '/listing"\n'
                             'cat a.txt > "' + o + '/a.txt" 2>/dev/null\n'
                             '(touch ./written 2>/dev/null && echo yes || echo no) > "' + o + '/writable"\n'
                             'echo $$ > "' + o + '/pid"\n'
                             'cat > "' + o + '/stdin"\n'
                             'grep -q SLEEP "' + o + '/stdin" && sleep 60\n'
                             "echo '" + text + "'\n"
                             'exit ' + str(rc) + '\n')
        os.chmod(self.fake, 0o755)

    def serve(self, token=TOKEN, peer_uid=None, config=None, name='s', env_token=None, extra=()):
        sock = self.base / name
        env = {'PATH': os.environ['PATH']}
        if env_token:
            env['CLAUDE_CODE_OAUTH_TOKEN'] = env_token
        read_end, write_end = os.pipe()
        os.write(write_end, (token + '\n').encode())
        os.close(write_end)
        proc = subprocess.Popen([sys.executable, str(self.HERE / 'fable_review.py'), 'serve', '--socket', str(sock),
                                 '--model', 'claude-fable-5-2', '--config-dir', str(config or self.cfg), '--home', str(self.home),
                                 '--cli', str(self.fake), '--setpriv', str(self.setpriv), '--root', str(self.root),
                                 '--token-fd', str(read_end),
                                 '--peer-uid', str(os.getuid() if peer_uid is None else peer_uid)] + list(extra),
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(read_end,))
        os.close(read_end)
        self.procs.append(proc)
        for _ in range(100):
            if sock.exists() or proc.poll() is not None:
                break
            time.sleep(0.05)
        return sock, proc

    def call(self, sock, cwd=None, args=(), prompt='review this', wait=True):
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.base), 'FABLE_REVIEW_SOCKET': str(sock),
               'FABLE_REVIEW_LIB': str(self.HERE)}
        command = ['bash', str(self.WRAPPER)] + list(args)
        if not wait:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, env=env, cwd=str(cwd or self.mirror),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
            return proc
        return subprocess.run(command, input=prompt, env=env, cwd=str(cwd or self.mirror),
                              capture_output=True, text=True, timeout=60)

    def raw_request(self, sock, request):
        import socket as socketlib
        import struct
        with socketlib.socket(socketlib.AF_UNIX) as conn:
            conn.connect(str(sock))
            body = json.dumps(request).encode()
            conn.sendall(struct.pack('>Q', len(body)) + body)
            return json.loads(conn.makefile('rb').read())

    def log_lines(self):
        path = self.cfg / 'reviewer-calls.log'
        return path.read_text().splitlines() if path.exists() else []

    def test_fixed_reviewer_runs_on_a_snapshot_with_the_token_on_a_pipe_only(self):
        sock, _ = self.serve()
        done = self.call(sock)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), 'REVIEW TEXT')
        argv = (self.out / 'args').read_text().split('\n')
        self.assertEqual(argv[:2], ['-p', '--model'])
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-fable-5-2')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Grep,Glob')
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'user')       # no project/local settings
        self.assertIn('--strict-mcp-config', argv)                                 # no MCP server from anywhere
        self.assertEqual((self.out / 'setpriv').read_text().split(),                # the CLI keeps no capability
                         ['--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all'])
        self.assertNotIn('--dangerously-skip-permissions', argv)
        self.assertNotIn('--resume', argv)
        sid = argv[argv.index('--session-id') + 1]
        self.assertEqual((self.out / 'config').read_text(), str(self.cfg))         # never the PM's harness config dir
        self.assertEqual((self.out / 'token-env').read_text(), 'absent')           # not in the CLI's environment
        self.assertEqual((self.out / 'token-fd').read_text(), self.TOKEN)          # only on the descriptor
        cwd = (self.out / 'cwd').read_text().strip()
        self.assertTrue(cwd.startswith(str(self.home / 'snapshots') + '/'), cwd)   # a snapshot, not the PM's directory
        self.assertEqual((self.out / 'listing').read_text().split(), ['.', './_packet', './_packet/spec.md', './a.txt'])
        self.assertEqual((self.out / 'a.txt').read_text(), 'hello\n')              # same content as the mirror
        self.assertEqual((self.out / 'writable').read_text().strip(), 'no')        # read-only like the mirror
        self.assertFalse(Path(cwd).exists())                                       # removed after the call
        self.assertEqual((self.out / 'stdin').read_text(), 'review this')
        lines = self.log_lines()
        self.assertEqual(len(lines), 2)
        started = launch.CALL_STARTED.fullmatch(lines[0])
        self.assertEqual(started.group(2), sid)
        self.assertEqual(launch.CALL_EXIT.fullmatch(lines[1]).groups(), (started.group(1), '0'))
        self.assertEqual(json.loads((self.cfg / 'raw' / (sid + '.json')).read_text())['total_cost_usd'], 0.2)

    def test_failure_exit_code_propagates_and_is_logged(self):
        self.reply('{"type":"result","result":"x"}', rc=3)
        sock, _ = self.serve()
        self.assertEqual(self.call(sock).returncode, 3)
        self.assertTrue(self.log_lines()[-1].endswith(' exit=3'))

    def test_empty_or_error_review_is_a_failure(self):
        sock, _ = self.serve()
        for text in ('{"type":"result","result":""}', '{"type":"result","result":"  "}',
                     '{"type":"result","is_error":true,"result":"Not logged in"}', 'not json'):
            with self.subTest(reply=text):
                self.reply(text)
                done = self.call(sock)
                self.assertEqual(done.returncode, 96, text)
                self.assertIn('no review text', done.stderr)
                self.assertTrue(self.log_lines()[-1].endswith(' exit=96'))

    def test_request_carries_only_cwd_and_prompt_and_anything_else_is_refused(self):
        sock, _ = self.serve()
        outside = self.base / 'elsewhere'
        outside.mkdir()
        self.assertEqual(self.call(sock, cwd=outside).returncode, 94)          # cwd outside the allowed roots
        self.assertEqual(self.call(sock, prompt='  \n').returncode, 94)       # nothing to review
        self.assertEqual(self.call(sock, args=['--model', 'claude-opus-5-5']).returncode, 2)
        for request in ({'cwd': str(self.mirror), 'prompt': 'x', 'model': 'claude-opus-5-5'},
                        {'cwd': str(self.mirror), 'prompt': 'x', 'argv': ['id']}, ['id'], {'cwd': 'mirror', 'prompt': 'x'},
                        {'cwd': str(self.mirror) + '/../mirror', 'prompt': 'x'}, {'cwd': str(self.root) + '/', 'prompt': 'x'}):
            with self.subTest(request=request):
                self.assertEqual(self.raw_request(sock, request)['exit'], 94)
        self.assertFalse((self.out / 'args').exists())                         # the CLI never started
        lines = self.log_lines()                                               # recorded as refusals, never as attempts
        self.assertEqual(len(lines), 8, lines)
        self.assertTrue(all(launch.CALL_REFUSED.fullmatch(l) and not launch.CALL_STARTED.fullmatch(l) for l in lines), lines)
        import socket as socketlib                                             # an empty connection (readiness probe)
        with socketlib.socket(socketlib.AF_UNIX) as conn:                      # is not recorded
            conn.connect(str(sock))
        time.sleep(0.3)
        self.assertEqual(len(self.log_lines()), 8)

    def test_links_special_files_and_oversized_directories_are_refused(self):
        sock, _ = self.serve(extra=['--max-files', '3', '--max-bytes', '100'])
        cases = {}
        linked = self.root / 'linked'                      # a symbolic link inside the directory
        shutil.copytree(self.mirror, linked)
        (linked / 'escape').symlink_to('/')
        cases['symbolic link inside'] = (linked, 'symbolic link')
        (self.root / 'via-link').symlink_to(self.mirror)   # the requested path itself goes through a link (a raw
        reply = self.raw_request(sock, {'cwd': str(self.root / 'via-link'), 'prompt': 'x'})   # request: getcwd resolves)
        self.assertEqual(reply['exit'], 94)
        self.assertIn('without following links', reply['stderr'])
        fifo = self.root / 'fifo'
        shutil.copytree(self.mirror, fifo)
        os.mkfifo(fifo / 'pipe')
        cases['special file'] = (fifo, 'special file')
        many = self.root / 'many'
        many.mkdir()
        for i in range(5):
            (many / ('f%d' % i)).write_text('x')
        cases['too many files'] = (many, 'more than 3 files')
        big = self.root / 'big'
        big.mkdir()
        (big / 'f').write_text('x' * 200)
        cases['too many bytes'] = (big, 'more than 100 bytes')
        for name, (cwd, reason) in cases.items():
            with self.subTest(name):
                done = self.call(sock, cwd=cwd)
                self.assertEqual(done.returncode, 94, done.stderr)
                self.assertIn(reason, done.stderr)
        self.assertFalse((self.out / 'args').exists())
        self.assertEqual(list((self.home / 'snapshots').iterdir()), [])        # refused snapshots are removed too
        reasons = [launch.CALL_REFUSED.fullmatch(l).group(2) for l in self.log_lines()]
        self.assertTrue(reasons and all(r for r in reasons))                   # each refusal recorded with its reason
        self.assertTrue(any('symbolic link' in r for r in reasons))

    def test_only_what_the_requester_could_read_itself_is_copied(self):
        spec_ = importlib.util.spec_from_file_location('fable_review', self.HERE / 'fable_review.py')
        fr = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(fr)
        me, other = os.getuid(), os.getuid() + 1
        src = self.root / 'perm'
        (src / 'sub').mkdir(parents=True)
        (src / 'sub' / 'ok.txt').write_text('ok')
        os.chmod(src / 'sub' / 'ok.txt', 0o644)
        os.chmod(src / 'sub', 0o755)
        os.chmod(src, 0o755)
        def copy(uid, name):
            fd = os.open(str(src), os.O_RDONLY | os.O_DIRECTORY)
            try:
                return fr.snapshot(fd, str(self.base / name), uid)
            finally:
                os.close(fd)
        self.assertEqual(copy(other, 'c1')['files'], 1)                       # world-readable: another uid could read it
        os.chmod(src / 'sub' / 'ok.txt', 0o600)                               # now only its owner can
        self.assertEqual(copy(me, 'c2')['files'], 1)                          # the owner asks: copied
        with self.assertRaisesRegex(ValueError, 'cannot read the file itself: sub/ok.txt'):
            copy(other, 'c3')                                                  # anyone else: refused, never left out
        os.chmod(src / 'sub' / 'ok.txt', 0o644)
        os.chmod(src / 'sub', 0o700)
        with self.assertRaisesRegex(ValueError, 'cannot read the directory itself: sub'):
            copy(other, 'c4')
        os.chmod(src / 'sub', 0o755)
        os.link(src / 'sub' / 'ok.txt', src / 'hard.txt')                      # a hard link is judged by its inode
        os.chmod(src / 'hard.txt', 0o600)
        with self.assertRaisesRegex(ValueError, 'cannot read the file'):
            copy(other, 'c5')
        os.unlink(src / 'hard.txt')
        os.chmod(src / 'sub' / 'ok.txt', 0o644)
        real_fstat = fr.os.fstat
        def other_device(fd):                                                  # a mount point inside the directory
            st = real_fstat(fd)
            if os.readlink('/proc/self/fd/%d' % fd).endswith('/sub'):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
            return st
        with patch.object(fr.os, 'fstat', other_device), self.assertRaisesRegex(ValueError, 'another file system'):
            copy(me, 'c6')
        # the requested directory itself and the way to it follow the same rule
        with self.assertRaisesRegex(ValueError, 'cannot enter'):            # the temporary base is 0700: no way in
            os.close(fr.open_dir(str(src), other))
        os.chmod(self.base, 0o755)
        os.chmod(self.root, 0o755)
        os.close(fr.open_dir(str(src), other))                                # 0755 all the way: another uid could
        os.chmod(src, 0o700)
        with self.assertRaisesRegex(ValueError, 'cannot read'):
            os.close(fr.open_dir(str(src), other))
        os.close(fr.open_dir(str(src), me))

    def test_a_caller_gone_during_the_snapshot_gets_no_cli_and_a_refusal_record(self):
        spec_ = importlib.util.spec_from_file_location('fable_review', self.HERE / 'fable_review.py')
        fr = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(fr)
        reviewer = fr.Reviewer('claude-fable-5-2', str(self.cfg), str(self.home), str(self.fake), 'tok', str(self.setpriv))
        fd = os.open(str(self.mirror), os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assertIsNone(reviewer.run(fd, b'x', lambda: True, os.getuid()))
        finally:
            os.close(fd)
        self.assertFalse((self.out / 'args').exists())                         # the CLI was never started
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(launch.CALL_REFUSED.fullmatch(lines[0]).group(2), 'client disconnected before the reviewer started')

    def test_a_disconnected_caller_cancels_the_call_and_leaves_it_unobserved(self):
        sock, _ = self.serve()
        client = self.call(sock, prompt='SLEEP please', wait=False)
        for _ in range(100):
            if (self.out / 'pid').exists() and (self.out / 'stdin').exists():
                break
            time.sleep(0.05)
        cli_pid = int((self.out / 'pid').read_text())
        client.kill()                                      # e.g. the PM's Bash timed out
        client.wait()
        for _ in range(200):
            if any(' cancelled ' in line for line in self.log_lines()) and not Path('/proc/%d' % cli_pid).exists():
                break
            time.sleep(0.05)
        self.assertFalse(Path('/proc/%d' % cli_pid).exists())                  # the reviewer CLI was stopped
        lines = self.log_lines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(launch.CALL_STARTED.fullmatch(lines[0]))
        hit = launch.CALL_CANCELLED.fullmatch(lines[1])
        self.assertEqual(hit.groups(), (launch.CALL_STARTED.fullmatch(lines[0]).group(1),
                                        launch.CALL_STARTED.fullmatch(lines[0]).group(2), 'client-disconnected'))
        self.assertIsNone(launch.CALL_EXIT.fullmatch(lines[1]))

    def test_refuses_without_helper_or_logging_and_from_another_uid(self):
        done = self.call(self.base / 'no-socket')                              # arms 1/2: no helper in the container
        self.assertEqual(done.returncode, 98)
        self.assertIn('no reviewer is configured', done.stderr)
        blocked = self.base / 'not-a-dir'
        blocked.write_text('file, not a directory')
        sock, _ = self.serve(config=blocked, name='s2')
        self.assertEqual(self.call(sock).returncode, 97)                       # attempt cannot be logged: refused
        sock3, _ = self.serve(peer_uid=os.getuid() + 1, name='s3')
        self.assertEqual(self.call(sock3).returncode, 94)                      # only the PM's uid may ask
        self.assertFalse((self.out / 'args').exists())
        _, proc = self.serve(token='', name='s4')                              # a helper without token does not serve
        self.assertEqual(proc.wait(timeout=30), 90)
        _, proc = self.serve(env_token=self.TOKEN, name='s5')                  # nor one with the token in its environment
        self.assertEqual(proc.wait(timeout=30), 90)

    def test_container_start_scripts_parse(self):
        for script in ('fable-review.sh', 'lab-arm3-start.sh'):
            self.assertEqual(subprocess.run(['bash', '-n', str(self.HERE / script)]).returncode, 0, script)
        dockerfile = (self.HERE / 'Dockerfile').read_text()
        for line in ('COPY fable-review.sh /usr/local/bin/fable-review', 'COPY lab-arm3-start.sh ' + launch.ARM3_START,
                     'COPY fable_review.py /usr/local/lib/fable-review/fable_review.py'):
            self.assertIn(line, dockerfile)
        self.assertIn('--uid %d' % launch.REVIEWER_UID, dockerfile)
        self.assertIn('chown fablereview:fablereview %s && chmod 700 %s' % (launch.REVIEWER_DST, launch.REVIEWER_DST), dockerfile)
        start = (self.HERE / 'lab-arm3-start.sh').read_text()
        self.assertIn('CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3', start)
        self.assertIn('unset CLAUDE_CODE_OAUTH_TOKEN LAB_REVIEWER_MODEL LAB_REVIEWER_CONFIG_DIR', start)
        self.assertIn('exec setpriv --reuid=node --regid=node --init-groups --bounding-set=-all --inh-caps=-all "$@"', start)
        self.assertIn('--token-fd 4', start)
        self.assertIn('chmod o-rwx /workspace', start)                         # the reviewer CLI cannot enter the work folder
        self.assertLess(start.index('chmod o-rwx /workspace'), start.index('exec setpriv --reuid=node'))
        self.assertIn('--ambient-caps=+dac_read_search,+setpcap', start)       # the helper only, not its reviewer CLI
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"', start)           # the helper's environment never gets it

if __name__ == '__main__':
    unittest.main()
