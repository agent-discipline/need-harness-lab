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
import freeze_readiness
import hashlib
from preflight import readiness_blockers
import commit_run


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run_plan(cycle, arm):
    run_id = f'claude-arm{arm}-core{cycle:02d}'
    project = f'payment-claude-c{cycle:02d}-a{arm}'
    ports = {'APP_PORT': str(38000 + cycle * 10 + arm), 'DB_PORT': str(39000 + cycle * 10 + arm),
             'BANK_PORT': str(40000 + cycle * 10 + arm)}
    return run_id, project, ports


def gate(ready, cycle):
    """Per-arm preflight (cycle stage) on each arm's record merged with readiness (preflight.merge)."""
    return readiness_blockers(ready, cycle, 'cycle', root=ROOT)


def contract_drift(ready, frozen_list=None):
    """Recompute the frozen-input hash from the current files; on mismatch name the files that differ (when the
    freeze's frozen-inputs.json is available). Empty list = unchanged since the freeze."""
    current = freeze_readiness.inventory()
    if hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest() == ready.get('contract_sha256'):
        return []
    try:
        frozen = json.loads(Path(frozen_list).read_text()) if frozen_list else None
    except (OSError, ValueError):
        frozen = None
    if frozen is None:
        return ['contract_sha256 differs from the current frozen inputs (frozen-inputs.json unavailable)']
    changed = sorted(k for k in frozen.keys() | current.keys() if frozen.get(k) != current.get(k))
    return changed or ['contract_sha256 differs although frozen-inputs.json matches the current files']


def git(*args):
    return subprocess.run(['git', '-C', str(REPO), *args], capture_output=True, text=True, check=True).stdout


def cycle_order_blockers(cycle):
    """Evidence, not cycles.json: every run of each earlier cycle is terminal, committed by commit_run (subject
    'experiment: <run_id> <status>'), has no uncommitted changes in its code or records, and had its volumes removed;
    no run of this cycle exists yet."""
    failures = []
    subjects = git('log', '--format=%s').splitlines() if cycle > 1 else []
    for c in range(1, cycle):
        for arm in (1, 2, 3):
            run_id = run_plan(c, arm)[0]
            records = ROOT / 'docs/experiments' / str(arm) / f'core{c}'
            status = json.loads((records / 'run.json').read_text()).get('status')
            if status not in commit_run.TERMINAL:
                failures.append(f'{run_id}: status {status} is not terminal')
            if not any(line.startswith(f'experiment: {run_id} ') for line in subjects):
                failures.append(f'{run_id}: no run commit')
            dirty = git('status', '--porcelain', '--', str(records), str(ROOT / str(arm) / f'core{c}'))
            if dirty.strip():
                failures.append(f'{run_id}: uncommitted changes in its code or records')
            if not freeze_readiness.launch.discard_complete(ROOT / '.runs' / run_id):
                failures.append(f'{run_id}: volumes not removed (launch.py discard incomplete or missing)')
    for arm in (1, 2, 3):
        run_id = run_plan(cycle, arm)[0]
        status = json.loads((ROOT / 'docs/experiments' / str(arm) / f'core{cycle}' / 'run.json').read_text()).get('status')
        if status != 'NOT_STARTED' or (ROOT / '.runs' / run_id).exists():
            failures.append(f'{run_id}: already started (status {status}); recover its exact session instead')
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
    drift = contract_drift(ready, args.readiness.with_name('frozen-inputs.json'))
    if drift:
        p.error('frozen inputs changed since the freeze (new protocol version and rerun of all arms required): '
                + ', '.join(drift[:20]))
    cycles_path = ROOT / 'docs/results/cycles.json'
    cycles = json.loads(cycles_path.read_text())
    current = cycles[args.cycle - 1]
    order = cycle_order_blockers(args.cycle)
    if order:
        p.error('; '.join(order))
    lab.verify(args.cycle)
    runner = ROOT / 'runner/launch.py'
    plan = [run_plan(args.cycle, arm) for arm in (1, 2, 3)]
    if args.dry_run:
        print(json.dumps({'cycle': args.cycle, 'runs': [dict(run_id=r, project=pr, ports=po) for r, pr, po in plan],
                          'image': ready['image_digests']['runner']}, indent=2))
        return 0
    # Set up all three arms first; a cycle's turns start together or not at all.
    launches, prepared, failures = [], [], {}
    for arm, (run_id, project, ports) in zip((1, 2, 3), plan):
        records = ROOT / 'docs/experiments' / str(arm) / f'core{args.cycle}'
        prompt = records / 'initial-prompt.txt'
        # models come from launch.MODELS (frozen in readiness.requested_models); the image is the frozen id
        init = [sys.executable, str(runner), 'init', '--run-id', run_id, '--arm', str(arm), '--core', str(args.cycle),
                '--image', ready['image_digests']['runner'], '--readiness', str(args.readiness),
                '--network', project + '_default']
        if arm == 3:
            init += ['--harness', str(ROOT / 'harness-source')]
        env = dict(os.environ, ARM=f'.runs/{run_id}', CORE='workspace', **ports)
        compose = ['docker', 'compose', '-p', project, '-f', str(ROOT / 'compose.yaml')]
        try:
            prompt.write_text(lab.prompt(arm))
            for step, command, extra in (('init', init, {}), ('compose up', compose + ['up', '-d', '--wait', 'db', 'bank'], {'env': env})):
                done = subprocess.run(command, cwd=REPO, **extra)
                if done.returncode:
                    raise RuntimeError('%s exited %s' % (step, done.returncode))
        except (OSError, RuntimeError) as exc:
            failures[run_id] = str(exc)[:500]
            run = json.loads((records / 'run.json').read_text())
            run.update(status='SETUP_FAILED', setup_error=failures[run_id], project=project, ports=ports,
                       setup_failed_at=datetime.now(timezone.utc).isoformat())
            save(records / 'run.json', run)
            continue
        prepared.append((arm, run_id, project, ports, records, prompt))
    now = datetime.now(timezone.utc).isoformat()
    if failures:
        current.update(status='SETUP_FAILED', setup_failures=failures, setup_failed_at=now,
                       prepared_runs=[r for _, r, *_ in prepared])
        save(cycles_path, cycles)
        print(json.dumps({'cycle': args.cycle, 'setup_failed': failures, 'turns_started': 0,
                          'recovery': 'payment-settlement/claude/docs/runbook.md: 사이클 준비 실패 복구'}, ensure_ascii=False))
        return 1
    for arm, run_id, project, ports, records, prompt in prepared:
        run = json.loads((records / 'run.json').read_text())
        run.update({k: ready[k] for k in ready if k not in ('status', 'run_id', 'arm', 'core', 'evidence', 'actual_models')})
        run.update(status='RUNNING', started_at=now, project=project, ports=ports,
                   actual_model=(ready.get('actual_models') or {}).get(str(arm)))
        save(records / 'run.json', run)
        launches.append((run_id, records, prompt))
    current.update(status='RUNNING', started_at=now)
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
    # Each run keeps its own exit; a failed first turn stays RUNNING and is recovered per the runbook (same session).
    for (run_id, records, _), code in zip(launches, codes):
        run = json.loads((records / 'run.json').read_text())
        run.update(initial_turn_exit=code, initial_turn_failed=bool(code))
        save(records / 'run.json', run)
    # Implementation completion is NOT experiment completion; validation follows.
    current.update(initial_turns_finished=True, initial_turn_exits={r: c for (r, _, _), c in zip(launches, codes)})
    save(cycles_path, cycles)
    return int(any(codes))


if __name__ == '__main__':
    raise SystemExit(main())
