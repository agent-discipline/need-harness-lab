#!/usr/bin/env python3
"""Freeze protocol payment-settlement-v3-claude: inputs, image ids and observed preparation evidence.

Every verification flag is derived from evidence files with the criterion written below, or stays false.
Flags that no runner artifact can prove (acceptance suite, harness adapter) come only from an explicit --attest file
whose evidence paths are hashed into readiness.json. pilot_passed is derived from the pilot run (--pilot-run).
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]           # payment-settlement/claude
SHARED = ROOT.parent                                 # payment-settlement
REPO = SHARED.parent
sys.path.insert(0, str(ROOT / 'runner'))
sys.path.insert(0, str(ROOT / 'scripts'))
import launch
import prepare

PROTOCOL = 'payment-settlement-v3-claude'
LIMITS = dict(token_limit=150000000, time_limit_seconds=28800, feedback_limit=30)
# Inputs whose bytes define the protocol (contract, evaluator, fixtures, prompts, harness, runner, compose).
FROZEN = ('docs', 'evaluation', 'infra', 'measurement', 'recording', 'claude/prompts', 'claude/harness-source',
          'claude/runner', 'claude/scripts', 'claude/template', 'claude/compose.yaml')
ATTESTABLE = ('acceptance_suite_verified', 'harness_adapter_verified')     # pilot_passed is derived (pilot_evidence)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inventory():
    result = {}
    for rel in FROZEN:
        path = SHARED / rel
        files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
        for file in files:
            parts = file.relative_to(SHARED).parts
            if '__pycache__' in parts or file.suffix == '.pyc' or {'.gradle', 'build'} & set(parts):
                continue
            result[str(file.relative_to(SHARED))] = sha(file)
    return result


def images():
    """Runner image of this CLI version plus every image the fixtures and starters build from (must exist locally)."""
    tags = ['payment-claude-runner:' + launch.CLI_VERSION]
    for text in ((ROOT / 'compose.yaml').read_text(), (ROOT / 'scripts/load_check.py').read_text()):
        tags += re.findall(r"(?:image:\s*|')((?:postgres|grafana/k6):[A-Za-z0-9._-]+)", text)
    for dockerfile in (ROOT / 'template/Dockerfile', SHARED / 'infra/fake-bank/Dockerfile'):
        tags += re.findall(r'^FROM\s+(\S+)', dockerfile.read_text(), re.M)
    found, missing = {}, []
    for tag in dict.fromkeys(tags):
        done = subprocess.run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], capture_output=True, text=True)
        if done.returncode:
            missing.append(tag)
        else:
            found[tag] = done.stdout.strip()
    return found, missing


def clean_baseline():
    paths = [str((SHARED / rel).relative_to(REPO)) for rel in FROZEN]
    dirty = subprocess.run(['git', 'status', '--porcelain', '--', *paths], cwd=REPO, capture_output=True, text=True, check=True).stdout
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    return head, dirty.splitlines()


# Agreed model per (arm, role) — absolute ids from the protocol (launch.MODELS, launch.REVIEWER_MODEL), never the
# ids a run happened to request.
AGREED = {(1, 'implementer'): launch.MODELS[1][0], (2, 'implementer'): launch.MODELS[2][0],
          (3, 'pm'): launch.MODELS[3][0], (3, 'worker'): launch.MODELS[3][1], (3, 'reviewer'): launch.REVIEWER_MODEL,
          (3, 'user-proxy'): launch.USER_PROXY_MODEL}
# Roles each kind of evidence must show. Smoke runs prove the main role of every arm; the arm 3 pilot proves the rest.
SMOKE_ROLES = {1: ('implementer',), 2: ('implementer',), 3: ('pm',)}
PILOT_ROLES = ('pm', 'worker', 'reviewer')
# Roles a finished run must show (scripts/finish_run.py, all_role_models_verified). Arm 3 also needs the user proxy: the
# pre-implementation interview is mandatory in arm 3, so an arm 3 run without an observed proxy request is not verified.
# The freeze's pilot check keeps PILOT_ROLES (a proxy request is checked there only when one was made).
FINISH_ROLES = {1: ('implementer',), 2: ('implementer',), 3: PILOT_ROLES + ('user-proxy',)}


def model_matches(observed, agreed):
    """Exactly one provider-reported model for the role, and it is the agreed id (exact or that id plus a suffix)."""
    return (bool(agreed) and len(observed or []) == 1 and isinstance(observed[0], str)
            and (observed[0] == agreed or observed[0].startswith(agreed + '-')))


def worker_requests_check(events):
    """Arm 3 workers, per request: an explorer request (agentType 'explorer' from its transcript .meta.json) must be on a
    Sonnet model as the harness defines it; every other worker request, including one whose type is unknown, must be on
    the agreed worker model. A Sonnet request that is not identified as explorer fails."""
    workers = [e for e in events if e.get('role') == 'worker']
    def ok(event):
        if event.get('agent_type') == launch.EXPLORER_AGENT:
            return isinstance(event.get('model'), str) and event['model'].startswith(launch.EXPLORER_MODEL_PREFIX)
        return model_matches([event.get('model')], AGREED[(3, 'worker')])
    return bool(workers) and all(ok(e) for e in workers)


def role_check(arm, observed, required, run=None):
    """Every required role observed with its agreed model, and no observed role outside the agreed set or off-model.
    A required role that was not observed is reported as False, never as verified. Arm 3 workers are checked per request
    from the run's events when the run directory is given (explorer may differ from the other workers)."""
    check = {role: model_matches(observed.get(role), AGREED.get((arm, role))) for role in observed}
    if arm == 3 and run is not None and ('worker' in observed or 'worker' in required):
        events, _ = launch.reconcile_events(Path(run)) if (Path(run) / 'turns').is_dir() else ([], [])
        if any(e.get('role') == 'worker' for e in events):
            # per request; and the manifest's observed worker models must be exactly those of the collected requests
            check['worker'] = worker_requests_check(events) and \
                set(observed.get('worker') or []) == {e['model'] for e in events if e.get('role') == 'worker'}
    for role in required:
        check.setdefault(role, False)
    return check


def run_role_check(run, manifest, required):
    """role_check over one run's own records: the manifest's observed role models (turns) plus, per request, the
    user-proxy consultations of the run, which must be on the agreed proxy model. A required 'user-proxy' without any
    proxy request stays False. Used at the freeze (pilot, PILOT_ROLES) and at each run's finish (FINISH_ROLES)."""
    arm = manifest.get('arm')
    models = role_check(arm, manifest.get('actual_role_models') or {}, required, run)
    proxy_models = sorted({e['model'] for e in launch.reconcile_events(Path(run))[0] if e.get('role') == 'user-proxy'})
    if proxy_models:                                    # consultations made during the run must use the agreed proxy model
        models['user-proxy'] = all(model_matches([m], AGREED.get((arm, 'user-proxy'))) for m in proxy_models)
    return models


def frozen_tools(readiness, arm):
    """The tool surface frozen for an arm: what the smoke runs of that arm observed (readiness evidence.smoke_runs,
    written by smoke_evidence). None when the freeze recorded no smoke run of the arm."""
    runs = ((readiness or {}).get('evidence') or {}).get('smoke_runs') or {}
    surfaces = [set(d.get('observed_tools') or []) for d in runs.values() if isinstance(d, dict) and d.get('arm') == arm]
    return set().union(*surfaces) if surfaces and all(surfaces) else None


def tools_match(observed, frozen):
    """A run's observed tool surface (its init self-test) equals the surface frozen for its arm. The cross-arm
    comparison is made once at the freeze (isolation_verified of smoke_evidence), not per run."""
    observed = {t for t in observed or [] if t}
    return bool(observed) and frozen is not None and observed == set(frozen)


def run_isolation_check(run, manifest, readiness):
    """isolation_verified of one run: its init self-test passed, every hook self-test it ran before a call (arm 3)
    passed — including a failed one that stopped the turn before any turn directory existed (runner
    selftest-failures-before-call/) — and its observed tool surface equals the surface frozen for its arm."""
    pre = []
    for path in sorted(Path(run).glob('turns/*/invocation.json')):
        try:
            pre.append(json.loads(path.read_text()).get('hook_selftest_before_call'))
        except (OSError, ValueError):
            pre.append({'passed': False, 'unreadable': str(path)})
    failed_before_turn = launch.precall_selftest_failures(run)
    checks = {'init_selftest_passed': (manifest.get('selftest') or {}).get('passed') is True,
              # a harness run (arm 3) self-tests before every call; arms 1/2 record None
              'call_selftests_passed': all((p is None and not manifest.get('harness')) or
                                           (isinstance(p, dict) and p.get('passed') is True) for p in pre),
              'no_selftest_failed_before_a_turn': not failed_before_turn,
              'tools_equal_frozen': tools_match(manifest.get('observed_tools'), frozen_tools(readiness, manifest.get('arm')))}
    return all(checks.values()), checks


def smoke_evidence(runs, run_ids, runner_image):
    """Derive flags from paid smoke runs made by launch.py (manifest + finalized turns). Every adopted run counts as a
    whole: a run with any turn that did not end ok with complete usage cannot verify the collector (no picking of the
    good turns), and each run's observed tool surface must equal its own arm's allow-list (no union over runs).
    runner_image (the runner image id being frozen, required): every turn of every adopted run must have run on exactly
    that image, otherwise every smoke-derived flag is false and the runs are named in the detail."""
    if not isinstance(runner_image, str) or not runner_image:
        raise ValueError('smoke_evidence needs the runner image id being frozen')
    flags = {k: False for k in ('isolation_verified', 'hook_enforcement_verified', 'oauth_token_source_verified',
                                'cache_observation_verified', 'usage_collector_verified', 'role_models_verified')}
    detail, actual, arms = {}, {}, set()
    ok_turns, all_ok_complete, cache_seen, token_ok, roles_ok, selftests_ok, hooks_ok = 0, True, False, True, True, True, False
    tools_ok, images_ok = True, True
    reviewer_turns = []                                 # arm 3 smoke turns in which the second reviewer was really called
    for run_id in run_ids:
        run = Path(runs) / run_id
        manifest = json.loads((run / 'manifest.json').read_text())
        arm = manifest['arm']
        arms.add(arm)
        st = manifest.get('selftest') or {}
        selftests_ok = selftests_ok and st.get('passed') is True
        observed_tools = {t for t in manifest.get('observed_tools') or [] if t}
        expected_tools = set(launch.allowed_tools({'arm': arm}))
        tools_ok = tools_ok and observed_tools == expected_tools
        if arm == 3:
            hooks_ok = hooks_ok or (st.get('passed') is True and st.get('gate_l1_write_exit') == '2'
                                    and st.get('gate_docs_write_exit') == '0' and st.get('git_guard_push') == 'ask'
                                    and st.get('harness_hash_match') == '1' and st.get('reviewer_on_path') == '1')
        results = [json.loads(p.read_text()) for p in sorted((run / 'turns').glob('*/result.json'))]
        good = [r for r in results if r.get('status') == 'ok' and r.get('session_id_confirmed') is True]
        ok_turns += len(good)
        run_complete = bool(results) and len(good) == len(results) and all(r.get('usage_complete') is True for r in results)
        token_ok = token_ok and bool(good) and all((r.get('config_volume') or {}).get('credentials_file_in_volume') is False for r in good)
        all_ok_complete = all_ok_complete and run_complete
        cache_seen = cache_seen or any(all(isinstance(((r.get('usage_cross_check') or {}).get('reported') or {}).get(k), int)
                                           for k in launch.USAGE_KEYS) for r in good)
        proven = ['%s/%s' % (run_id, p.parent.name) for p in sorted((run / 'turns').glob('*/result.json'))
                  if arm == 3 and run_complete and reviewer_smoke_turn(json.loads(p.read_text()))]
        reviewer_turns += proven
        observed = manifest.get('actual_role_models') or {}
        checks = role_check(arm, observed, SMOKE_ROLES.get(arm, ()), run)
        roles_ok = roles_ok and bool(checks) and all(checks.values())
        main_role = SMOKE_ROLES.get(arm, ('',))[0]
        if checks.get(main_role):
            actual[str(arm)] = observed[main_role][0]
        image_ids = turn_image_ids(run)
        image_match = bool(image_ids) and set(image_ids) == {runner_image}
        images_ok = images_ok and image_match
        detail[run_id] = dict(arm=arm, manifest_sha256=sha(run / 'manifest.json'), ok_turns=len(good), turns=len(results),
                              all_turns_ok_and_complete=run_complete,
                              observed_role_models=observed, role_model_check=checks,
                              observed_tools=sorted(observed_tools), tools_equal_arm_allow_list=observed_tools == expected_tools,
                              image_ids=image_ids, image_matches_runner=image_match, reviewer_smoke_turns=proven)
    flags['isolation_verified'] = bool(run_ids) and selftests_ok and tools_ok and bool(arms & {1, 2}) and 3 in arms
    flags['hook_enforcement_verified'] = hooks_ok
    flags['oauth_token_source_verified'] = bool(run_ids) and token_ok
    flags['cache_observation_verified'] = cache_seen
    # spec 11.5: the collector is verified only with a real reviewer call in an arm 3 smoke (authentication of the
    # reviewer and collection of its usage under the reviewer role), not with the PM's usage alone
    flags['usage_collector_verified'] = bool(run_ids) and all_ok_complete and bool(reviewer_turns)
    flags['role_models_verified'] = bool(run_ids) and roles_ok and 3 in arms and bool(arms & {1, 2})
    if not images_ok:                                   # evidence from another image proves nothing about this one
        flags = {k: False for k in flags}
    return flags, actual, detail


def reviewer_smoke_turn(result):
    """An arm 3 smoke turn that proves the second reviewer end to end: the turn ended ok with complete usage, at least
    one reviewer call of this turn exited 0, and reviewer-role requests were collected from its transcripts."""
    reviewer = result.get('reviewer') or {}
    return (result.get('status') == 'ok' and result.get('usage_complete') is True
            and (reviewer.get('calls_succeeded_this_turn') or 0) >= 1 and (reviewer.get('event_count') or 0) >= 1
            and 'reviewer' in (result.get('roles') or []) and not result.get('reviewer_usage_pending'))


def turn_image_ids(run):
    """Image ids the run's turns actually ran on (container-inspect.json of every turn that has a result; None where it
    is missing or unreadable, so an unknown image never matches)."""
    ids = set()
    for result in sorted(Path(run).glob('turns/*/result.json')):
        try:
            data = json.loads((result.parent / 'container-inspect.json').read_text())
            ids.add(data[0].get('Image') if isinstance(data, list) and data else None)
        except (OSError, ValueError, AttributeError, KeyError):
            ids.add(None)
    return sorted(ids, key=str)


def pilot_evidence(runs, run_id, contract_sha256, runner_image):
    """Pilot (spec section 5): one arm 3 run carried to its end, whose subagent usage is separated from the parent's
    without double counting and whose second reviewer's usage was observed. Derived from the run's own artifacts.
    It counts only for the freeze it ran under: the contract hash and runner image its manifest recorded at init must
    be the ones of the readiness being written."""
    run = Path(runs) / run_id
    manifest = json.loads((run / 'manifest.json').read_text())
    results = [json.loads(p.read_text()) for p in sorted(run.glob('turns/*/result.json'))]
    ok = [r for r in results if r.get('status') == 'ok']
    ledger = launch.run_ledger(run)
    roles = ledger['roles']
    models = run_role_check(run, dict(manifest, arm=3), PILOT_ROLES)
    checks = {
        'arm_3': manifest.get('arm') == 3,
        'same_contract': bool(contract_sha256) and manifest.get('contract_sha256') == contract_sha256,
        'frozen_image': bool(runner_image) and manifest.get('image') == runner_image,
        # carried to its end: not censored and the last turn finished ok (quality acceptance is not part of the pilot gate)
        'completed': bool(results) and not (run / 'censored.json').exists() and results[-1].get('status') == 'ok',
        'all_usage_observed': ledger['usage_complete'] and not ledger['conflicts'],
        # every ok turn's CLI total matches the parent-only or parent+subagent request sum, each request counted once
        'worker_usage_separated': roles.get('worker', {}).get('requests', 0) > 0 and bool(ok)
                                  and all(r.get('usage_complete') is True for r in ok),
        'reviewer_usage_observed': roles.get('reviewer', {}).get('requests', 0) > 0
                                   and not any(r.get('reviewer_usage_pending') for r in results),
        'role_models_agreed': all(models.values()),
    }
    return all(checks.values()), {'run_id': run_id, 'checks': checks, 'role_model_check': models,
                                  'ledger': {k: ledger[k] for k in ('roles', 'total_tokens', 'usage_complete', 'cost_usd')},
                                  'manifest_sha256': sha(run / 'manifest.json')}


def replace_blockers(previous, frozen, digest, found, listing=None):
    """--replace may re-derive flags (pilot, attestations, smoke runs) but never re-freeze changed inputs: the current
    inventory and image ids must equal those of the readiness being replaced. Empty list = nothing changed."""
    failures = []
    if previous.get('contract_sha256') != digest:
        try:
            old = json.loads(Path(listing).read_text()) if listing else None
        except (OSError, ValueError):
            old = None
        changed = sorted(k for k in old.keys() | frozen.keys() if old.get(k) != frozen.get(k)) if isinstance(old, dict) else []
        failures.append('frozen inputs differ from the readiness being replaced: ' +
                        (', '.join(changed[:20]) if changed else 'contract_sha256 %s != %s' % (previous.get('contract_sha256'), digest)))
    if (previous.get('image_digests') or {}) != dict(found, runner=found.get('payment-claude-runner:' + launch.CLI_VERSION)):
        failures.append('image ids differ from the readiness being replaced')
    return failures


def experiment_started(root=None):
    """Evidence that a measured run (claude-armA-coreNN) has started, read from what start_cycle.py itself uses: each
    run's record (docs/experiments/<arm>/core<N>/run.json status), its run directory (.runs/<run id>), the cycle table
    (docs/results/cycles.json status) and run commits ('experiment: <run id> ...') on every local branch. The run
    directories are always the measured runs' own folder <root>/.runs (never a path given on the command line). Empty
    list = nothing has started. A record that cannot be read counts as started (unknown is never "not started")."""
    import start_cycle                                  # run ids of a cycle (start_cycle.run_plan): one naming source
    root = Path(root or ROOT)
    runs = root / '.runs'
    found = []
    for cycle in range(1, 11):
        for arm in (1, 2, 3):
            run_id = start_cycle.run_plan(cycle, arm)[0]
            record = root / 'docs/experiments' / str(arm) / ('core%d' % cycle) / 'run.json'
            try:
                status = json.loads(record.read_text()).get('status')
            except (OSError, ValueError, AttributeError) as exc:
                found.append('%s: record unreadable (%s)' % (run_id, exc))
                continue
            if status != 'NOT_STARTED':
                found.append('%s: status %s' % (run_id, status))
            if (runs / run_id).exists():
                found.append('%s: run directory exists' % run_id)
    try:
        cycles = json.loads((root / 'docs/results/cycles.json').read_text())
        found += ['cycle %s: status %s' % (c.get('cycle'), c.get('status')) for c in cycles if c.get('status') != 'NOT_STARTED']
    except (OSError, ValueError, AttributeError) as exc:
        found.append('cycles.json unreadable (%s)' % exc)
    done = subprocess.run(['git', '-C', str(root), 'log', '--branches', '--format=%s'], capture_output=True, text=True)
    if done.returncode:
        found.append('git log unreadable: ' + done.stderr.strip()[-200:])
    else:
        found += ['commit: ' + line for line in done.stdout.splitlines() if line.startswith('experiment: claude-arm')]
    return found


# The supersede path is for changes of this arm's own runner, scripts, image and records only: a changed shared input
# (contract documents, evaluator, fixtures, measurement, recording) is a protocol change (spec 2).
SUPERSEDE_ALLOWED_PREFIX = 'claude/'


def supersede_plan(output, reason, now, frozen):
    """Everything the supersession needs, checked before any file is touched: the archive names are free, the frozen
    inputs listing of the freeze being replaced is readable, and only inputs under SUPERSEDE_ALLOWED_PREFIX changed.
    Raises ValueError naming the refusal. Returns (record for the new readiness, [(path, archive path)])."""
    previous = json.loads(output.read_text())
    listing = output.with_name('frozen-inputs.json')
    try:
        old = json.loads(listing.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('the frozen inputs of the freeze being replaced (%s) cannot be read (%s); the changed inputs '
                         'cannot be established' % (listing, exc))
    if not isinstance(old, dict):
        raise ValueError('%s is not a frozen inputs listing' % listing)
    changed = sorted(k for k in old.keys() | frozen.keys() if old.get(k) != frozen.get(k))
    shared = [k for k in changed if not k.startswith(SUPERSEDE_ALLOWED_PREFIX)]
    if shared:
        raise ValueError('shared inputs changed since the freeze (%s): the contract, evaluator and shared fixtures do not '
                         'change after a freeze — that is a protocol version change, not a supersession' % ', '.join(shared[:20]))
    stamp = now.replace(':', '-')
    moves, archived = [], {}
    for path, name in ((output, 'readiness.superseded-%s.json' % stamp), (listing, 'frozen-inputs.superseded-%s.json' % stamp)):
        target = path.with_name(name)
        if target.exists():
            raise ValueError('archive %s already exists' % target)
        moves.append((path, target))
        archived[path.name] = {'archived_as': name, 'sha256': sha(path)}
    record = {'reason': reason, 'at': now, 'previous_contract_sha256': previous.get('contract_sha256'),
              'previous_image_digests': previous.get('image_digests'), 'previous_frozen_at': previous.get('frozen_at'),
              'archived': archived, 'pilot_passed_reset': True, 'changed_inputs': changed,
              'rule': 'replaced before any measured run started (freeze_readiness.experiment_started was empty); only '
                      'inputs under %s changed' % SUPERSEDE_ALLOWED_PREFIX}
    return record, moves


def write_freeze(output, readiness, frozen, moves):
    """Write the new readiness.json and frozen-inputs.json atomically as a pair: both are written under temporary names
    first, every file that exists now is kept under a hard-linked backup name, then the files being replaced are
    archived (moves) and the new files take their place. If any step fails, both files return to their previous state
    (restored from the backups, or removed when they did not exist), the archives made in this call are removed (the
    backups held the same files), and no temporary file is left: never a mix of the old and the new freeze."""
    listing = output.with_name('frozen-inputs.json')
    finals = (output, listing)
    staged, backups, archived = [], {}, []
    try:
        for path, text in ((output, json.dumps(readiness, indent=2, ensure_ascii=False) + '\n'),
                           (listing, json.dumps(frozen, indent=2) + '\n')):
            tmp = path.with_name('.%s.tmp-%d' % (path.name, os.getpid()))
            tmp.write_text(text)
            staged.append((tmp, path))
        for path in finals:
            if path.exists():
                backup = path.with_name('.%s.bak-%d' % (path.name, os.getpid()))
                os.link(path, backup)
                backups[path] = backup
        try:
            for path, target in moves:
                path.rename(target)
                archived.append(target)
            for tmp, path in staged:
                tmp.replace(path)
        except BaseException:
            for path in finals:
                if path in backups:                       # restored with link/unlink only: the rollback never needs
                    backup = backups.pop(path)            # the rename/replace that may be what just failed
                    if not (path.exists() and os.path.samefile(path, backup)):
                        if path.exists():
                            path.unlink()
                        os.link(backup, path)
                    backup.unlink()
                elif path.exists():
                    path.unlink()
            for target in archived:
                if target.exists():
                    target.unlink()
            raise
    finally:
        for leftover in [tmp for tmp, _ in staged] + list(backups.values()):
            if leftover.exists():
                leftover.unlink()


def attested(path):
    """{flag: {"value": bool, "evidence": [repo-relative paths]}} for ATTESTABLE flags only; evidence is hashed."""
    data = json.loads(Path(path).read_text())
    flags, evidence = {}, {}
    for key, item in data.items():
        if key not in ATTESTABLE:
            raise ValueError('only %s can be attested, not %s' % (', '.join(ATTESTABLE), key))
        files = item.get('evidence') or []
        if item.get('value') is True and not files:
            raise ValueError(key + ': a true attestation needs evidence files')
        hashes = {}
        for rel in files:
            file = (REPO / rel).resolve()
            if REPO not in file.parents or not file.is_file():
                raise ValueError(key + ': evidence must be an existing file inside the repository: ' + rel)
            hashes[rel] = sha(file)
        flags[key] = item.get('value') is True
        evidence[key] = hashes
    return flags, evidence


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--smoke-run', action='append', default=[], help='launch.py run id of a paid smoke (repeat; arms 1|2 and 3)')
    p.add_argument('--runs', type=Path, default=ROOT / '.runs')
    p.add_argument('--attest', type=Path, help='JSON attestation for ' + ', '.join(ATTESTABLE))
    p.add_argument('--pilot-run', help='launch.py run id of the finished arm 3 pilot; pilot_passed is derived from it')

    p.add_argument('--output', type=Path, default=ROOT / 'docs/results/readiness.json')
    p.add_argument('--replace', action='store_true', help='supersede an existing readiness.json (kept with a timestamp) '
                   'with re-derived flags; refused when the frozen inputs or image ids changed (protocol version change)')
    p.add_argument('--supersede-before-runs', action='store_true',
                   help='replace the freeze although frozen inputs or image ids changed, ONLY while no measured run has '
                   'started (experiment_started); needs --reason; archives readiness.json and frozen-inputs.json, records '
                   'the supersession and resets pilot_passed (no --pilot-run). Same protocol name.')
    p.add_argument('--reason', help='with --supersede-before-runs: why the freeze is replaced (required)')
    a = p.parse_args()
    if a.supersede_before_runs:
        if a.replace:
            p.error('--supersede-before-runs and --replace are different paths; give one')
        if not (a.reason or '').strip():
            p.error('--supersede-before-runs requires --reason')
        if a.pilot_run:
            p.error('--supersede-before-runs resets pilot_passed; the pilot is run again under the new freeze (no --pilot-run)')
        if not a.output.exists():
            p.error('nothing to supersede: %s does not exist' % a.output)
        started = experiment_started()
        if started:
            p.error('a measured run has started (%s): the freeze can no longer be superseded; a change requires a protocol '
                    'version change and a re-run of every arm' % '; '.join(started[:10]))
    elif a.reason:
        p.error('--reason applies only to --supersede-before-runs')
    if a.output.exists() and not (a.replace or a.supersede_before_runs):
        p.error('readiness exists; a protocol freeze is not overwritten (use --replace to supersede and keep the old one)')
    head, dirty = clean_baseline()
    if dirty:
        p.error('frozen inputs have uncommitted changes:\n' + '\n'.join(dirty))
    found, missing = images()
    if missing:
        p.error('images not present locally (pull/build first): ' + ', '.join(missing))
    runner = found['payment-claude-runner:' + launch.CLI_VERSION]
    flags, actual, detail = smoke_evidence(a.runs, a.smoke_run, runner)
    mismatched = {r: d.get('image_ids') for r, d in detail.items() if d.get('image_matches_runner') is False}
    if mismatched:
        print('smoke runs not on the runner image being frozen (%s); every smoke-derived flag is false: %s'
              % (runner, json.dumps(mismatched)), file=sys.stderr)
    smoked = {detail[r]['arm'] for r in detail}
    if smoked != {1, 2, 3} or set(actual) != {'1', '2', '3'}:
        p.error('smoke runs of all three arms with their agreed models are required (arms smoked: %s, verified actual '
                'models: %s)' % (sorted(smoked), sorted(actual)))
    frozen = inventory()
    digest = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    if a.output.exists() and a.replace:
        changed = replace_blockers(json.loads(a.output.read_text()), frozen, digest, found,
                                   a.output.with_name('frozen-inputs.json'))
        if changed:
            p.error('; '.join(changed) + ' — nothing changes after the freeze: a change requires a protocol version change '
                    '(new protocol, new freeze) and a re-run of every arm, not --replace')
    attest_flags, attest_evidence = attested(a.attest) if a.attest else ({}, {})
    pilot_passed, pilot_detail = pilot_evidence(a.runs, a.pilot_run, digest, runner) if a.pilot_run else (False, None)
    now = datetime.now(timezone.utc).isoformat()
    readiness = dict(
        protocol=PROTOCOL, provider='claude-code', auth='subscription-oauth-token', cli_version=launch.CLI_VERSION,
        requested_models={str(arm): dict(zip(('model', 'worker_model'), m)) for arm, m in launch.MODELS.items()},
        requested_reviewer_model=launch.REVIEWER_MODEL, user_proxy_model=launch.USER_PROXY_MODEL,
        actual_models=actual, reasoning_effort='high', cache_policy='observed-cache', billed_cost=None,
        baseline_sha=head, harness_sha=prepare.HARNESS_SHA,
        contract_sha256=digest, image_digests=dict(found, runner=runner),
        **LIMITS, **flags, **{k: attest_flags.get(k, False) for k in ATTESTABLE}, pilot_passed=pilot_passed,
        evidence=dict(smoke_runs=detail, attested=attest_evidence, pilot=pilot_detail), frozen_at=now,
        note='flags derive from smoke-run artifacts or hashed attestations; false means not yet shown, never assumed')
    moves = []
    if a.supersede_before_runs:
        try:
            readiness['supersedes'], moves = supersede_plan(a.output, a.reason.strip(), now, frozen)
        except ValueError as exc:
            p.error(str(exc))
    elif a.output.exists():
        moves = [(a.output, a.output.with_name('readiness.superseded-%s.json' % now.replace(':', '-')))]
    write_freeze(a.output, readiness, frozen, moves)
    print(a.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
