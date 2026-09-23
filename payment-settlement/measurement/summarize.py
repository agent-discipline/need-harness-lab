#!/usr/bin/env python3
"""Aggregate own-request records, deduplicating events across input files."""
import argparse
import json
from pathlib import Path
from normalize import unique


def summarize(records):
    rows = list(unique(records))
    if not rows:
        raise ValueError("no records")
    if any(r.get("usage_scope") != "own_request" for r in rows):
        raise ValueError("only own_request usage accepted; parent totals including children are forbidden")
    if len({r["run_id"] for r in rows}) != 1:
        raise ValueError("summarize one run at a time")
    def total(key):
        return None if any(r.get(key) is None for r in rows) else sum(r[key] for r in rows)
    return dict(run_id=rows[0]["run_id"], event_count=len(rows),
                input_tokens=total("input_tokens"), output_tokens=total("output_tokens"),
                total_tokens=sum(r["input_tokens"] + r["output_tokens"] for r in rows),
                cached_input_tokens=total("cached_input_tokens"),
                cache_write_input_tokens=total("cache_write_input_tokens"),
                reasoning_tokens=total("reasoning_tokens"),
                actual_cost_usd=total("provider_reported_cost_usd"),
                cost_source="provider_reported" if total("provider_reported_cost_usd") is not None else None,
                model_labels=sorted({r["model_label"] for r in rows}),
                actual_models=sorted({r["actual_model"] for r in rows if r.get("actual_model")}),
                actual_model_missing_events=sum(r.get("actual_model") is None for r in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        rows = [json.loads(line) for path in args.inputs for line in Path(path).read_text().splitlines() if line.strip()]
        result = json.dumps(summarize(rows), ensure_ascii=False, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(result)
        else:
            print(result, end="")
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
