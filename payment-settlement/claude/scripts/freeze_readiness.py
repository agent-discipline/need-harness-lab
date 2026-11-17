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
LIMITS = dict(token_limit=20000000, time_limit_seconds=28800, feedback_limit=30)
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
    user-proxy consultations of the run, which must be on the agreed proxy model. Used at the freeze (pilot) and at
    each run's finish (scripts/finish_run.py)."""
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
    passed, and its observed tool surface equals the surface frozen for its arm."""
    pre = []
    for path in sorted(Path(run).glob('turns/*/invocation.json')):
        try:
            pre.append(json.loads(path.read_text()).get('hook_selftest_before_call'))
        except (OSError, ValueError):
            pre.append({'passed': False, 'unreadable': str(path)})
    checks = {'init_selftest_passed': (manifest.get('selftest') or {}).get('passed') is True,
              # a harness run (arm 3) self-tests before every call; arms 1/2 record None
              'call_selftests_passed': all((p is None and not manifest.get('harness')) or
                                           (isinstance(p, dict) and p.get('passed') is True) for p in pre),
              'tools_equal_frozen': tools_match(manifest.get('observed_tools'), frozen_tools(readiness, manifest.get('arm')))}
    return all(checks.values()), checks


def smoke_evidence(runs, run_ids):
    """Derive flags from paid smoke runs made by launch.py (manifest + finalized turns)."""
    flags = {k: False for k in ('isolation_verified', 'hook_enforcement_verified', 'oauth_token_source_verified',
                                'cache_observation_verified', 'usage_collector_verified', 'role_models_verified')}
    detail, actual, arms, tools = {}, {}, set(), {}
    ok_turns, all_ok_complete, cache_seen, token_ok, roles_ok, selftests_ok, hooks_ok = 0, True, False, True, True, True, False
    for run_id in run_ids:
        run = Path(runs) / run_id
        manifest = json.loads((run / 'manifest.json').read_text())
        arm = manifest['arm']
        arms.add(arm)
        st = manifest.get('selftest') or {}
        selftests_ok = selftests_ok and st.get('passed') is True
        tools.setdefault(arm, set()).update(t for t in manifest.get('observed_tools') or [] if t)
        if arm == 3:
            hooks_ok = hooks_ok or (st.get('passed') is True and st.get('gate_l1_write_exit') == '2'
                                    and st.get('gate_docs_write_exit') == '0' and st.get('git_guard_push') == 'ask'
                                    and st.get('harness_hash_match') == '1' and st.get('reviewer_on_path') == '1')
        results = [json.loads(p.read_text()) for p in sorted((run / 'turns').glob('*/result.json'))]
        good = [r for r in results if r.get('status') == 'ok' and r.get('session_id_confirmed') is True]
        ok_turns += len(good)
        token_ok = token_ok and bool(good) and all((r.get('config_volume') or {}).get('credentials_file_in_volume') is False for r in good)
        all_ok_complete = all_ok_complete and bool(good) and all(r.get('usage_complete') is True for r in good)
        cache_seen = cache_seen or any(all(isinstance(((r.get('usage_cross_check') or {}).get('reported') or {}).get(k), int)
                                           for k in launch.USAGE_KEYS) for r in good)
        observed = manifest.get('actual_role_models') or {}
        checks = role_check(arm, observed, SMOKE_ROLES.get(arm, ()), run)
        roles_ok = roles_ok and bool(checks) and all(checks.values())
        main_role = SMOKE_ROLES.get(arm, ('',))[0]
        if checks.get(main_role):
            actual[str(arm)] = observed[main_role][0]
        detail[run_id] = dict(arm=arm, manifest_sha256=sha(run / 'manifest.json'), ok_turns=len(good),
                              observed_role_models=observed, role_model_check=checks,
                              observed_tools=sorted(tools.get(arm, ())))
    # same tools in every arm; arm 3 differs only by the subagent surface
    base = [tools[a] for a in (1, 2) if a in tools]
    same_tools = bool(base) and 3 in tools and all(b == base[0] for b in base) and \
        tools[3] - set(launch.SUBAGENT_TOOLS) == base[0] and set(launch.SUBAGENT_TOOLS) <= tools[3]
    flags['isolation_verified'] = bool(run_ids) and selftests_ok and same_tools
    flags['hook_enforcement_verified'] = hooks_ok
    flags['oauth_token_source_verified'] = bool(run_ids) and token_ok
    flags['cache_observation_verified'] = cache_seen
    flags['usage_collector_verified'] = bool(run_ids) and all_ok_complete
    flags['role_models_verified'] = bool(run_ids) and roles_ok and 3 in arms and bool(arms & {1, 2})
    return flags, actual, detail


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
    a = p.parse_args()
    if a.output.exists() and not a.replace:
        p.error('readiness exists; a protocol freeze is not overwritten (use --replace to supersede and keep the old one)')
    head, dirty = clean_baseline()
    if dirty:
        p.error('frozen inputs have uncommitted changes:\n' + '\n'.join(dirty))
    found, missing = images()
    if missing:
        p.error('images not present locally (pull/build first): ' + ', '.join(missing))
    flags, actual, detail = smoke_evidence(a.runs, a.smoke_run)
    smoked = {detail[r]['arm'] for r in detail}
    if smoked != {1, 2, 3} or set(actual) != {'1', '2', '3'}:
        p.error('smoke runs of all three arms with their agreed models are required (arms smoked: %s, verified actual '
                'models: %s)' % (sorted(smoked), sorted(actual)))
    frozen = inventory()
    digest = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    runner = found['payment-claude-runner:' + launch.CLI_VERSION]
    if a.output.exists():
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
    if a.output.exists():
        a.output.rename(a.output.with_name('readiness.superseded-%s.json' % now.replace(':', '-')))
    a.output.write_text(json.dumps(readiness, indent=2, ensure_ascii=False) + '\n')
    a.output.with_name('frozen-inputs.json').write_text(json.dumps(frozen, indent=2) + '\n')
    print(a.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
