#!/usr/bin/env python3
"""Verify Claude-side preparation against the Codex side; never an experiment result."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CODEX = ROOT.parent / "codex"
sys.path.insert(0, str(ROOT / "scripts"))
import lab

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()

def main():
    evidence = ROOT / "docs/results/setup"
    evidence.mkdir(parents=True, exist_ok=True)
    starters_ok = True
    for core in range(1, 11):
        try:
            lab.verify(core)
        except ValueError as exc:
            starters_ok = False
            print(str(exc), file=sys.stderr)
    codex_lab = load(CODEX / "scripts/lab.py", "codex_lab")
    parity = {}
    for relative in ("template", "prompts", "harness-source"):
        parity[relative] = lab.inventory(ROOT / relative) == codex_lab.inventory(CODEX / relative) if relative != "prompts" else None
    shared_prompts = ("common.md", "1-minimal.md", "feedback.md", "slo.md")
    parity["prompts"] = all((ROOT / "prompts" / p).read_bytes() == (CODEX / "prompts" / p).read_bytes() for p in shared_prompts)
    parity["compose.yaml"] = (ROOT / "compose.yaml").read_bytes() == (CODEX / "compose.yaml").read_bytes()
    for script in ("lab.py", "commit_run.py"):
        parity["scripts/" + script] = (ROOT / "scripts" / script).read_bytes() == (CODEX / "scripts" / script).read_bytes()
    prompts = {}
    for arm in (1, 2, 3):
        ours, theirs = sha(lab.prompt(arm)), sha(codex_lab.prompt(arm))
        prompts[str(arm)] = dict(claude_sha256=ours, codex_sha256=theirs, identical=ours == theirs)
    expectations = {"1": True, "2": True, "3": False}   # arm 3 prompt differs by design (native hooks, Fable PM, Opus workers)
    prompt_ok = all(prompts[a]["identical"] == expectations[a] for a in prompts)
    ledgers = sum(1 for arm in (1, 2, 3) for core in range(1, 11)
                  if (ROOT / "docs/experiments" / str(arm) / f"core{core}" / "run.json").is_file())
    parity["experiment_ledgers"] = ledgers == 30
    codex_schedule = json.loads((CODEX / "docs/results/schedule.json").read_text())
    claude_schedule = json.loads((ROOT / "docs/results/schedule.json").read_text())
    parity["schedule"] = [dict(r, run_id=None) for r in codex_schedule] == [dict(r, run_id=None) for r in claude_schedule] \
        and all(r["run_id"] == f"claude-arm{r['arm']}-core{r['core']:02d}" for r in claude_schedule)
    checked = ("template", "prompts", "harness-source", "compose.yaml", "scripts/lab.py", "scripts/commit_run.py", "experiment_ledgers", "schedule")
    passed = starters_ok and all(parity.get(k) is True for k in checked) and prompt_ok
    codex_evidence = CODEX / "docs/results/setup/verification.json"
    fixture = dict(fixture_verified_here=False,
                   codex_evidence_path=str(codex_evidence.relative_to(ROOT.parents[1])) if codex_evidence.exists() else None,
                   codex_evidence_sha256=hashlib.sha256(codex_evidence.read_bytes()).hexdigest() if codex_evidence.exists() else None,
                   basis="compose.yaml byte-identical to codex/compose.yaml" if parity.get("compose.yaml") else "compose differs; fixture NOT covered")
    (evidence / "verification.json").write_text(json.dumps(dict(
        scope="SETUP_ONLY", timestamp_utc=datetime.now(timezone.utc).isoformat(),
        starter_copies=30, all_starters_identical=starters_ok, shared_input_parity=parity,
        prompt_parity=prompts, prompt_parity_expected=expectations, docker_fixture=fixture, passed=passed),
        indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"setup_passed": passed, "parity": parity, "prompt_parity": prompts}, ensure_ascii=False, indent=2))
    return 0 if passed else 1

if __name__ == "__main__":
    sys.exit(main())
