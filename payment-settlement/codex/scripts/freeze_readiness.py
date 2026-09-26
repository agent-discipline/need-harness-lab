#!/usr/bin/env python3
"""Freeze observed preparation evidence; never claim the pilot quality passed."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / 'docs/pilots/detailed-01'


def read(path):
    return json.loads(path.read_text())


def main():
    correctness = read(PILOT / 'verification-02/http.json')
    faults = read(PILOT / 'verification-02/faults.json')
    audit = read(PILOT / 'verification-02/audit.json')
    gate = read(ROOT / 'docs/results/setup/harness-gate/verification.json')
    load = read(PILOT / 'load-preparation/results/summary.json')
    review = read(PILOT / 'review/attempt-01/summary.json')
    cases = correctness.get('cases', correctness.get('results', {}))
    required = ['C%02d' % i for i in list(range(1, 13)) + [15, 16, 18]]
    assert all(cases[k]['status'] == 'PASS' for k in required), 'correctness preparation failed'
    assert all(faults['cases'][k]['status'] == 'PASS' for k in ('C13', 'C14', 'C17'))
    assert audit['status'] == 'PASS'
    assert {p['phase'] for p in load['phases']} >= {'warmup', 'normal'}
    assert all(p['audit_passed'] and p['resources_passed'] and p['pending_converged'] for p in load['phases'])
    metrics = read(PILOT / 'load-preparation/results/normal/k6-summary.json')['metrics']
    assert metrics['iterations']['count'] >= 15000 and metrics['dropped_iterations']['count'] == 0, 'generator cannot deliver target'
    # A correctly detected candidate SLO failure does not invalidate the evaluator.

    assert all(gate.get(k) is True for k in ('readonly_denied', 'approved_write_verified',
        'configured_models_verified', 'all_spawned_workers_collected', 'all_threads_have_usage',
        'all_threads_have_model_context'))
    assert review['resolved_models'] == ['claude-fable-5-1'] and review['review_completed']
    inventory = {}
    for folder in (ROOT.parent / 'docs', ROOT.parent / 'evaluation', ROOT / 'prompts'):
        for path in sorted(folder.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                inventory[str(path.relative_to(ROOT.parent))] = hashlib.sha256(path.read_bytes()).hexdigest()
    def image(tag):
        return subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()
    result = dict(protocol='payment-settlement-v2', configured_model='gpt-6-sol',
        selected_model='gpt-6-sol', actual_model=None, backend_model_identity_unavailable=True,
        model_configuration_verified=True, reasoning_effort='high',
        baseline_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        contract_sha256=hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
        image_digests={key:image(tag) for key,tag in [('runner','payment-codex-runner:0.155.1'),
            ('postgres','postgres:16'),('k6','grafana/k6:1.2.3'),
            ('bank','payment-codex-pilot-verify3-bank'),('jdk','eclipse-temurin:21-jdk'),
            ('jre','eclipse-temurin:21-jre'),('python','python:3.12-slim')]},
        isolation_verified=True, usage_collector_verified=True, harness_adapter_verified=True,
        acceptance_suite_verified=True, pilot_validation_completed=True,
        pilot_quality_passed=False, pilot_normal_slo_passed=False, pilot_full_c20_run=False,
        cache_policy='observed-cache', cache_observation_verified=True,
        token_limit=20000000, time_limit_seconds=28800, feedback_limit=30,
        external_reviewer_model='claude-fable-5-1',
        note='Preparation verified by observed failures, fixes, negative controls, runtime checks and 50RPS calibration. Formal candidates still require full C20 and one Fable review.')
    destination = ROOT / 'docs/results/readiness.json'
    destination.write_text(json.dumps(result, indent=2) + '\n')
    (ROOT / 'docs/results/frozen-inputs.json').write_text(json.dumps(inventory, indent=2) + '\n')
    print(destination)


if __name__ == '__main__':
    main()
