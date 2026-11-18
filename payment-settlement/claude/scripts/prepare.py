#!/usr/bin/env python3
"""Idempotent Claude-side preparation, without model calls or overwriting experiment code."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]      # payment-settlement/claude
SHARED = ROOT.parent                             # payment-settlement (shared contract, recording, codex snapshot)
sys.path.insert(0, str(ROOT / "scripts"))
import lab

# Harness v4.3 (main, 2026-11-17). Snapshot paths = the deploy manifest of hooks/deploy.sh (core.md HISTORY.md hooks
# playbooks templates agents) plus the repo-root reference files; .mcp.json (host-only paths) and docs/archive are not
# part of an installed harness.
HARNESS_REPO = Path("/home/jun/project/agent-discipline/harness")
HARNESS_SHA = "7b0ef7b45aa88bab7d529f1ea1f61a69bbad6ce3"
HARNESS_PATHS = ("src", "hooks", "playbooks", "templates", "agents", "HISTORY.md", "README.md", "CLAUDE.md", "settings.json")
# Agreed model control (v3-claude, 2026-11-17): implementers Opus 5.5, arm 3 PM Fable 5.2 with Opus 5.5 workers,
# effort high everywhere. Must stay equal to runner/launch.py MODELS.
MODELS = {
    1: {"requested_model": "claude-opus-5-5"},
    2: {"requested_model": "claude-opus-5-5"},
    3: {"requested_model": "claude-fable-5-2", "requested_pm_model": "claude-fable-5-2",
        "requested_worker_model": "claude-opus-5-5"},
}

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def snapshot_harness(snapshot):
    """Extract HARNESS_PATHS of the frozen commit into snapshot/ and write SOURCE.json (fails if the commit is absent)."""
    archive = subprocess.run(["git", "-C", str(HARNESS_REPO), "archive", "--format=tar", HARNESS_SHA, *HARNESS_PATHS],
                             capture_output=True, check=False)
    if archive.returncode:
        raise ValueError("harness commit %s not readable from %s: %s" % (HARNESS_SHA, HARNESS_REPO, archive.stderr.decode()[-300:]))
    snapshot.mkdir()
    subprocess.run(["tar", "-x", "-C", str(snapshot)], input=archive.stdout, check=True)
    (snapshot / "SOURCE.json").write_text(json.dumps({
        "path": str(HARNESS_REPO), "commit": HARNESS_SHA, "version": "v4.3", "paths": list(HARNESS_PATHS),
        "method": "git archive of the commit (working tree not used)",
        "purpose": "frozen harness for arm 3; runner seeds core.md, HISTORY.md, hooks, playbooks, templates, agents"},
        indent=2) + "\n")

LIMIT_KEYS = ("token_limit", "time_limit_seconds", "feedback_limit")

def refresh_limits(root=None):
    """The one exception to "values already present are never overwritten": rewrite only the three cap fields of
    NOT_STARTED run records from the template (docs/results/run-manifest.example.json), and only while no measured
    run has started (freeze_readiness.experiment_started). Every other field and every record in another state is left
    as it is. Returns [(record, field, before, after)] of the fields changed."""
    root = Path(root or ROOT)
    import freeze_readiness                      # imported here: the check reads the measured runs' evidence
    started = freeze_readiness.experiment_started(root)
    if started:
        raise ValueError("a measured run has started (%s): run records are not rewritten" % "; ".join(started[:5]))
    template = json.loads((root / "docs/results/run-manifest.example.json").read_text())
    changes = []
    for arm in (1, 2, 3):
        for core in range(1, 11):
            plan = root / "docs/experiments" / str(arm) / f"core{core}" / "run.json"
            current = json.loads(plan.read_text())
            if current.get("status") != "NOT_STARTED":
                continue
            changed = [(key, current.get(key), template[key]) for key in LIMIT_KEYS if current.get(key) != template[key]]
            if not changed:
                continue
            for key, _, after in changed:
                current[key] = after
            plan.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
            changes += [(str(plan.relative_to(root)), key, before, after) for key, before, after in changed]
    return changes

def main():
    if sys.argv[1:] == ["--refresh-limits"]:
        changes = refresh_limits()
        for path, key, before, after in changes:
            print("%s %s: %s -> %s" % (path, key, before, after))
        print("refreshed %d field(s); other fields and records not in NOT_STARTED unchanged" % len(changes))
        return
    if sys.argv[1:]:
        raise SystemExit("usage: prepare.py [--refresh-limits]")
    record = load(SHARED / "recording/record.py", "record")
    for core in range(1, 11):
        paths = [ROOT / str(arm) / f"core{core}" for arm in (1, 2, 3)]
        if not any(p.exists() for p in paths):
            lab.clone(core)
        elif not all(p.is_dir() for p in paths):
            raise ValueError(f"partial core{core}; inspect manually")
        for arm, path in enumerate(paths, 1):
            record_dir = ROOT / "docs/experiments" / str(arm) / f"core{core}"
            record.init_run(record_dir)
            plan = record_dir / "run.json"
            example = json.loads((ROOT / "docs/results/run-manifest.example.json").read_text())
            if not plan.exists():
                payload = dict(example)
                payload.update(arm=arm, core=core, run_id=f"claude-arm{arm}-core{core:02d}", status="NOT_STARTED")
                payload.update(MODELS[arm])
                plan.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            else:
                # keys added to the example later are backfilled; values already present are never overwritten
                current = json.loads(plan.read_text())
                missing = {k: v for k, v in example.items() if k not in current}
                if missing:
                    current.update(missing)
                    plan.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    # Frozen harness snapshot: the committed tree of HARNESS_SHA (git archive, never the working tree), created once.
    snapshot = ROOT / "harness-source"
    if not snapshot.exists():
        snapshot_harness(snapshot)
    if json.loads((snapshot / "SOURCE.json").read_text())["commit"] != HARNESS_SHA:
        raise ValueError("claude harness snapshot commit differs from frozen SHA")
    # Run order is DERIVED from the Codex schedule (same order/cycle/policies; only run_id is re-prefixed),
    # so the two conditions keep identical counterbalancing. verify_setup asserts this parity.
    codex_schedule = json.loads((SHARED / "codex/docs/results/schedule.json").read_text())
    rows = [dict(row, run_id=f"claude-arm{row['arm']}-core{row['core']:02d}") for row in codex_schedule]
    schedule = ROOT / "docs/results/schedule.json"
    current = json.loads(schedule.read_text()) if schedule.exists() else None
    if current != rows:
        schedule.write_text(json.dumps(rows, indent=2) + "\n")
        print("schedule.json regenerated from codex/docs/results/schedule.json (%d rows)" % len(rows))
    # One row per cycle (start_cycle.py gates on it); created once, never rewritten once cycles have state.
    cycles = ROOT / "docs/results/cycles.json"
    if not cycles.exists():
        plan = [dict(cycle=core, status="NOT_STARTED", runs=[r["run_id"] for r in rows if r["core"] == core],
                     max_parallel_implementers=3, max_parallel_load_tests=1,
                     next_cycle_requires="all three terminal, documented, committed") for core in range(1, 11)]
        cycles.write_text(json.dumps(plan, indent=2) + "\n")
    print("Prepared 30 independent Claude starter directories and NOT_STARTED journals; no model executed")

if __name__ == "__main__":
    main()
