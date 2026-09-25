#!/usr/bin/env python3
"""Fail closed until the measured run has verifiable prerequisites."""
import argparse
import json
from pathlib import Path
import sys

REQUIRED = ("configured_model", "model_configuration_verified", "baseline_sha", "contract_sha256", "image_digests",
            "isolation_verified", "usage_collector_verified", "harness_adapter_verified",
            "acceptance_suite_verified", "pilot_validation_completed")

def blockers(manifest):
    failures = [f"{key}: missing/unverified" for key in REQUIRED if not manifest.get(key)]
    if manifest.get("configured_model") != manifest.get("selected_model"):
        failures.append("configured model does not match selected model")
    if manifest.get("actual_model") is None and not manifest.get("backend_model_identity_unavailable"):
        failures.append("backend model identity limitation must be recorded")
    if manifest.get("reasoning_effort") != "high":
        failures.append("reasoning_effort must be high")
    policy = manifest.get("cache_policy")
    if policy not in ("strict-zero-cache", "observed-cache"):
        failures.append("cache policy must be explicit")
    if policy == "strict-zero-cache" and manifest.get("cache_control_verified") is not True:
        failures.append("provider cache read/write disablement is not verified")
    if policy == "observed-cache" and manifest.get("cache_observation_verified") is not True:
        failures.append("cache usage observation is not verified")
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
