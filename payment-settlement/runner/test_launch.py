import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('launch', Path(__file__).with_name('launch.py'))
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)

class RunnerTest(unittest.TestCase):
    def test_commands_isolate_state_and_auth(self):
        data = {'workspace': '/tmp/run/workspace', 'volume': 'unique-state', 'image': 'runner:test',
                'requested_model': 'explicit-model', 'harness': '/public/harness'}
        cmd = launch.docker_command(data, Path('/private/auth.json'), 'generated-name')
        mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertEqual(len(mounts), 4)
        self.assertIn('type=bind,src=/private/auth.json,dst=/home/node/.codex/auth.json,readonly', mounts)
        self.assertIn('type=volume,src=unique-state,dst=/home/node/.codex', mounts)
        self.assertNotIn('docker.sock', ' '.join(cmd))
        self.assertNotIn('--ephemeral', cmd)
        self.assertIn('--ignore-user-config', cmd)
        self.assertIn('--ignore-rules', cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertEqual(cmd[-1], '-')
        resume = launch.codex_command('explicit-model', 'thread-a')
        self.assertEqual(resume[:4], ['codex', 'exec', 'resume', 'thread-a'])
        self.assertNotIn('--last', resume)

    def test_initialization_distinct_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / '1/core1'
            source.mkdir(parents=True)
            (source / 'build').mkdir()
            (source / 'build/leak.txt').write_text('exclude')
            (source / 'starter.kt').write_text('starter')
            (source / 'experiment').mkdir()
            (source / 'experiment/private.txt').write_text('do not expose evaluator records')
            args = SimpleNamespace(runs=root / '.runs', run_id='a', arm=1, core=1,
                                   model='explicit', image='image', harness=None)
            with patch.object(launch, 'ROOT', root):
                launch.initialize(args)
                with self.assertRaises(ValueError):
                    launch.initialize(args)
                args.run_id = 'b'
                launch.initialize(args)
            a = json.loads((root / '.runs/a/manifest.json').read_text())
            b = json.loads((root / '.runs/b/manifest.json').read_text())
            self.assertNotEqual(a['volume'], b['volume'])
            self.assertFalse((root / '.runs/a/workspace/build').exists())
            self.assertFalse((root / '.runs/a/workspace/experiment').exists())

    def test_collect_raw_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'raw'
            content = b'{"type":"thread.started","thread_id":"t1"}\n{"type":"turn.completed","usage":{"input_tokens":5}}\n'
            raw.write_bytes(content)
            result = launch.collect(raw, root / 'events', 'run-a', '0001')
            self.assertEqual(result['thread_id'], 't1')
            self.assertEqual(raw.read_bytes(), content)
            self.assertIn('run-a:0001:2', (root / 'events').read_text())

    def test_resume_counters_are_differenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'raw'
            raw.write_text(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 26213, 'cached_input_tokens': 24064, 'output_tokens': 17}}) + '\n')
            result = launch.collect(raw, root / 'events', 'run', '0002', {'input_tokens': 13086, 'cached_input_tokens': 11136, 'output_tokens': 7})
            event = json.loads((root / 'events').read_text())
            self.assertEqual(event['usage'], {'input_tokens': 13127, 'cached_input_tokens': 12928, 'output_tokens': 10})
            self.assertEqual(event['usage_semantics'], 'delta')
            self.assertEqual(result['last_cumulative_usage']['output_tokens'], 17)
            with self.assertRaises(ValueError):
                launch.collect(raw, root / 'invalid', 'run', '0003', {'output_tokens': 999})

    def test_dry_run_never_calls_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'a'
            (dest / 'turns').mkdir(parents=True)
            auth = root / 'auth.json'
            auth.write_text('DO NOT READ')
            prompt = root / 'prompt'
            prompt.write_text('hello')
            launch.write_json(dest / 'manifest.json', {'run_id': 'a', 'workspace': str(dest / 'workspace'),
                'volume': 'v-a', 'image': 'image', 'requested_model': 'model'})
            args = SimpleNamespace(runs=root, run_id='a', auth=auth, prompt=prompt, resume=False, dry_run=True)
            with patch.object(launch.subprocess, 'run') as run:
                launch.turn(args)
                run.assert_not_called()
            args.resume = True
            with self.assertRaises(ValueError):
                launch.turn(args)

if __name__ == '__main__':
    unittest.main()
