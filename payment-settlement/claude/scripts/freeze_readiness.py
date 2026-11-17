#!/usr/bin/env python3
"""Freeze protocol payment-settlement-v3-claude: inputs, image ids and observed preparation evidence.

Every verification flag is derived from evidence files with the criterion written below, or stays false.
Flags that no runner artifact can prove (acceptance suite, harness adapter, pilot) come only from an explicit
--attest file whose evidence paths are hashed into readiness.json. Never claims a pilot result by itself.
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
ATTESTABLE = ('acceptance_suite_verified', 'harness_adapter_verified', 'pilot_passed')


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
          (3, 'pm'): launch.MODELS[3][0], (3, 'worker'): launch.MODELS[3][1], (3, 'reviewer'): launch.REVIEWER_MODEL}
# Roles each kind of evidence must show. Smoke runs prove the main role of every arm; the arm 3 pilot proves the rest.
SMOKE_ROLES = {1: ('implementer',), 2: ('implementer',), 3: ('pm',)}
PILOT_ROLES = ('pm', 'worker', 'reviewer')


def model_matches(observed, agreed):
    """Exactly one provider-reported model for the role, and it is the agreed id (exact or that id plus a suffix)."""
    return (bool(agreed) and len(observed or []) == 1 and isinstance(observed[0], str)
            and (observed[0] == agreed or observed[0].startswith(agreed + '-')))


def role_check(arm, observed, required):
    """Every required role observed with its agreed model, and no observed role outside the agreed set or off-model.
    A required role that was not observed is reported as False, never as verified."""
    check = {role: model_matches(observed.get(role), AGREED.get((arm, role))) for role in observed}
    for role in required:
        check.setdefault(role, False)
    return check


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
        checks = role_check(arm, observed, SMOKE_ROLES.get(arm, ()))
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
    p.add_argument('--user-proxy-model', required=True,
                   help='user proxy model chosen by the user (no default); consult_contract.py refuses any other model')
    p.add_argument('--output', type=Path, default=ROOT / 'docs/results/readiness.json')
    p.add_argument('--replace', action='store_true', help='supersede an existing readiness.json (kept with a timestamp)')
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
    attest_flags, attest_evidence = attested(a.attest) if a.attest else ({}, {})
    frozen = inventory()
    now = datetime.now(timezone.utc).isoformat()
    readiness = dict(
        protocol=PROTOCOL, provider='claude-code', auth='subscription-oauth-token', cli_version=launch.CLI_VERSION,
        requested_models={str(arm): dict(zip(('model', 'worker_model'), m)) for arm, m in launch.MODELS.items()},
        requested_reviewer_model=launch.REVIEWER_MODEL, user_proxy_model=a.user_proxy_model,
        actual_models=actual, reasoning_effort='high', cache_policy='observed-cache', billed_cost=None,
        baseline_sha=head, harness_sha=prepare.HARNESS_SHA,
        contract_sha256=hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
        image_digests=dict(found, runner=found['payment-claude-runner:' + launch.CLI_VERSION]),
        **LIMITS, **flags, **{k: attest_flags.get(k, False) for k in ATTESTABLE},
        evidence=dict(smoke_runs=detail, attested=attest_evidence), frozen_at=now,
        note='flags derive from smoke-run artifacts or hashed attestations; false means not yet shown, never assumed')
    if a.output.exists():
        a.output.rename(a.output.with_name('readiness.superseded-%s.json' % now.replace(':', '-')))
    a.output.write_text(json.dumps(readiness, indent=2, ensure_ascii=False) + '\n')
    a.output.with_name('frozen-inputs.json').write_text(json.dumps(frozen, indent=2) + '\n')
    print(a.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
