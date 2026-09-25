"""Host-owned arm 3 approval gate and response-level session usage audit."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ('goal', 'scope', 'constraints', 'acceptance', 'test_plan', 'risks')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def writable(run):
    path = Path(run) / 'harness-approval.json'
    if not path.exists():
        return False
    state = json.loads(path.read_text())
    spec = Path(run) / 'approved-spec.json'
    return (state.get('approved') is True and spec.is_file()
            and state.get('spec_sha256') == digest(spec))


def approve(run, spec, answers):
    run, spec, answers = Path(run), Path(spec), Path(answers)
    manifest = json.loads((run / 'manifest.json').read_text())
    if manifest['arm'] != 3:
        raise ValueError('approval only applies to arm 3')
    body = json.loads(spec.read_text())
    if any(not body.get(field) for field in FIELDS):
        raise ValueError('spec needs nonempty fields: ' + ', '.join(FIELDS))
    if not answers.read_text().strip():
        raise ValueError('controller interview answers required')
    for name, source in [('approved-spec.json', spec), ('interview-answers.txt', answers)]:
        with (run / name).open('xb') as target:
            target.write(source.read_bytes())
    with (run / 'harness-approval.json').open('x') as target:
        json.dump({'approved': True, 'approved_by': 'external-controller',
                   'approved_at': datetime.now(timezone.utc).isoformat(),
                   'spec_sha256': digest(spec), 'answers_sha256': digest(answers)}, target, indent=2)


def audit_sessions(directory, model='gpt-6-sol'):
    """Use each provider response once; never add CLI/thread/turn cumulative totals."""
    responses, contexts, sessions = {}, [], set()
    spawned_calls, worker_paths, contexts_by_thread = set(), set(), {}
    for path in sorted(Path(directory).rglob('*.jsonl')):
        current_thread = None
        for line in path.read_text().splitlines():
            event = json.loads(line)
            payload = event.get('payload', {})
            if event.get('type') == 'session_meta':
                current_thread = payload.get('id') or payload.get('session_id')
                sessions.add(current_thread)
                source = payload.get('source')
                if isinstance(source, dict):
                    spawn = source.get('subagent', {}).get('thread_spawn', {})
                    if spawn.get('agent_path'):
                        worker_paths.add(spawn['agent_path'])
            elif event.get('type') == 'response_item' and payload.get('name') == 'spawn_agent':
                spawned_calls.add(payload.get('call_id'))
            elif event.get('type') == 'turn_context':
                contexts.append({'model': payload.get('model'),
                                 'effort': payload.get('effort') or payload.get('reasoning_effort')})
                contexts_by_thread.setdefault(current_thread, []).append(contexts[-1])
            elif event.get('type') == 'token_usage_record':
                key = payload.get('response_id')
                if not key:
                    raise ValueError('response usage missing response_id')
                record = {'thread_id': payload['thread_id'], 'usage': payload['usage']}
                if key in responses and responses[key] != record:
                    raise ValueError('conflicting duplicate response usage')
                responses[key] = record
    total, per_thread = {}, {}
    for record in responses.values():
        subtotal = per_thread.setdefault(record['thread_id'], {})
        for key, value in record['usage'].items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError('invalid response usage')
            total[key] = total.get(key, 0) + value
            subtotal[key] = subtotal.get(key, 0) + value
    return {'semantics': 'unique_response_id', 'response_count': len(responses),
            'session_count': len(sessions), 'usage': total, 'per_thread': per_thread,
            'configured_models_verified': bool(contexts) and all(
                c == {'model': model, 'effort': 'high'} for c in contexts),
            'backend_model_verified': False,
            'spawn_call_count': len(spawned_calls),
            'worker_paths': sorted(worker_paths),
            'all_spawned_workers_collected': len(spawned_calls) == len(worker_paths),
            'all_threads_have_usage': bool(sessions) and sessions <= set(per_thread),
            'all_threads_have_model_context': bool(sessions) and sessions <= set(contexts_by_thread),
            'worker_session_observed': len(per_thread) > 1,
            'note': 'Session presence alone does not prove all spawned workers were collected.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    approve_parser = sub.add_parser('approve')
    approve_parser.add_argument('--run', type=Path, required=True)
    approve_parser.add_argument('--spec', type=Path, required=True)
    approve_parser.add_argument('--answers', type=Path, required=True)
    audit_parser = sub.add_parser('audit')
    audit_parser.add_argument('--sessions', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'approve':
        approve(args.run, args.spec, args.answers)
    else:
        print(json.dumps(audit_sessions(args.sessions), indent=2))
