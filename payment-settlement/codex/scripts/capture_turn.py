#!/usr/bin/env python3
"""Archive an actual completed CLI turn and its evaluator-written analysis."""
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


def capture(run_id, turn, analysis, status):
    source = ROOT / ".runs" / run_id
    manifest = json.loads((source / "manifest.json").read_text())
    records = ROOT / "docs/experiments" / str(manifest["arm"]) / f"core{manifest['core']}"
    raw = source / "turns" / f"{turn:04d}"
    result = json.loads((raw / "result.json").read_text())
    if not result.get("thread_id"):
        raise ValueError("missing actual thread identity")
    destination = records / "turns" / f"{turn:04d}"
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("prompt.txt", "result.json", "invocation.json", "stderr.txt"):
        shutil.copy2(raw / name, destination / name)
    # Full tool/session logs stay in evaluator-owned .runs. Preserve final messages here.
    messages = []
    for line in (raw / "stdout.jsonl").read_text().splitlines():
        event = json.loads(line)
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item.get("text", ""))
    (destination / "response.txt").write_text("\n\n".join(messages))
    shutil.copy2(analysis, destination / "analysis.md")
    rows = []
    seen = set()
    for prior in (records / "turns").glob("*/usage.jsonl"):
        for line in prior.read_text().splitlines():
            seen.add(json.loads(line)["event_id"])
    # Canonical provider response records include PM and workers; CLI totals do not.
    for session in sorted((raw / "sessions").rglob("*.jsonl")):
        for line in session.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") != "token_usage_record":
                continue
            payload = event["payload"]
            identity = payload["response_id"]
            if identity in seen:
                continue
            seen.add(identity)
            thread = payload["thread_id"]
            role = ("pm" if manifest["arm"] == 3 else "implementer") if thread == result["thread_id"] else "worker"
            row = normalize({"type": "turn.completed", "event_id": identity,
                             "usage_semantics": "delta", "usage": payload["usage"]},
                            "codex", run_id, thread, role, manifest["requested_model"])
            rows.append(row)
    if not rows and result.get("turn_completed_count"):
        raise ValueError("completed turn has no unique response usage; inspect session export")
    (destination / "usage.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    all_rows = [json.loads(line) for file in sorted((records / "turns").glob("*/usage.jsonl"))
                for line in file.read_text().splitlines()]
    if all_rows:
        summary = summarize(all_rows)
        summary["scope"] = "unique_provider_responses_in_collected_sessions"
        (records / "usage-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    spec = importlib.util.spec_from_file_location("record", ROOT.parent / "recording/record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    recorder.record_turn(records, destination / "prompt.txt", destination / "analysis.md",
                         "pm" if manifest["arm"] == 3 else "implementer", result["thread_id"], status,
                         destination / "response.txt", [destination / "result.json", destination / "usage.jsonl"])
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--turn", required=True, type=int)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--status", default="ITERATING")
    args = parser.parse_args()
    if "/" in args.run_id or args.run_id in (".", "..") or args.turn < 1:
        parser.error("invalid run or turn")
    capture(args.run_id, args.turn, args.analysis, args.status)
