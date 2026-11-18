#!/usr/bin/env python3
"""Append one evaluation-cost line (experiment conductor session or judging-adapter authoring) to the experiment-wide
ledger docs/results/evaluation-cost.jsonl.

Decision 2026-11-17 (spec 10.4): evaluation cost belongs to the experiment, not to a run. It never enters a run ledger,
a run cap or an arm's cost. One line = one conductor session or one adapter-writing job; the main session records it
from the agent's completion notice usage. A session that wrote several adapters records one judging-adapter line per
job, each with its own --item label — required for every judging-adapter line (the same role, session and item is
never recorded twice). A conductor line stays one per session and takes no --item. A conductor that wrote the adapter itself records one conductor line marked
--includes-judging-adapter and no separate adapter line. The ledger is append-only; a line is never rewritten.
Tokens are either the four request counters or, when the source reports only a total, --total-tokens alone.
A line that names several runs (--run-id repeated: one conductor session over several runs) records them as
unallocated (per_run_split): its tokens are never divided among the runs.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runner'))
import launch

ROLES = ('conductor', 'judging-adapter')
SPLIT = {'input_tokens': '--input-tokens', 'cache_creation_input_tokens': '--cache-creation-input-tokens',
         'cache_read_input_tokens': '--cache-read-input-tokens', 'output_tokens': '--output-tokens'}


def default_ledger():
    return ROOT / 'docs/results/evaluation-cost.jsonl'


def non_negative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be >= 0')
    return number


def read_lines(ledger):
    """Every line of the ledger; an unreadable line stops the append (the ledger is inspected, never patched)."""
    lines = []
    for number, line in enumerate(Path(ledger).read_text().splitlines(), 1) if Path(ledger).exists() else []:
        try:
            value = json.loads(line)
        except ValueError:
            raise ValueError('ledger %s line %d is not JSON; inspect it before appending' % (ledger, number))
        if not isinstance(value, dict):
            raise ValueError('ledger %s line %d is not an object' % (ledger, number))
        lines.append(value)
    return lines


def entry(role, session, model, usage, total, source, run_ids, cost_usd=None, includes_adapter=False, note=None,
          item=None):
    """Validate one line; returns the dict to append."""
    if role not in ROLES:
        raise ValueError('role must be one of ' + ', '.join(ROLES))
    for name, value in (('session', session), ('model', model), ('source', source)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(name + ' is required')
    if role == 'judging-adapter':
        if not isinstance(item, str) or not item.strip():
            raise ValueError('a judging-adapter line needs --item (label of the adapter-writing job, not empty)')
    elif item is not None:
        raise ValueError('--item labels a judging-adapter job; a conductor line is one per session')
    if not run_ids:
        raise ValueError('name every run id this session or adapter covered (--run-id, repeat)')
    for run_id in run_ids:
        launch.valid_run(run_id)
    if len(set(run_ids)) != len(run_ids):
        raise ValueError('a run id is listed twice')
    if includes_adapter and role != 'conductor':
        raise ValueError('--includes-judging-adapter marks a conductor line only')
    if usage is not None:
        if set(usage) != set(launch.USAGE_KEYS) or any(not isinstance(v, int) or isinstance(v, bool) or v < 0
                                                       for v in usage.values()):
            raise ValueError('usage needs all four non-negative counters')
        if total is not None and total != launch.total_tokens(usage):
            raise ValueError('--total-tokens differs from the sum of the four counters')
        total = launch.total_tokens(usage)
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError('give the four counters or --total-tokens')
    if cost_usd is not None:
        cost_usd = launch.cost_value(cost_usd)
    return {'role': role, 'session': session, 'item': item, 'model': model, 'usage': usage,
            'usage_split_observed': usage is not None,
            'total_tokens': total, 'tokens_source': source, 'run_ids': list(run_ids), 'cost_usd': cost_usd,
            'includes_judging_adapter': bool(includes_adapter), 'note': note,
            # one session over several runs (e.g. a conductor that ran two pilots): the tokens are not split per run
            'per_run_split': 'unallocated' if len(run_ids) > 1 else 'single run',
            'recorded_at': datetime.now(timezone.utc).isoformat(),
            'counted_in_run_cap': False, 'counted_in_arm_cost': False}


def append(ledger, line):
    """Append under an exclusive lock; the same (role, session, item) is never recorded twice (a line written before
    items existed has item None)."""
    ledger = Path(ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    key = (line['role'], line['session'], line.get('item'))
    with ledger.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if any((l.get('role'), l.get('session'), l.get('item')) == key for l in read_lines(ledger)):
            raise ValueError('%s session %s%s is already recorded; the ledger is append-only'
                             % (line['role'], line['session'], ' item %s' % key[2] if key[2] is not None else ''))
        stream.write(json.dumps(line, ensure_ascii=False) + '\n')
        stream.flush()
    return line


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger', type=Path, default=default_ledger())
    p.add_argument('--role', required=True, choices=ROLES)
    p.add_argument('--session', required=True, help='conductor session / adapter authoring agent identifier')
    p.add_argument('--item', help='judging-adapter: required label of this adapter-writing job (e.g. the run id or '
                   'adapter name) so one session records one line per job; refused for conductor')
    p.add_argument('--model', required=True, help='model id the source reports (not the requested alias)')
    for key, flag in SPLIT.items():
        p.add_argument(flag, dest=key, type=non_negative)
    p.add_argument('--total-tokens', type=non_negative, help='when the source reports only a total (split stays unobserved)')
    p.add_argument('--source', required=True, help='where the numbers come from, e.g. the agent completion notice usage')
    p.add_argument('--run-id', dest='run_ids', action='append', default=[], help='run covered (repeat for every run)')
    p.add_argument('--cost-usd', type=launch.cost_value)
    p.add_argument('--includes-judging-adapter', action='store_true',
                   help='conductor line: the conductor wrote the judging adapter itself (no separate adapter line)')
    p.add_argument('--note')
    a = p.parse_args()
    given = {k: getattr(a, k) for k in SPLIT if getattr(a, k) is not None}
    if given and len(given) != len(SPLIT):
        p.error('give all four counters (%s) or none of them' % ', '.join(SPLIT.values()))
    try:
        line = append(a.ledger, entry(a.role, a.session, a.model, given or None, a.total_tokens, a.source, a.run_ids,
                                      a.cost_usd, a.includes_judging_adapter, a.note, a.item))
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    print(json.dumps(line, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
