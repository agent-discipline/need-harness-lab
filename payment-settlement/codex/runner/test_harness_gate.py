import json
import tempfile
import unittest
from pathlib import Path
from harness_gate import approve, writable, audit_sessions, FIELDS
from launch import docker_command


class GateTest(unittest.TestCase):
    def test_external_approval_controls_mount_and_tamper_closes_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            manifest = {'arm': 3, 'workspace': str(run / 'workspace'), 'volume': 'v',
                        'image': 'i', 'requested_model': 'gpt-6-sol'}
            (run / 'manifest.json').write_text(json.dumps(manifest))
            command = docker_command(manifest, run / 'auth', 'n')
            self.assertIn('type=bind,src=' + manifest['workspace'] + ',dst=/workspace,readonly', command)
            (run / 'spec.json').write_text(json.dumps(dict.fromkeys(FIELDS, 'defined')))
            (run / 'answers.txt').write_text('Controller answers recorded')
            approve(run, run / 'spec.json', run / 'answers.txt')
            self.assertTrue(writable(run))
            command = docker_command(manifest, run / 'auth', 'n')
            self.assertIn('type=bind,src=' + manifest['workspace'] + ',dst=/workspace', command)
            (run / 'approved-spec.json').write_text('{}')
            self.assertFalse(writable(run))

    def test_usage_deduplicates_resume_copies_and_separates_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def record(response, thread):
                return {'type': 'token_usage_record', 'payload': {'response_id': response,
                    'thread_id': thread, 'usage': {'input_tokens': 10, 'output_tokens': 2}}}
            records = [record('r1', 'pm'), record('r1', 'pm'), record('r2', 'worker')]
            records += [{'type': 'turn_context', 'payload': {'model': 'gpt-6-sol', 'effort': 'high'}}]
            (root / 'session.jsonl').write_text('\n'.join(map(json.dumps, records)))
            result = audit_sessions(root)
            self.assertEqual(result['usage']['input_tokens'], 20)
            self.assertTrue(result['worker_session_observed'])
            self.assertTrue(result['configured_models_verified'])


if __name__ == '__main__':
    unittest.main()
