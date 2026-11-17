#!/usr/bin/env python3
"""Idempotent Claude-side preparation, without model calls or overwriting experiment code."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]      # payment-settlement/claude
SHARED = ROOT.parent                             # payment-settlement (shared contract, recording, codex snapshot)
sys.path.insert(0, str(ROOT / "scripts"))
import lab

HARNESS_SHA = "1754f16e0e4f54ad1a81d05beff15b895a26a1d5"
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

def main():
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
    # Frozen harness snapshot: byte copy of the Codex-side snapshot (same SOURCE commit), never re-fetched.
    snapshot = ROOT / "harness-source"
    origin = SHARED / "codex/harness-source"
    if not snapshot.exists():
        if json.loads((origin / "SOURCE.json").read_text())["commit"] != HARNESS_SHA:
            raise ValueError("codex harness snapshot commit differs; review before copying")
        shutil.copytree(origin, snapshot, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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
    print("Prepared 30 independent Claude starter directories and NOT_STARTED journals; no model executed")

if __name__ == "__main__":
    main()
