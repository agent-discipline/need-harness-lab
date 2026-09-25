#!/usr/bin/env python3
"""Launch exactly one cycle's three isolated initial turns after readiness gates."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import lab
from preflight import blockers

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent.parent


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cycle', type=int, choices=range(1, 11), required=True)
    p.add_argument('--readiness', type=Path, required=True)
    args = p.parse_args()
    ready = json.loads(args.readiness.read_text())
    failed = blockers(ready)
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
    launches = []
    for arm in (1, 2, 3):
        run_id = f'sol-arm{arm}-core{args.cycle:02d}'
        records = ROOT / 'docs/experiments' / str(arm) / f'core{args.cycle}'
        project = f'payment-codex-c{args.cycle:02d}-a{arm}'
        prompt = records / 'initial-prompt.txt'
        prompt.write_text(lab.prompt(arm))
        init = [sys.executable, str(runner), 'init', '--run-id', run_id, '--arm', str(arm),
                '--core', str(args.cycle), '--model', ready['configured_model'],
                '--image', ready['image_digests']['runner'], '--network', project + '_default']
        if arm == 3:
            init += ['--harness', str(ROOT / 'harness-source')]
        subprocess.run(init, check=True, cwd=REPO)
        env = dict(os.environ, ARM=f'.runs/{run_id}', CORE='workspace',
                   APP_PORT=str(28000 + args.cycle * 10 + arm),
                   DB_PORT=str(29000 + args.cycle * 10 + arm),
                   BANK_PORT=str(30000 + args.cycle * 10 + arm))
        compose = ['docker', 'compose', '-p', project, '-f', str(ROOT / 'compose.yaml')]
        subprocess.run(compose + ['up', '-d', '--wait', 'db', 'bank'], env=env, check=True, cwd=REPO)
        run = json.loads((records / 'run.json').read_text())
        run.update({k: ready[k] for k in ready if k not in ('status', 'run_id', 'arm', 'core')})
        run.update(status='RUNNING', started_at=datetime.now(timezone.utc).isoformat(),
                   project=project, ports={k: env[k] for k in ('APP_PORT', 'DB_PORT', 'BANK_PORT')})
        save(records / 'run.json', run)
        launches.append((run_id, records, prompt))
    current.update(status='RUNNING', started_at=datetime.now(timezone.utc).isoformat())
    save(cycles_path, cycles)

    def launch(item):
        run_id, records, prompt = item
        command = [sys.executable, str(runner), 'turn', '--run-id', run_id, '--prompt', str(prompt),
                   '--auth', str(Path.home() / '.codex/auth.json'), '--cache-policy', 'observed-cache',
                   '--timeout-seconds', '3600']
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
