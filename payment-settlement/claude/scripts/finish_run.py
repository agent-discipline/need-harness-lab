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
import re
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


CORRECTNESS = tuple('C%02d' % n for n in range(1, 20))      # C01..C19; C20 is the load criterion
K6_LATENCY = ('payment_latency', 'http_req_duration')   # the workload's own payment latency first


PHASE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


def count_of(samples):
    """The last PENDING count of a phase, or None when the samples are not a list of {count: int}."""
    if isinstance(samples, list) and samples and isinstance(samples[-1], dict) and isinstance(samples[-1].get('count'), int):
        return samples[-1]['count']
    return None


def phase_figures(folder, summary_path, phase):
    """One phase row from load_check.py's summary entry and the k6-summary.json beside it. The phase name is used as
    a path component only when it is a plain name (no separator, no '..') and the file it names is inside the record
    folder; a field in an unexpected shape is None with a note. Returns None for a phase without a plain name."""
    name = phase.get('phase') if isinstance(phase, dict) else None
    if not isinstance(name, str) or not PHASE_NAME.fullmatch(name) or '..' in name:
        return None
    notes = []
    pending = count_of(phase.get('pending_samples'))
    if phase.get('pending_samples') is not None and pending is None:
        notes.append('pending_samples in an unexpected shape')
    row = {'phase': name, 'passed': phase.get('passed'), 'k6_exit_code': phase.get('k6_exit_code'),
           'pending_converged': phase.get('pending_converged'), 'pending_last': pending,
           'audit_passed': phase.get('audit_passed'), 'resources_passed': phase.get('resources_passed'),
           'p95_ms': None, 'p99_ms': None, 'latency_metric': None, 'dropped_iterations': None,
           'committed_response_ratio': None, 'unexpected_failure_ratio': None, 'requests': None, 'notes': notes}
    k6 = (summary_path.parent / name / 'k6-summary.json').resolve()
    if folder.resolve() not in k6.parents:
        notes.append('k6-summary.json would lie outside the record folder; not read')
        return row
    try:
        metrics = json.loads(k6.read_text())['metrics']
        if not isinstance(metrics, dict):
            raise ValueError('metrics is not an object')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        row['k6_summary'] = 'unreadable: %s' % exc
        return row
    def number(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    for metric in K6_LATENCY:
        if isinstance(metrics.get(metric), dict) and 'p(95)' in metrics[metric]:
            row.update(p95_ms=number(metrics[metric].get('p(95)')), p99_ms=number(metrics[metric].get('p(99)')), latency_metric=metric)
            break
    dropped = metrics.get('dropped_iterations')
    row['dropped_iterations'] = number(dropped.get('count')) if isinstance(dropped, dict) else None   # absent stays None
    for key, metric in (('committed_response_ratio', 'committed_response'), ('unexpected_failure_ratio', 'unexpected_failure')):
        if isinstance(metrics.get(metric), dict):
            row[key] = number(metrics[metric].get('value'))
    if isinstance(metrics.get('iterations'), dict):
        row['requests'] = number(metrics['iterations'].get('count'))
    return row


def load_metrics(folder, c20_evidence):
    """C20 phase figures moved from the judging artifacts the verdict cites for C20 (load_check.py's summary.json and
    each phase's k6-summary.json/result.json beside it). Nothing is filled in: a figure the files do not hold is None,
    and when the files are not there or not in the expected shape the reason is recorded instead."""
    cited = [rel for rel in c20_evidence or [] if rel.endswith('load/summary.json')]
    if not cited:
        return {'available': False, 'reason': 'no load/summary.json among the C20 evidence of verdict.json'}
    path = inside(folder, cited[0])
    try:
        summary = json.loads(path.read_text())
        phases = summary['phases']
        if not isinstance(phases, list):
            raise ValueError('phases is not a list')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'available': False, 'source': cited[0], 'reason': 'unreadable or another shape: %s' % exc}
    rows, problems = [], []
    for phase in phases:
        try:
            row = phase_figures(folder, path, phase)
        except Exception as exc:                        # one odd phase never stops the finish
            row = {'phase': phase.get('phase') if isinstance(phase, dict) else None,
                   'error': 'not summarized: %s: %s' % (type(exc).__name__, str(exc)[:200])}
        if row is None:
            problems.append('a phase without a plain name: %r' % (phase.get('phase') if isinstance(phase, dict) else phase,))
            continue
        rows.append(row)
    return {'available': True, 'source': cited[0], 'c20_passed': summary.get('c20_passed'),
            'phase_gates_passed': summary.get('phase_gates_passed'), 'sustainable_rps': summary.get('sustainable_rps'),
            'phases': rows, 'problems': problems,
            'note': 'figures copied from the k6 and load_check artifacts; None = not in those files (e.g. k6 omits '
                    'dropped_iterations when none were dropped — not read as 0 here); PENDING convergence after the '
                    'load is in the judging folder, not summarized here'}


def result_summary(run, folder, verdict, cap):
    """Result figures reported beside the status (decision 2026-11-18, approval 2): the verdict rule is unchanged
    (PASSED = all of C01..C20 PASS); correctness C01..C19, the C20 result and its load figures, tokens per role and in
    total (cache reads apart), wall-clock time, the paid call time the time cap counts and the cap annotation."""
    out = {'rule': 'status unchanged: PASSED only when all of C01..C20 are PASS; these figures are reported beside it'}
    if verdict:
        results = verdict['results']
        out['correctness'] = {'criteria': 'C01..C19', 'passed': sum(1 for c in CORRECTNESS if results[c] == 'PASS'),
                              'of': len(CORRECTNESS),
                              'failed': {c: results[c] for c in CORRECTNESS if results[c] in ('FAIL', 'PARTIAL')},
                              'not_run': [c for c in CORRECTNESS if results[c] == 'NOT_RUN']}
        try:
            c20_evidence = json.loads((folder / VERDICT).read_text())['criteria']['C20'].get('evidence')
        except (OSError, ValueError, KeyError, AttributeError):
            c20_evidence = None
        out['c20'] = {'result': results['C20'], 'load': load_metrics(folder, c20_evidence)}
    else:
        out['correctness'] = out['c20'] = None
        out['no_verdict'] = 'no verdict.json (INVALID_INFRA without a verdict table)'
    ledger = launch.run_ledger(run)
    roles = {}
    for role, data in ledger['roles'].items():
        usage = data['usage']
        roles[role] = {'requests': data['requests'], 'total_tokens': data['total_tokens'],
                       'cache_read_input_tokens': usage['cache_read_input_tokens'],
                       'tokens_without_cache_reads': data['total_tokens'] - usage['cache_read_input_tokens']}
    cache_reads = sum(r['cache_read_input_tokens'] for r in roles.values())
    out['tokens'] = {'by_role': roles, 'total_tokens': ledger['total_tokens'], 'cache_read_input_tokens': cache_reads,
                     'tokens_without_cache_reads': ledger['total_tokens'] - cache_reads,
                     'usage_complete': ledger['usage_complete'], 'tokens_are_lower_bound': ledger['tokens_are_lower_bound'],
                     'counted': 'input + cache creation + cache read + output, request-level, every role of the run',
                     'cost_usd': ledger.get('cost_usd')}
    starts, ends = [], []
    for ident, _, _, started in launch.turn_records(run):
        if started:
            starts.append(started)
        try:
            finished = launch.parse_time(json.loads((run / 'turns' / ident / 'result.json').read_text()).get('finished_at'))
        except (OSError, ValueError):
            finished = None
        if finished:
            ends.append(finished)
    out['elapsed'] = {'first_turn_started_at': min(starts).isoformat() if starts else None,
                      'last_turn_finished_at': max(ends).isoformat() if ends else None,
                      'seconds': (max(ends) - min(starts)).total_seconds() if starts and ends else None,
                      'turns': len(launch.turn_records(run)),
                      'measure': 'wall clock, first turn start to last turn end; not what the time cap counts (call_time)'}
    out['call_time'] = launch.call_time(run)        # what time_limit_seconds counts (decision 2026-11-19)
    out['cap'] = None if cap is None else {k: cap.get(k) for k in ('status', 'when', 'call', 'overshoot')}
    return out


def safe_result_summary(run, folder, verdict, cap):
    """result_summary, never a reason for the finish to fail: an error is recorded in its place."""
    try:
        return result_summary(run, folder, verdict, cap)
    except Exception as exc:
        return {'available': False, 'error': 'result summary not made: %s: %s' % (type(exc).__name__, str(exc)[:300])}


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
                  turns_not_capturable=not_capturable, result_summary=safe_result_summary(run, folder, verdict, cap), **fields)
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
