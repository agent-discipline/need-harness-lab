#!/usr/bin/env python3
"""Prepare an isolated run; explicit turn executes a paid Codex CLI call."""
import argparse
from datetime import datetime, timezone
from types import SimpleNamespace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.gradle', 'build', '.runs', '.codex', '.claude', 'node_modules', 'logs', 'artifacts', 'experiment', '__pycache__'}

def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')

def valid_run(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('run-id must be 1..64 safe ASCII characters')
    return value

def location(args):
    return args.runs.resolve() / valid_run(args.run_id)

def initialize(args):
    dest = location(args)
    if args.harness and (args.arm != 3 or args.harness.resolve() != ROOT / 'harness-source'
                         or not args.harness.is_dir()):
        raise ValueError('--harness requires arm 3 and an existing source directory')
    if args.arm == 3 and not args.harness:
        raise ValueError('arm 3 requires --harness with the frozen source')
    network = getattr(args, 'network', None)
    if network and (network in ('host', 'container') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', network)):
        raise ValueError('use this run own Compose network; host network forbidden')
    source = ROOT / str(args.arm) / ('core' + str(args.core))
    if not source.is_dir():
        raise ValueError('starter does not exist: ' + str(source))
    if dest.exists():
        raise ValueError('run already exists; overwrite refused')
    dest.mkdir(parents=True)
    workspace = dest / 'workspace'
    # Follow no links: prevent starter symlinks from exporting arbitrary host files.
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('starter symlinks are not allowed')
    shutil.copytree(source, workspace, ignore=shutil.ignore_patterns(*SKIP))
    (dest / 'turns').mkdir()
    write_json(dest / 'manifest.json', {
        'run_id': args.run_id, 'arm': args.arm, 'core': args.core,
        'requested_model': args.model, 'actual_model': None, 'effort': 'high',
        'cache_policy': 'observed-cache', 'strict_zero_cache_supported': False,
        'image': args.image, 'volume': 'payment-agent-' + uuid.uuid4().hex,
        'source': str(source), 'workspace': str(workspace), 'network': network,
        'harness': str(args.harness.resolve()) if args.harness else None,
    })
    print(dest)

def codex_command(model, thread=None):
    command = ['codex', 'exec']
    if thread:
        command += ['resume', thread]
    command += ['--dangerously-bypass-approvals-and-sandbox', '--ignore-user-config',
                '--ignore-rules', '--skip-git-repo-check', '--json', '--model', model,
                '--config', 'model_reasoning_effort="high"', '--disable', 'goals',
                '--disable', 'memories', '-']
    return command

def docker_command(manifest, auth, name, thread=None):
    # Only workspace and this run's state are writable; auth is a single read-only file.
    command = ['docker', 'create', '--name', name, '--interactive', '--init',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=512',
            '--cpus=2', '--memory=3g', '--workdir=/workspace',
            '--mount', 'type=bind,src=' + manifest['workspace'] + ',dst=/workspace',
            '--mount', 'type=volume,src=' + manifest['volume'] + ',dst=/home/node/.codex',
            '--mount', 'type=bind,src=' + str(auth) + ',dst=/home/node/.codex/auth.json,readonly',
            ]
    if manifest.get('network'):
        command += ['--network', manifest['network'], '--env', 'DB_URL=jdbc:postgresql://db:5432/payment', '--env', 'BANK_BASE_URL=http://bank:8081']
    if manifest.get('harness'):
        command += ['--mount', 'type=bind,src=' + manifest['harness'] + ',dst=/harness,readonly']
    cli = codex_command(manifest['requested_model'], thread)
    cli[-1:-1] = ['--enable' if manifest.get('arm') == 3 else '--disable', 'multi_agent']
    return command + [manifest['image']] + cli

def collect(raw, decorated, run_id, turn_id, previous_usage=None):
    counters = previous_usage or {}
    thread = None
    completed = 0
    actual_models = set()
    with raw.open('rb') as source, decorated.open('x') as target:
        for index, line in enumerate(source, 1):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'thread.started':
                thread = event.get('thread_id')
            if event.get('type') == 'turn.completed':
                completed += 1
                event['event_id'] = f'{run_id}:{turn_id}:{index}'
                current = event.get('usage')
                if not isinstance(current, dict):
                    raise ValueError('completion has no cumulative usage')
                delta = {}
                for key, value in current.items():
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise ValueError('invalid cumulative counter')
                    before = counters.get(key, 0)
                    if value < before:
                        raise ValueError('cumulative usage decreased; inspect session identity')
                    delta[key] = value - before
                event['reported_cumulative_usage'] = current
                event['usage'] = delta
                event['usage_semantics'] = 'delta'
                counters = current
            if isinstance(event.get('model'), str):
                actual_models.add(event['model'])
            target.write(json.dumps(event, ensure_ascii=False) + '\n')
    return {'thread_id': thread, 'turn_completed_count': completed,
            'reported_models': sorted(actual_models), 'actual_model': None,
            'actual_model_note': 'Confirm against provider/session metadata; requested model is not proof.',
            'last_cumulative_usage': counters}

def turn(args):
    dest = location(args)
    manifest = json.loads((dest / 'manifest.json').read_text())
    if manifest['run_id'] != args.run_id or Path(manifest['workspace']).resolve() != dest / 'workspace':
        raise ValueError('manifest identity/workspace mismatch')
    auth = args.auth.resolve()
    if not auth.is_file() or ',' in str(auth) or ',' in str(dest):
        raise ValueError('auth must exist; Docker mount paths must not contain commas')
    history = sorted((dest / 'turns').glob('*/result.json'))
    thread = None
    previous_usage = None
    if history:
        previous = json.loads(history[-1].read_text())
        thread = previous.get('thread_id')
        previous_usage = previous.get('last_cumulative_usage')
        if previous_usage is None:
            # Compatibility with preparation captures made before cumulative semantics were verified.
            raw_history = history[-1].parent / 'stdout.jsonl'
            for line in raw_history.read_text().splitlines():
                event = json.loads(line)
                if event.get('type') == 'turn.completed':
                    previous_usage = event.get('usage')
        if not thread:
            raise ValueError('previous call has no recoverable thread ID; inspect raw output')
    if args.resume != bool(history):
        raise ValueError('first turn requires no --resume; subsequent turns require --resume')
    turn_id = f'{len(history) + 1:04d}'
    name = 'payment-call-' + uuid.uuid4().hex
    command = docker_command(manifest, auth, name, thread)
    prompt = args.prompt.read_bytes()
    if args.dry_run:
        print(json.dumps({'create': command, 'start': ['docker', 'start', '--attach', '--interactive', name],
                          'prompt_bytes': len(prompt), 'thread_id': thread}, indent=2))
        return
    if args.cache_policy != 'observed-cache':
        raise ValueError('explicit --cache-policy observed-cache required; strict cache control unavailable')
    output = dest / 'turns' / turn_id
    output.mkdir()
    (output / 'prompt.txt').write_bytes(prompt)
    write_json(output / 'invocation.json', {'create': command, 'thread_id': thread,
        'prompt_sha256': hashlib.sha256(prompt).hexdigest(), 'cache_policy': args.cache_policy,
        'started_at': datetime.now(timezone.utc).isoformat(), 'timeout_seconds': args.timeout_seconds})
    try:
        created = subprocess.run(command, capture_output=True, check=False)
        (output / 'create.stdout').write_bytes(created.stdout)
        (output / 'create.stderr').write_bytes(created.stderr)
        if created.returncode:
            raise RuntimeError('docker create failed; see preserved stderr')
        with (output / 'stdout.jsonl').open('xb') as stdout, (output / 'stderr.txt').open('xb') as stderr:
            try:
                proc = subprocess.run(['docker', 'start', '--attach', '--interactive', name], input=prompt,
                                      stdout=stdout, stderr=stderr, check=False, timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                subprocess.run(['docker', 'stop', '--time', '2', name], capture_output=True, check=False)
                proc = SimpleNamespace(returncode=124)
        inspected = subprocess.run(['docker', 'inspect', name], capture_output=True, check=False)
        (output / 'container-inspect.json').write_bytes(inspected.stdout)
        session_copy = subprocess.run(['docker', 'cp', name + ':/home/node/.codex/sessions', str(output / 'sessions')], capture_output=True, check=False)
        (output / 'session-copy.stderr').write_bytes(session_copy.stderr)
        result = collect(output / 'stdout.jsonl', output / 'events.jsonl', args.run_id, turn_id, previous_usage)
        if result['thread_id'] is None:
            result['thread_id'] = thread
        contexts = []
        for session_file in (output / 'sessions').rglob('*.jsonl'):
            for line in session_file.read_text().splitlines():
                try:
                    event = json.loads(line)
                    if event.get('type') == 'turn_context':
                        payload = event.get('payload', {})
                        contexts.append({'model': payload.get('model'), 'effort': payload.get('effort') or payload.get('reasoning_effort')})
                except ValueError:
                    pass
        result['model_contexts'] = contexts
        models = {c['model'] for c in contexts if c['model']}
        if len(models) == 1:
            result['configured_model'] = next(iter(models))
            result['actual_model_note'] = 'turn_context confirms client configuration, not the backend model identity.'
        result['exit_code'] = proc.returncode
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(output / 'result.json', result)
        print(json.dumps(result))
        if proc.returncode:
            raise RuntimeError('Codex call failed; raw logs preserved')
    finally:
        # Remove only this generated call container, never its run state volume.
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, default=ROOT / '.runs')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--run-id', required=True)
    init.add_argument('--arm', type=int, choices=(1, 2, 3), required=True)
    init.add_argument('--core', type=int, choices=range(1, 11), required=True)
    init.add_argument('--model', required=True)
    init.add_argument('--harness', type=Path, help='Arm 3 only: public harness snapshot, read-only /harness')
    init.add_argument('--network', help='Own run Compose network; never host networking')
    init.add_argument('--image', default='payment-codex-runner:0.155.1')
    run = sub.add_parser('turn')
    run.add_argument('--run-id', required=True)
    run.add_argument('--prompt', type=Path, required=True)
    run.add_argument('--auth', type=Path, required=True)
    run.add_argument('--timeout-seconds', type=int, default=1800)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--dry-run', action='store_true')
    run.add_argument('--cache-policy', choices=('observed-cache',))
    args = parser.parse_args()
    try:
        initialize(args) if args.command == 'init' else turn(args)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, str(exc) + '\n')

if __name__ == '__main__':
    main()
