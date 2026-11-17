#!/usr/bin/env python3
"""Record one evaluation-cost entry (experiment conductor or judging adapter) for a run.

Spec 2: the conductor's, judging adapter's and user proxy's tokens are recorded per role; spec 0: the judging adapter's
cost is evaluation cost, recorded separately. The entry goes to <runs>/<run-id>/evaluation/NNNN/record.json and appears
in the run ledger's evaluation_cost block. It never enters the run's token cap or the arm's cost totals.
Tokens are either the four request counters or, when the source reports only a total, --total-tokens alone.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runner'))
import launch

SPLIT = {'input_tokens': '--input-tokens', 'cache_creation_input_tokens': '--cache-creation-input-tokens',
         'cache_read_input_tokens': '--cache-read-input-tokens', 'output_tokens': '--output-tokens'}


def non_negative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be >= 0')
    return number


def record(runs, run_id, role, model, usage, total, source, cost_usd=None, note=None):
    """Write the next evaluation/NNNN/record.json of the run and refresh its ledger; returns the record path."""
    if role not in launch.EVALUATION_ROLES:
        raise ValueError('role must be one of ' + ', '.join(launch.EVALUATION_ROLES))
    if not (model or '').strip() or not (source or '').strip():
        raise ValueError('model and the source of the numbers are required')
    if usage is not None:
        if set(usage) != set(launch.USAGE_KEYS) or any(not isinstance(v, int) or v < 0 for v in usage.values()):
            raise ValueError('usage needs all four non-negative counters')
        if total is not None and total != launch.total_tokens(usage):
            raise ValueError('--total-tokens differs from the sum of the four counters')
        total = launch.total_tokens(usage)
    if not isinstance(total, int) or total < 0:
        raise ValueError('give the four counters or --total-tokens')
    run = Path(runs).resolve() / launch.valid_run(run_id)
    if not (run / 'manifest.json').is_file():
        raise ValueError('run does not exist: ' + str(run))
    folder = run / 'evaluation'
    number = len([p for p in folder.iterdir() if p.is_dir()]) + 1 if folder.is_dir() else 1
    out = folder / ('%04d' % number)
    out.mkdir(parents=True, exist_ok=False)
    value = {'run_id': run_id, 'role': role, 'model': model, 'usage': usage, 'usage_split_observed': usage is not None,
             'total_tokens': total, 'tokens_source': source, 'cost_usd': cost_usd, 'note': note,
             'recorded_at': datetime.now(timezone.utc).isoformat(),
             'counted_in_run_cap': False, 'counted_in_arm_cost': False}
    with (out / 'record.json').open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    launch.write_ledger(run)
    return out / 'record.json'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--runs', type=Path, default=ROOT / '.runs')
    p.add_argument('--role', required=True, choices=launch.EVALUATION_ROLES)
    p.add_argument('--model', required=True, help='model id the source reports (not the requested alias)')
    for key, flag in SPLIT.items():
        p.add_argument(flag, dest=key, type=non_negative)
    p.add_argument('--total-tokens', type=non_negative, help='when the source reports only a total (split stays unobserved)')
    p.add_argument('--source', required=True, help='where the numbers come from, e.g. the agent completion notice usage')
    p.add_argument('--cost-usd', type=float)
    p.add_argument('--note')
    a = p.parse_args()
    given = {k: getattr(a, k) for k in SPLIT if getattr(a, k) is not None}
    if given and len(given) != len(SPLIT):
        p.error('give all four counters (%s) or none of them' % ', '.join(SPLIT.values()))
    try:
        path = record(a.runs, a.run_id, a.role, a.model, given or None, a.total_tokens, a.source, a.cost_usd, a.note)
    except (OSError, ValueError, RuntimeError) as exc:
        p.error(str(exc))
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
