#!/usr/bin/env python3
"""Write a run's terminal status into its record (docs/experiments/<arm>/core<N>/run.json) from the evaluator's verdict
and the runner's cap record (.runs/<run>/censored.json).

Decision 2026-11-17 (spec 0, 상한 초과 완료 run): a cap is the rule that stops the NEXT paid call. A run whose final state
passed the acceptance criteria although its last call went past the token or time cap is PASSED with the overshoot
recorded next to it (cap_overshoot); a run that did not pass and reached a cap is CENSORED_* as before.
censored.json is never changed or removed, so the runner keeps refusing every further paid call of a capped run.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runner'))
import launch

VERDICTS = ('PASSED', 'FAILED', 'INVALID_INFRA')
# caps a passing last call may have run past (the feedback cap stops the 31st delivery before it is sent)
OVERSHOOT_CAPS = ('CENSORED_BUDGET', 'CENSORED_TIME')


def terminal(verdict, censored):
    """(status, cap_overshoot) for the evaluator's verdict and the run's censored.json content (or None)."""
    if verdict not in VERDICTS:
        raise ValueError('verdict must be one of ' + ', '.join(VERDICTS))
    if censored is None:
        return verdict, None
    first = censored.get('first') or {}
    if verdict == 'PASSED':
        if censored.get('status') not in OVERSHOOT_CAPS or first.get('when') != 'after call':
            raise ValueError('a run censored %s (%s) cannot be PASSED: only a last call that went past the token or time '
                             'cap counts as completed with overshoot' % (censored.get('status'), first.get('when')))
        return 'PASSED', {'censored_status': censored['status'], 'overshoot': first.get('overshoot') or {},
                          'used': first.get('used'), 'limits': first.get('limits'), 'when': first['when'],
                          'at': first.get('at'), 'record': 'censored.json (runner run directory, unchanged)',
                          'rule': 'cap stops the next paid call; acceptance passed on the last call (decision 2026-11-17)'}
    if verdict == 'INVALID_INFRA':
        return verdict, None
    return censored['status'], None                     # not passed and capped: censored as before


def finish(run_id, verdict, runs=None, root=None):
    root = Path(root or ROOT)
    run = Path(runs or root / '.runs').resolve() / launch.valid_run(run_id)
    manifest = json.loads((run / 'manifest.json').read_text())
    record_path = root / 'docs/experiments' / str(manifest['arm']) / ('core%d' % manifest['core']) / 'run.json'
    record = json.loads(record_path.read_text())
    if record.get('run_id') not in (None, run_id):
        raise ValueError('record %s belongs to %s, not %s' % (record_path, record.get('run_id'), run_id))
    censored = json.loads((run / 'censored.json').read_text()) if (run / 'censored.json').exists() else None
    status, overshoot = terminal(verdict, censored)
    record.update(status=status, verdict=verdict, cap_overshoot=overshoot, cap_reached=censored is not None,
                  finished_at=datetime.now(timezone.utc).isoformat())
    tmp = record_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(record_path)
    return record_path, status, overshoot


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--verdict', required=True, choices=VERDICTS,
                   help='evaluator verdict on the final state: PASSED = every acceptance criterion passed')
    p.add_argument('--runs', type=Path)
    a = p.parse_args()
    try:
        path, status, overshoot = finish(a.run_id, a.verdict, a.runs)
    except (OSError, ValueError, KeyError) as exc:
        p.error(str(exc))
    print(json.dumps({'record': str(path), 'status': status, 'cap_overshoot': overshoot}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
