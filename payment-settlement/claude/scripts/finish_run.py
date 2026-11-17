#!/usr/bin/env python3
"""Write a run's terminal status and its four evidence fields into its record run.json, derived only from fixed sources
(spec 10.2): the verdict table verdict.json in the record folder (written by the main supervising session), the run
directory .runs/<run> (ledger computed on the spot, manifest, request events, self-tests), the freeze (readiness.json
named in the manifest) and the cap record censored.json.

Order (spec 10.1): judging -> verdict.json -> launch.py normalize -> capture_turn.py -> this script -> discard ->
commit_run.py. Every finalized turn must already be captured in the record folder, its capture completed (the marker
capture_turn.py writes last; refused otherwise), so the hashes recorded here are those of the files as committed.

PASSED/FAILED are never arguments: PASSED only when all of C01..C20 are PASS in verdict.json. INVALID_INFRA (an
execution-environment failure unrelated to the implementation) is the only status given by argument, with a reason and
evidence files. A run is finished once; a correction needs --supersede --reason and keeps the previous run.json beside
the new one. Pass/fail comes from the verdict table alone; a cap record (censored.json) is copied as annotation
(cap_reached, status, before/after call, call kind, overshoot) and decides the status only for a run that did not pass
(CENSORED_*). censored.json is never changed, so a capped run stays closed to new calls.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runner'))
sys.path.insert(0, str(ROOT / 'scripts'))
import launch
import capture_turn
import commit_run
import freeze_readiness

CRITERIA = tuple('C%02d' % n for n in range(1, 21))
RESULTS = ('PASS', 'FAIL', 'PARTIAL', 'NOT_RUN')       # PARTIAL and NOT_RUN are not a pass (evaluation/README.md)
VERDICT = 'verdict.json'
CENSORED_STATUSES = tuple(status for _, status in launch.CENSORED)   # what a cap record (censored.json) may name
NOT_FINISHABLE = ('NOT_STARTED', 'SETUP_FAILED')        # setup failures follow the runbook's recovery, not this script
# roles each arm must show with its agreed model at the finish (arm 3 includes the user proxy; the pilot check differs)
RUN_ROLES = freeze_readiness.FINISH_ROLES


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(folder, rel):
    """A path written relative to the record folder that stays inside it; anything else is a malformed reference."""
    if not isinstance(rel, str) or not rel.strip() or Path(rel).is_absolute():
        raise ValueError('evidence path must be relative to the record folder: %r' % (rel,))
    path = (folder / rel).resolve()
    if folder.resolve() not in path.parents:
        raise ValueError('evidence path leaves the record folder: %s' % rel)
    return path


def read_verdict(folder, run_id):
    """Validate the verdict table's shape and support and hash its evidence files. Refused: a malformed table, and a
    table in which a PASS/FAIL/PARTIAL criterion has no existing evidence file or a NOT_RUN criterion has no note
    (whatever status would result). A listed evidence file that does not exist beside an existing one is recorded as
    missing (hash None) and makes all_acceptance_passed false."""
    path = folder / VERDICT
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('run_id') != run_id:
        raise ValueError('%s is not the verdict of %s (run_id %r)' % (path, run_id, data.get('run_id') if isinstance(data, dict) else None))
    if '_example' in data:
        raise ValueError('%s is the example file (docs/results/verdict.example.json), not a verdict' % path)
    if not isinstance(data.get('judged_by'), str) or not data['judged_by'].strip():
        raise ValueError('%s needs judged_by (who read the judging artifacts and wrote the table)' % path)
    criteria = data.get('criteria')
    if not isinstance(criteria, dict) or set(criteria) != set(CRITERIA):
        raise ValueError('%s must judge exactly C01..C20 under "criteria"' % path)
    results, evidence, unsupported = {}, {}, []
    for cid in CRITERIA:
        item = criteria[cid]
        if not isinstance(item, dict) or item.get('result') not in RESULTS:
            raise ValueError('%s %s: result must be one of %s' % (path, cid, ', '.join(RESULTS)))
        files = item.get('evidence')
        if not isinstance(files, list):
            raise ValueError('%s %s: evidence must be a list of paths relative to the record folder' % (path, cid))
        results[cid] = item['result']
        existing = 0
        for rel in files:
            file = inside(folder, rel)
            evidence.setdefault(rel, sha(file) if file.is_file() else None)
            existing += evidence[rel] is not None
        note = item.get('note')
        if item['result'] == 'NOT_RUN':
            if not isinstance(note, str) or not note.strip():
                unsupported.append('%s (NOT_RUN without a note)' % cid)
        elif not existing:
            unsupported.append('%s (%s without an existing evidence file)' % (cid, item['result']))
    if unsupported:
        raise ValueError('%s: every criterion needs support (PASS/FAIL/PARTIAL: at least one existing evidence file; '
                         'NOT_RUN: a note) — unsupported: %s' % (path, ', '.join(unsupported)))
    passed = all(results[c] == 'PASS' for c in CRITERIA)
    missing = sorted(rel for rel, digest in evidence.items() if digest is None)
    counts = {r: sum(1 for v in results.values() if v == r) for r in RESULTS}
    return {'file': VERDICT, 'sha256': sha(path), 'judged_by': data['judged_by'], 'judged_at': data.get('judged_at'),
            'results': results, 'counts': counts, 'passed': passed, 'evidence': evidence, 'evidence_missing': missing,
            'evidence_complete': not missing}


def cap_annotation(censored):
    """What run.json carries from the run's cap record (censored.json, never changed): which cap, when it was reached
    (before a call / after a call), in which kind of call (turn / user-proxy; None when the record does not say), and
    the overshoot amounts of a call that went past the cap (None for a call refused before it was made)."""
    if censored is None:
        return None
    first = censored.get('first') or {}
    when = first.get('when')
    return {'status': censored.get('status'), 'when': when, 'call': first.get('call'), 'at': first.get('at'),
            'overshoot': (first.get('overshoot') or {}) if when == 'after call' else None,
            'used': first.get('used'), 'limits': first.get('limits'),
            'record': 'censored.json (runner run directory, unchanged)'}


def terminal(passed, censored):
    """Terminal status from the verdict table alone: all 20 PASS -> PASSED whatever the cap record says; otherwise
    the cap record's CENSORED_* when the run was capped, else FAILED. The cap record is annotation (cap_annotation)."""
    if passed:
        return 'PASSED'
    if censored is not None:
        if censored.get('status') not in CENSORED_STATUSES:
            raise ValueError('censored.json carries no known cap status: %r' % (censored.get('status'),))
        return censored['status']
    return 'FAILED'


def capture_state(run, folder):
    """The finish comes after normalize and capture_turn (spec 10.1), so the record folder must already hold every turn
    the run directory finalized (status ok/failed — what capture_turn accepts): a completed capture (captured_complete)
    of a byte-identical result.json. Returns (turns not captured, captured only part-way, changed since, or captured
    from another result; turns capture_turn cannot take: cut short / not finalized)."""
    missing, not_capturable = [], []
    for turn in sorted(p for p in (run / 'turns').iterdir() if p.is_dir()) if (run / 'turns').is_dir() else []:
        try:
            status = json.loads((turn / 'result.json').read_text()).get('status')
        except (OSError, ValueError, AttributeError):
            status = None
        if status not in ('ok', 'failed'):
            not_capturable.append(turn.name)
            continue
        if not captured_complete(folder / 'turns' / turn.name, sha(turn / 'result.json')):
            missing.append(turn.name)
    return missing, not_capturable


def captured_complete(copy, result_sha):
    """A turn folder whose capture finished: the marker capture_turn.py writes last is present, every file it lists is
    there unchanged, and the result.json among them is the run directory's."""
    try:
        files = json.loads((copy / capture_turn.CAPTURED).read_text())['files']
        return (isinstance(files, dict) and files.get('result.json') == result_sha
                and all(isinstance(name, str) and '/' not in name and (copy / name).is_file() and sha(copy / name) == digest
                        for name, digest in files.items()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def readiness_of(manifest):
    path = manifest.get('readiness')
    try:
        return json.loads(Path(path).read_text()) if path else None
    except (OSError, ValueError):
        return None


def evidence_fields(run, manifest, verdict):
    """The four fields commit_run.py requires for a PASSED run, each from its fixed source only (false when the source
    is absent or false), plus how each was derived."""
    ledger = launch.run_ledger(run)                     # computed now from the run directory, not a copy
    roles = freeze_readiness.run_role_check(run, manifest, RUN_ROLES.get(manifest.get('arm'), ()))
    isolated, isolation = freeze_readiness.run_isolation_check(run, manifest, readiness_of(manifest))
    fields = {
        'all_acceptance_passed': bool(verdict) and verdict['passed'] and verdict['evidence_complete'],
        'usage_complete': ledger['usage_complete'] is True,
        'all_role_models_verified': bool(roles) and all(roles.values()),
        'isolation_verified': isolated,
    }
    derivation = {
        'all_acceptance_passed': {'source': VERDICT, 'all_pass': bool(verdict) and verdict['passed'],
                                  'evidence_missing': verdict['evidence_missing'] if verdict else None},
        'usage_complete': {'source': '.runs/<run> ledger computed at finish', 'incomplete_calls': ledger['incomplete_calls'],
                           'conflicts': len(ledger['conflicts'])},
        'all_role_models_verified': {'source': 'manifest actual_role_models + request events (freeze_readiness.run_role_check)',
                                     'checks': roles},
        'isolation_verified': {'source': 'manifest self-tests + readiness evidence.smoke_runs tool surface '
                                         '(freeze_readiness.run_isolation_check)', 'readiness': manifest.get('readiness'),
                               'checks': isolation},
    }
    return fields, derivation


def finish(run_id, runs=None, root=None, infra_reason=None, infra_evidence=(), supersede_reason=None):
    root = Path(root or ROOT)
    run = Path(runs or root / '.runs').resolve() / launch.valid_run(run_id)
    manifest = json.loads((run / 'manifest.json').read_text())
    folder = launch.open_record(manifest, run_id, root)
    record_path = folder / 'run.json'
    record = json.loads(record_path.read_text())
    if record.get('status') in NOT_FINISHABLE:
        raise ValueError('record status %s: a run that never started is recovered per the runbook, not finished'
                         % record['status'])
    if not launch.turn_records(run):
        raise ValueError('run %s sent no turn; nothing to finish' % run_id)
    finished = record.get('status') in commit_run.TERMINAL or bool(record.get('finished_at'))
    if finished and supersede_reason is None:
        raise ValueError('run %s is already finished (%s); a correction needs --supersede --reason' % (run_id, record.get('status')))
    if supersede_reason is not None and (not finished or not supersede_reason.strip()):
        raise ValueError('--supersede needs a finished record and a non-empty --reason')
    uncaptured, not_capturable = capture_state(run, folder)
    if uncaptured:
        raise ValueError('turns %s of %s are not captured in %s (missing, capture did not complete, files changed since, or '
                         'differ from the run directory): the finish comes after normalize and capture_turn.py, so the evidence '
                         'hashes are those of the committed files' % (', '.join(uncaptured), run_id, folder / 'turns'))
    censored = json.loads((run / 'censored.json').read_text()) if (run / 'censored.json').exists() else None
    verdict = read_verdict(folder, run_id) if (folder / VERDICT).exists() else None
    invalid = None
    if infra_reason is not None:
        if not infra_reason.strip() or not infra_evidence:
            raise ValueError('INVALID_INFRA needs a reason and at least one evidence file')
        if verdict and verdict['passed']:
            raise ValueError('verdict.json passes every criterion; that is not an infrastructure failure')
        hashes = {}
        for rel in infra_evidence:
            file = inside(folder, rel)
            if not file.is_file():
                raise ValueError('INVALID_INFRA evidence does not exist: ' + rel)
            hashes[rel] = sha(file)
        status = 'INVALID_INFRA'
        invalid = {'reason': infra_reason, 'evidence': hashes}
    elif verdict is None:
        raise ValueError('%s missing: the main session writes the verdict table before the finish' % (folder / VERDICT))
    else:
        status = terminal(verdict['passed'], censored)
    cap = cap_annotation(censored)
    fields, derivation = evidence_fields(run, manifest, verdict)
    if invalid:
        fields['all_acceptance_passed'] = False
    now = datetime.now(timezone.utc).isoformat()
    if supersede_reason is not None:
        kept = folder / ('run.superseded-%s.json' % now.replace(':', '-'))
        shutil.copy2(record_path, kept)
        record['supersedes'] = {'file': kept.name, 'sha256': sha(kept), 'previous_status': record.get('status'),
                                'reason': supersede_reason, 'at': now}
    # the cap record is copied as annotation: the result table tells "completed", "completed + cap reached" (refused
    # before a call) and "completed + overshoot" (a call went past the cap) apart
    record.update(status=status, verdict=verdict, invalid_infra=invalid, cap_reached=cap is not None, cap=cap,
                  cap_status=cap and cap['status'], cap_when=cap and cap['when'], cap_call=cap and cap['call'],
                  cap_overshoot=cap and cap['overshoot'], finished_at=now, evidence_derivation=derivation,
                  turns_not_capturable=not_capturable, **fields)
    tmp = record_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(record_path)
    return record_path, status, cap, fields


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--runs', type=Path)
    p.add_argument('--invalid-infra', metavar='REASON', help='execution-environment failure unrelated to the implementation '
                   '(Docker, host, the judging tool itself); confirmed by the main session before it is recorded')
    p.add_argument('--evidence', action='append', default=[], help='INVALID_INFRA evidence file, relative to the record '
                   'folder (repeat)')
    p.add_argument('--supersede', action='store_true', help='correct an already finished record (the old run.json is kept)')
    p.add_argument('--reason', help='why the finished record is superseded (required with --supersede)')
    a = p.parse_args()
    if a.supersede and not a.reason:
        p.error('--supersede requires --reason')
    if a.reason and not a.supersede:
        p.error('--reason belongs to --supersede (INVALID_INFRA takes its reason as --invalid-infra REASON)')
    if a.evidence and a.invalid_infra is None:
        p.error('--evidence belongs to --invalid-infra')
    try:
        path, status, cap, fields = finish(a.run_id, a.runs, infra_reason=a.invalid_infra, infra_evidence=a.evidence,
                                           supersede_reason=a.reason if a.supersede else None)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        p.error(str(exc))
    print(json.dumps({'record': str(path), 'status': status, 'cap': cap, **fields}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
