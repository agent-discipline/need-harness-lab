#!/usr/bin/env python3
"""Idempotent preparation, without model calls or overwriting experiment code."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lab

def main():
    spec = importlib.util.spec_from_file_location("record", ROOT.parent / "recording/record.py")
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    for core in range(1, 11):
        paths = [ROOT / str(arm) / f"core{core}" for arm in (1, 2, 3)]
        if not any(p.exists() for p in paths):
            lab.clone(core)
        elif not all(p.is_dir() for p in paths):
            raise ValueError(f"partial core{core}; inspect manually")
        for arm, path in enumerate(paths, 1):
            record_dir = ROOT / "docs/experiments" / str(arm) / f"core{core}"
            record.init_run(record_dir)
            issues = record_dir / "issues.md"
            if not issues.exists():
                issues.write_text((ROOT / "docs/issue-template.md").read_text())
            plan = record_dir / "run.json"
            if not plan.exists():
                payload = json.loads((ROOT / "docs/results/run-manifest.example.json").read_text())
                payload.update(arm=arm, core=core, run_id=f"sol-arm{arm}-core{core:02d}", status="NOT_STARTED")
                plan.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    # Frozen source is evaluator-owned; never load it automatically into arms 1/2.
    source = Path("/home/jun/project/claude-code-harness")
    snapshot = ROOT / "harness-source"
    if not snapshot.exists():
        sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if sha != "1754f16e0e4f54ad1a81d05beff15b895a26a1d5":
            raise ValueError("harness source changed; review before snapshot")
        snapshot.mkdir()
        for relative in ("src", "playbooks", "templates", "hooks"):
            shutil.copytree(source / relative, snapshot / relative,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for relative in ("README.md", "CLAUDE.md", "settings.json"):
            shutil.copy2(source / relative, snapshot / relative)
        (snapshot / "SOURCE.json").write_text(json.dumps({"path": str(source), "commit": sha,
            "purpose": "read-only experiment reference; not installed as Codex hooks"}, indent=2) + "\n")
    permutations = [(1, 2, 3)] * 10
    schedule = ROOT / "docs/results/schedule.json"
    if not schedule.exists():
        rows = [dict(order=3*(core-1)+position, arm=arm, core=core,
                     run_id=f"sol-arm{arm}-core{core:02d}")
                for core, arms in enumerate(permutations, 1) for position, arm in enumerate(arms, 1)]
        schedule.write_text(json.dumps(rows, indent=2) + "\n")
    print("Prepared 30 independent starter directories and NOT_STARTED journals; no model executed")

if __name__ == "__main__":
    main()
