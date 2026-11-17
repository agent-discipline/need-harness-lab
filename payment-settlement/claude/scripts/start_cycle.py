#!/usr/bin/env python3
"""Launch exactly one cycle's three isolated initial Claude turns after readiness gates (paid unless --dry-run)."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]           # payment-settlement/claude
REPO = ROOT.parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
from preflight import blockers


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run_plan(cycle, arm):
    run_id = f'claude-arm{arm}-core{cycle:02d}'
    project = f'payment-claude-c{cycle:02d}-a{arm}'
    ports = {'APP_PORT': str(38000 + cycle * 10 + arm), 'DB_PORT': str(39000 + cycle * 10 + arm),
             'BANK_PORT': str(40000 + cycle * 10 + arm)}
    return run_id, project, ports


def gate(ready, cycle):
    """Per-arm preflight on the ledger merged with readiness (the arm's observed model becomes actual_model)."""
    failures = []
    for arm in (1, 2, 3):
        records = ROOT / 'docs/experiments' / str(arm) / f'core{cycle}'
        run = json.loads((records / 'run.json').read_text())
        merged = dict(run, **{k: v for k, v in ready.items() if k not in ('status', 'run_id', 'arm', 'core', 'evidence')})
        merged['actual_model'] = (ready.get('actual_models') or {}).get(str(arm))
        failures += ['arm %d: %s' % (arm, f) for f in blockers(merged)]
    return failures


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cycle', type=int, choices=range(1, 11), required=True)
    p.add_argument('--readiness', type=Path, default=ROOT / 'docs/results/readiness.json')
    p.add_argument('--env-file', type=Path, required=True, help='0600 file outside the repo with only CLAUDE_CODE_OAUTH_TOKEN=...')
    p.add_argument('--timeout-seconds', type=int, default=3600)
    p.add_argument('--dry-run', action='store_true', help='check gates and print the plan; no init, compose or model call')
    args = p.parse_args()
    ready = json.loads(args.readiness.read_text())
    failed = gate(ready, args.cycle)
    if failed:
        p.error('; '.join(failed))
    cycles_path = ROOT / 'docs/results/cycles.json'
    cycles = json.loads(cycles_path.read_text())
    current = cycles[args.cycle - 1]
    if current['status'] != 'NOT_STARTED':
        p.error('cycle already started; recover its exact sessions instead')
    if any(c['status'] != 'COMMITTED' for c in cycles[:args.cycle - 1]):
        p.error('previous cycle not committed')
    lab.verify(args.cycle)
    runner = ROOT / 'runner/launch.py'
    plan = [run_plan(args.cycle, arm) for arm in (1, 2, 3)]
    if args.dry_run:
        print(json.dumps({'cycle': args.cycle, 'runs': [dict(run_id=r, project=pr, ports=po) for r, pr, po in plan],
                          'image': ready['image_digests']['runner']}, indent=2))
        return 0
    launches = []
    for arm, (run_id, project, ports) in zip((1, 2, 3), plan):
        records = ROOT / 'docs/experiments' / str(arm) / f'core{args.cycle}'
        prompt = records / 'initial-prompt.txt'
        prompt.write_text(lab.prompt(arm))
        # models come from launch.MODELS (frozen in readiness.requested_models); the image is the frozen id
        init = [sys.executable, str(runner), 'init', '--run-id', run_id, '--arm', str(arm), '--core', str(args.cycle),
                '--image', ready['image_digests']['runner'], '--network', project + '_default']
        if arm == 3:
            init += ['--harness', str(ROOT / 'harness-source')]
        subprocess.run(init, check=True, cwd=REPO)
        env = dict(os.environ, ARM=f'.runs/{run_id}', CORE='workspace', **ports)
        compose = ['docker', 'compose', '-p', project, '-f', str(ROOT / 'compose.yaml')]
        subprocess.run(compose + ['up', '-d', '--wait', 'db', 'bank'], env=env, check=True, cwd=REPO)
        run = json.loads((records / 'run.json').read_text())
        run.update({k: ready[k] for k in ready if k not in ('status', 'run_id', 'arm', 'core', 'evidence', 'actual_models')})
        run.update(status='RUNNING', started_at=datetime.now(timezone.utc).isoformat(), project=project, ports=ports,
                   actual_model=(ready.get('actual_models') or {}).get(str(arm)))
        save(records / 'run.json', run)
        launches.append((run_id, records, prompt))
    current.update(status='RUNNING', started_at=datetime.now(timezone.utc).isoformat())
    save(cycles_path, cycles)

    def launch(item):
        run_id, records, prompt = item
        command = [sys.executable, str(runner), 'turn', '--run-id', run_id, '--prompt', str(prompt),
                   '--env-file', str(args.env_file), '--cache-policy', 'observed-cache',
                   '--timeout-seconds', str(args.timeout_seconds)]
        with (records / 'initial-launch.log').open('x') as stream:
            code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, cwd=REPO).returncode
        print(json.dumps({'run_id': run_id, 'initial_turn_exit': code}), flush=True)
        return code
    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(launch, launches))
    # Implementation completion is NOT experiment completion; validation follows.
    current['initial_turns_finished'] = True
    save(cycles_path, cycles)
    return int(any(codes))


if __name__ == '__main__':
    raise SystemExit(main())
