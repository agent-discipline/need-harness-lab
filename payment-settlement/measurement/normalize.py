#!/usr/bin/env python3
"""Normalize per-turn/per-request usage; never derive usage from text length."""
import argparse
import hashlib
import json
from pathlib import Path


def integer(value, name, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}: nonnegative integer required")
    return value


def normalize(event, provider, run_id, session_id, role, model):
    # An adapter must explicitly extract deltas before passing cumulative data.
    if event.get("cumulative") or event.get("usage_semantics") in ("cumulative", "unknown"):
        raise ValueError("cumulative/unknown usage is not accepted; supply request deltas")
    if provider == "codex":
        if event.get("type") != "turn.completed":
            if event.get("type") == "token_count":
                raise ValueError("token_count snapshots require a separate delta adapter")
            return None
        if event.get("usage_semantics") != "delta":
            raise ValueError("Codex CLI counters are cumulative; use the runner delta collector")
        usage = event.get("usage")
        source_id = event.get("event_id") or event.get("id")
        actual_model = event.get("model")
    elif provider == "openai":
        if event.get("type") and event["type"] != "response.completed":
            return None
        response = event.get("response", event)
        usage = response.get("usage")
        source_id = response.get("id") or event.get("event_id")
        actual_model = response.get("model")
    else:
        if event.get("type") not in (None, "message"):
            if event.get("type") in ("message_start", "message_delta"):
                raise ValueError("Anthropic streaming fragments must be assembled into one final message")
            return None
        usage = event.get("usage")
        source_id = event.get("id") or event.get("event_id")
        actual_model = event.get("model")
    if not isinstance(usage, dict):
        raise ValueError("final event has no usage object")
    if not source_id:
        raise ValueError("stable event_id required (Codex collector must assign turn sequence IDs)")
    inp = integer(usage.get("input_tokens"), "input_tokens")
    out = integer(usage.get("output_tokens"), "output_tokens")
    reasoning = None
    write = None
    if provider == "codex":
        cached = integer(usage.get("cached_input_tokens"), "cached_input_tokens", True)
        write = integer(usage.get("cache_write_input_tokens"), "cache_write_input_tokens", True)
        reasoning = integer(usage.get("reasoning_output_tokens"), "reasoning_output_tokens", True)
    elif provider == "openai":
        details = usage.get("input_tokens_details") or {}
        cached = integer(details.get("cached_tokens"), "cached_tokens", True)
        write = integer(details.get("cache_write_tokens"), "cache_write_tokens", True)
        reasoning = integer((usage.get("output_tokens_details") or {}).get("reasoning_tokens"), "reasoning_tokens", True)
    else:
        cached = integer(usage.get("cache_read_input_tokens"), "cache_read_input_tokens", True)
        write = integer(usage.get("cache_creation_input_tokens"), "cache_creation_input_tokens", True)
        if cached is None or write is None:
            raise ValueError("Anthropic total input requires explicit cache read and creation counts")
        inp += cached + write
    if cached is not None and cached > inp:
        raise ValueError("cached input exceeds total input")
    if reasoning is not None and reasoning > out:
        raise ValueError("reasoning exceeds output")
    cost = event.get("provider_reported_cost_usd")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (float, int)) or cost < 0):
        raise ValueError("provider_reported_cost_usd must be nonnegative")
    return dict(schema_version=1, provider=provider, run_id=run_id, session_id=session_id,
                event_id=str(source_id), role=role, model_label=model, actual_model=actual_model,
                usage_scope="own_request", input_tokens=inp, cached_input_tokens=cached,
                cache_write_input_tokens=write, output_tokens=out, reasoning_tokens=reasoning,
                total_tokens=inp + out, provider_reported_cost_usd=cost,
                source_sha256=hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest())


def unique(records):
    seen = {}
    for record in records:
        key = tuple(record[k] for k in ("provider", "run_id", "session_id", "event_id"))
        if key in seen:
            if seen[key] != record:
                raise ValueError(f"conflicting duplicate: {key}")
            continue
        seen[key] = record
        yield record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", required=True, choices=["codex", "openai", "anthropic"])
    for key in ("run-id", "session-id", "model", "input", "output"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--role", required=True, choices=["implementer", "orchestrator", "worker", "reviewer", "prompt-author", "pm", "user-proxy", "test-designer"])
    args = p.parse_args()
    try:
        records = []
        for line in Path(args.input).read_text().splitlines():
            if line.strip():
                record = normalize(json.loads(line), args.provider, args.run_id, args.session_id, args.role, args.model)
                if record:
                    records.append(record)
        result = list(unique(records))
        if not result:
            raise ValueError("no final usage events found")
        Path(args.output).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in result))
    except (ValueError, KeyError, OSError) as exc:
        p.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
