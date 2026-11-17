#!/usr/bin/env python3
"""Fail closed until the measured Claude run has verifiable prerequisites."""
import argparse
import json
from pathlib import Path
import sys

REQUIRED = ("actual_model", "baseline_sha", "contract_sha256", "image_digests")
FLAGS = ("isolation_verified", "usage_collector_verified", "harness_adapter_verified",
         "acceptance_suite_verified", "pilot_passed", "oauth_token_source_verified",
         "role_models_verified", "cache_observation_verified")
AUTH = ("subscription-oauth-token",)
# Agreed protocol v3-claude (2026-11-17): a ledger or readiness from another protocol or with other limits never passes.
PROTOCOL = "payment-settlement-v3-claude"
LIMITS = {"token_limit": 20000000, "time_limit_seconds": 28800, "feedback_limit": 30}

def blockers(manifest):
    failures = [f"{key}: missing/unverified" for key in REQUIRED if not manifest.get(key)]
    if manifest.get("protocol") != PROTOCOL:
        failures.append(f"protocol must be {PROTOCOL}")
    failures += [f"{key} must be {value}" for key, value in LIMITS.items() if manifest.get(key) != value]
    if manifest.get("arm") not in (1, 2, 3) or isinstance(manifest.get("arm"), bool):
        failures.append("arm must be the integer 1, 2 or 3")
    if manifest.get("cache_policy") != "observed-cache":
        failures.append("claude runner supports only observed-cache")
    # Verification flags must be the literal boolean true; strings such as "pending" never pass.
    failures += [f"{key}: must be boolean true (got {manifest.get(key)!r})" for key in FLAGS if manifest.get(key) is not True]
    if manifest.get("auth") not in AUTH:
        failures.append("auth must be one of " + ", ".join(AUTH))
    if manifest.get("reasoning_effort") != "high":
        failures.append("reasoning_effort must be high")
    if manifest.get("provider") != "claude-code":
        failures.append("provider must be claude-code for this folder")
    if manifest.get("arm") == 3 and manifest.get("hook_enforcement_verified") is not True:
        failures.append("arm 3 requires verified native hook enforcement inside the container")
    if manifest.get("arm") == 3 and not manifest.get("requested_worker_model"):
        failures.append("arm 3 requires an explicit worker model")
    # arm 3 consults a user proxy; its model must be frozen like every other input (no default anywhere)
    if manifest.get("arm") == 3 and not manifest.get("user_proxy_model"):
        failures.append("arm 3 requires the frozen user_proxy_model")
    policy = manifest.get("cache_policy")
    if policy not in ("strict-zero-cache", "observed-cache"):
        failures.append("cache policy must be explicit")
    if policy == "strict-zero-cache" and manifest.get("cache_control_verified") is not True:
        failures.append("provider cache read/write disablement is not verified")
    if manifest.get("billed_cost") is not None:
        failures.append("subscription runs cannot claim a billed cost")
    return failures

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    failed = blockers(json.loads(args.manifest.read_text()))
    print(json.dumps({"ready": not failed, "blockers": failed}, ensure_ascii=False, indent=2))
    return 2 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
