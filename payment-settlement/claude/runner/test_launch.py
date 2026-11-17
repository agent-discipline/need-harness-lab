import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('launch', Path(__file__).with_name('launch.py'))
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
USAGE = {'input_tokens': 1, 'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 3, 'output_tokens': 4, 'extra': 9}
SELFTEST_OK = {'passed': True, 'selftest_failed': '0'}

def manifest(arm=1, **extra):
    data = {'run_id': 'r', 'arm': arm, 'workspace': '/tmp/run/workspace', 'config_volume': 'cfg-vol', 'image': 'runner:test',
            'requested_model': 'model-x', 'requested_worker_model': 'worker-y' if arm == 3 else None,
            'session_id': '11111111-2222-4333-8444-555555555555', 'codex_volume': 'codex-vol' if arm == 3 else None,
            'harness': '/h' if arm == 3 else None}
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
        self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL', ' '.join(cmd))
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

    def test_arm3_requires_codex_auth_worker_model_no_socket(self):
        with self.assertRaises(ValueError):
            launch.docker_command(manifest(3), Path('/e'), 'n', resume=False)
        cmd = launch.docker_command(manifest(3), Path('/e'), 'n', resume=False, codex_auth=Path('/private/auth.json'))
        mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertEqual(len(mounts), 4)
        self.assertIn('type=bind,src=/private/auth.json,dst=/home/node/.codex/auth.json,readonly', mounts)
        self.assertIn('CLAUDE_CODE_SUBAGENT_MODEL=worker-y', cmd)
        self.assertNotIn('--tools', cmd)
        self.assertNotIn('docker.sock', ' '.join(cmd))

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

    def test_arm3_seeds_harness_and_records_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '3/core1').mkdir(parents=True)
            (root / '3/core1/starter.kt').write_text('starter')
            harness = root / 'harness-source'
            for rel in ('src', 'hooks', 'playbooks', 'templates'):
                (harness / rel).mkdir(parents=True)
            (harness / 'src/core.md').write_text('core')
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
            self.assertIn('core.md', c['harness_seed']['sources'])
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
            big.write_bytes(b'x' * (launch.SCAN_CHUNK - 3) + b'codex-secret-value-1234567890' + b'y' * 10)   # straddles a chunk boundary
            (Path(tmp) / 'clean.bin').write_bytes(b'z' * (2 * launch.SCAN_CHUNK + 7))
            hits = launch.scrub(tmp, ['secret-value', 'codex-secret-value-1234567890'])
            self.assertEqual(hits, ['big.bin'])
            self.assertNotIn(b'codex-secret-value-1234567890', big.read_bytes())
            auth = Path(tmp) / 'auth.json'
            auth.write_text(json.dumps({'tokens': {'access_token': 'A' * 40, 'refresh_token': 'R' * 30, 'account_id': 'short'}, 'last_refresh': '2026-09-23T00:00:00Z'}))
            self.assertEqual(sorted(launch.codex_secrets(auth)), sorted(['A' * 40, 'R' * 30, '2026-09-23T00:00:00Z']))
            auth.write_text('not json')
            with self.assertRaises(ValueError):
                launch.codex_secrets(auth)

    def test_dry_run_validates_env_and_never_calls_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            env = env_file(tmp, 'CLAUDE_CODE_OAUTH_TOKEN=DO-NOT-PRINT\n')
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, codex_auth=None, prompt=prompt, resume=False,
                                   dry_run=True, cache_policy=None, timeout_seconds=1, max_turns=None, max_budget_usd=None)
            with patch.object(launch.subprocess, 'run') as run:
                launch.turn(args)
                run.assert_not_called()
            args.resume = True
            with self.assertRaises(ValueError):
                launch.turn(args)
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
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, codex_auth=None, prompt=prompt, resume=False,
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
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, codex_auth=None, prompt=prompt, resume=False,
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
            args.resume = True
            with patch.object(launch.subprocess, 'run', side_effect=failing_create), patch.object(launch, 'volume_listing', return_value={}), patch.object(launch, 'session_exists', return_value=True):
                with self.assertRaises(RuntimeError):
                    launch.turn(args)
            self.assertTrue((dest / 'turns/0003/result.json').exists())
            self.assertFalse((dest / 'request-ids.txt').exists())

    def test_arm3_codex_attempt_without_session_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            (dest / 'workspace').mkdir()
            env = env_file(tmp)
            auth = root / 'auth.json'
            auth.write_text('{}')
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', manifest(3, run_id='a', workspace=str(dest / 'workspace')))
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, codex_auth=auth, prompt=prompt, resume=False,
                                   dry_run=False, cache_policy='observed-cache', timeout_seconds=1, max_turns=None, max_budget_usd=None)
            good = {'type': 'assistant', 'requestId': 'req-ok', 'sessionId': 's', 'message': {'model': 'm', 'usage': USAGE}}
            def fake_run(cmd, **kw):
                if cmd[:2] == ['docker', 'create']:
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                if cmd[:2] == ['docker', 'start']:
                    kw['stdout'].write(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': manifest()['session_id'],
                                                   'usage': {k: USAGE[k] for k in launch.USAGE_KEYS}, 'total_cost_usd': 0.1}).encode() + b'\n')
                    return SimpleNamespace(returncode=0)
                if cmd[:2] == ['docker', 'inspect']:
                    return SimpleNamespace(returncode=0, stdout=b'[]', stderr=b'')
                if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/projects'):
                    target = Path(cmd[-1]) / '-workspace'
                    target.mkdir(parents=True)
                    (target / 's.jsonl').write_text(json.dumps(good) + '\n')
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('/sessions'):
                    return SimpleNamespace(returncode=1, stdout=b'', stderr=b'No such file or directory')
                if cmd[:2] == ['docker', 'cp'] and cmd[2].endswith('wrapper-calls.log'):
                    Path(cmd[-1]).write_text('2026-09-23T00:00:00Z-77 started argc=6 stripped=1\n')   # interrupted: no exit line
                    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            with patch.object(launch.subprocess, 'run', side_effect=fake_run), patch.object(launch, 'volume_listing', return_value={}), \
                 patch.object(launch, 'session_exists', return_value=False), patch.object(launch, 'selftest', return_value=SELFTEST_OK):
                launch.turn(args)
            result = json.loads((dest / 'turns/0001/result.json').read_text())
            self.assertEqual(result['codex_sessions']['attempts_this_turn'], 1)
            self.assertEqual(result['codex_sessions']['finished_total'], 0)
            self.assertTrue(result['reviewer_usage_pending'])
            self.assertFalse(result['usage_complete'])
            self.assertEqual(result['status'], 'ok')

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
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, codex_auth=None, prompt=prompt, resume=False,
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
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, codex_auth=None, prompt=prompt, resume=False,
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
            args = SimpleNamespace(runs=root, run_id='a', env_file=env, codex_auth=None, prompt=prompt, resume=False,
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

if __name__ == '__main__':
    unittest.main()
