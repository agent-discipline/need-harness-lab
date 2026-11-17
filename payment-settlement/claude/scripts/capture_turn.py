#!/usr/bin/env python3
"""Archive an actual Claude runner turn and its evaluator-written analysis into the run's records."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "measurement"))
from normalize import normalize
from summarize import summarize


def main_text(stdout):
    """Assistant text of the pinned session only (subagent messages carry a parent_tool_use_id)."""
    texts = []
    for line in stdout.read_bytes().splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant" or event.get("parent_tool_use_id"):
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
    return "\n\n".join(texts)


def capture(run_id, turn, analysis, status, runs=None):
    source = Path(runs or ROOT / ".runs") / run_id
    manifest = json.loads((source / "manifest.json").read_text())
    records = ROOT / "docs/experiments" / str(manifest["arm"]) / f"core{manifest['core']}"
    raw = source / "turns" / f"{turn:04d}"
    result = json.loads((raw / "result.json").read_text())
    if result.get("status") not in ("ok", "failed") or result.get("session_id") != manifest["session_id"]:
        raise ValueError("turn is not finalized by the runner or belongs to another session")
    destination = records / "turns" / f"{turn:04d}"
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("prompt.txt", "result.json", "invocation.json", "stderr.txt"):
        shutil.copy2(raw / name, destination / name)
    # Full transcripts stay in evaluator-owned .runs; the record keeps the pinned session's visible text.
    (destination / "response.txt").write_text(main_text(raw / "stdout.jsonl"))
    shutil.copy2(analysis, destination / "analysis.md")
    seen = set()
    for prior in (records / "turns").glob("*/usage.jsonl"):
        for line in prior.read_text().splitlines():
            seen.add(json.loads(line)["event_id"])
    # The runner already reduced transcripts to one event per requestId with its role (pm/implementer/worker/reviewer).
    labels = {"worker": manifest.get("requested_worker_model"), "reviewer": manifest.get("requested_reviewer_model")}
    rows = []
    for line in (raw / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        role = event["role"]
        rows.append(normalize(event, "anthropic", run_id, manifest["session_id"] + "/" + role, role,
                              labels.get(role) or manifest["requested_model"]))
    if not rows and result.get("event_count"):
        raise ValueError("turn reported events but none are new; inspect the request ledger")
    (destination / "usage.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    all_rows = [json.loads(line) for file in sorted((records / "turns").glob("*/usage.jsonl"))
                for line in file.read_text().splitlines()]
    complete = all(json.loads(p.read_text()).get("usage_complete") is True
                   for p in sorted((records / "turns").glob("*/result.json")))
    if all_rows:
        summary = summarize(all_rows)
        summary["scope"] = "unique_requests_in_collected_transcripts"
        summary["usage_complete"] = complete      # false if any captured turn left usage unaccounted
        (records / "usage-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    spec = importlib.util.spec_from_file_location("record", ROOT.parent / "recording/record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    recorder.record_turn(records, destination / "prompt.txt", destination / "analysis.md",
                         "pm" if manifest["arm"] == 3 else "implementer", manifest["session_id"], status,
                         destination / "response.txt", [destination / "result.json", destination / "usage.jsonl"])
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--turn", required=True, type=int)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--status", default="ITERATING")
    parser.add_argument("--runs", type=Path, help="runner .runs directory (default payment-settlement/claude/.runs)")
    args = parser.parse_args()
    if "/" in args.run_id or args.run_id in (".", "..") or args.turn < 1:
        parser.error("invalid run or turn")
    capture(args.run_id, args.turn, args.analysis, args.status, args.runs)
