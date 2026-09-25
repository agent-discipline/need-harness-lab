#!/usr/bin/env python3
"""Commit one finished experiment after documenting it, never invent results."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent.parent
TERMINAL = {"PASSED", "FAILED", "CENSORED_BUDGET", "CENSORED_TIME", "CENSORED_TURNS", "INVALID_INFRA"}

def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args], text=True).strip()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arm", type=int, choices=(1, 2, 3), required=True)
    p.add_argument("--core", type=int, choices=range(1, 11), required=True)
    args = p.parse_args()
    folder = ROOT / str(args.arm) / f"core{args.core}"
    relative = str(folder.relative_to(REPO))
    records = ROOT / "docs/experiments" / str(args.arm) / f"core{args.core}"
    records_relative = str(records.relative_to(REPO))
    run = json.loads((records / "run.json").read_text())
    if run["status"] not in TERMINAL:
        p.error("run has not finished; no experimental commit created")
    for name in ("summary.md", "transcript.md", "events.jsonl", "usage-summary.json", "issues.md"):
        file = records / name
        if not file.is_file() or not file.read_text().strip():
            p.error(f"missing result document: {name}")
    spec = importlib.util.spec_from_file_location("record", ROOT.parent / "recording/record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    events = recorder.existing(records)
    if len(events) < 2:
        p.error("no actual prompt/analysis turns")
    if run["status"] == "PASSED" and not all(run.get(x) for x in
            ("usage_complete", "isolation_verified", "all_acceptance_passed", "all_role_models_verified")):
        p.error("passed run lacks validation evidence")
    schedule = json.loads((ROOT / "docs/results/schedule.json").read_text())
    position = next(i for i, x in enumerate(schedule) if (x["arm"], x["core"]) == (args.arm, args.core))
    for prior in schedule[:position]:
        subject = f"experiment: {prior['run_id']} "
        if not any(line.startswith(subject) for line in git("log", "--format=%s").splitlines()):
            p.error(f"previous run has not been committed: {prior['run_id']}")
    if git("diff", "--cached", "--name-only"):
        p.error("index is not empty; inspect unrelated staged changes")
    git("add", "--", relative, records_relative)
    staged = git("diff", "--cached", "--name-only").splitlines()
    if not staged or any(not (name.startswith(relative + "/") or name.startswith(records_relative + "/")) for name in staged):
        p.error("unexpected staged paths or no changes")
    git("commit", "-m", f"experiment: {run['run_id']} {run['status']}")
    print(git("rev-parse", "HEAD"))

if __name__ == "__main__":
    main()
