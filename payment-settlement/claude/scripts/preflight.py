#!/usr/bin/env python3
"""Fail closed until the measured Claude run has verifiable prerequisites."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]      # payment-settlement/claude
sys.path.insert(0, str(ROOT / "runner"))
from launch import USER_PROXY_MODEL            # agreed protocol constant (claude-opus-5-5)

REQUIRED = ("actual_model", "baseline_sha", "contract_sha256", "image_digests")
FLAGS = ("isolation_verified", "usage_collector_verified", "harness_adapter_verified",
         "acceptance_suite_verified", "pilot_passed", "oauth_token_source_verified",
         "role_models_verified", "cache_observation_verified")
AUTH = ("subscription-oauth-token",)
# Agreed protocol v3-claude (2026-11-17): a ledger or readiness from another protocol or with other limits never passes.
PROTOCOL = "payment-settlement-v3-claude"
LIMITS = {"token_limit": 150000000, "time_limit_seconds": 28800, "feedback_limit": 30}
# Two gates in protocol order (freeze -> pilot -> cycles): starting the pilot cannot require the pilot's own result;
# starting a measured cycle requires everything. There is no other stage and no way to waive any other flag.
STAGE_WAIVED = {"pilot": ("pilot_passed",), "cycle": ()}
# Decision 2026-11-19 (requirement-spec "파일럿 게이트 결정"): no further pilot; the measured run of arm 3 in cycle 1 stands
# in for it. While pilot_passed is false the cycle gate lets through that one run (arm, core) and no other; pilot_passed is
# then derived from it (freeze_readiness.pilot_evidence) and opens arms 1 and 2 of cycle 1 and every later cycle.
PILOT_IN_CYCLE = (3, 1)

def waived(manifest, stage):
    """Flags the gate does not require for this merged ledger at this stage."""
    if stage not in STAGE_WAIVED:
        raise ValueError("stage must be pilot or cycle")
    where = (manifest.get("arm"), manifest.get("core"))
    if stage == "cycle" and all(type(v) is int for v in where) and where == PILOT_IN_CYCLE:
        return ("pilot_passed",)
    return STAGE_WAIVED[stage]

def blockers(manifest, stage="cycle"):
    skip = waived(manifest, stage)
    failures = [f"{key}: missing/unverified" for key in REQUIRED if not manifest.get(key)]
    if manifest.get("protocol") != PROTOCOL:
        failures.append(f"protocol must be {PROTOCOL}")
    failures += [f"{key} must be {value}" for key, value in LIMITS.items() if manifest.get(key) != value]
    if manifest.get("arm") not in (1, 2, 3) or isinstance(manifest.get("arm"), bool):
        failures.append("arm must be the integer 1, 2 or 3")
    if manifest.get("cache_policy") != "observed-cache":
        failures.append("claude runner supports only observed-cache")
    # Verification flags must be the literal boolean true; strings such as "pending" never pass.
    failures += [f"{key}: must be boolean true (got {manifest.get(key)!r})" for key in FLAGS
                 if key not in skip and manifest.get(key) is not True]
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
    # arm 3 consults a user proxy; the frozen value must be the agreed model (absolute comparison)
    if manifest.get("arm") == 3 and manifest.get("user_proxy_model") != USER_PROXY_MODEL:
        failures.append(f"arm 3 requires user_proxy_model {USER_PROXY_MODEL}")
    policy = manifest.get("cache_policy")
    if policy not in ("strict-zero-cache", "observed-cache"):
        failures.append("cache policy must be explicit")
    if policy == "strict-zero-cache" and manifest.get("cache_control_verified") is not True:
        failures.append("provider cache read/write disablement is not verified")
    if manifest.get("billed_cost") is not None:
        failures.append("subscription runs cannot claim a billed cost")
    return failures

def merge(run, ready, arm):
    """The gate input of one arm: its run record (run.json from prepare.py: arm, models, provider) with the frozen
    readiness over it; the model the freeze observed for the arm becomes actual_model."""
    merged = dict(run, **{k: v for k, v in ready.items() if k not in ("status", "run_id", "arm", "core", "evidence")})
    merged["actual_model"] = (ready.get("actual_models") or {}).get(str(arm))
    return merged

def readiness_blockers(ready, core, stage, arms=(1, 2, 3), root=None):
    """Blockers of each arm's record of cycle `core` merged with readiness, prefixed with the arm. Used by the pilot gate
    (this script, --readiness) and the cycle gate (start_cycle.gate)."""
    failures = []
    for arm in arms:
        record = Path(root or ROOT) / "docs/experiments" / str(arm) / f"core{core}" / "run.json"
        failures += ["arm %d: %s" % (arm, f) for f in blockers(merge(json.loads(record.read_text()), ready, arm), stage)]
    return failures

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path, nargs="?", help="one ledger already merged with readiness")
    parser.add_argument("--readiness", type=Path, help="instead of a merged ledger: the frozen readiness.json, merged here "
                        "with the run record docs/experiments/<arm>/core<N>/run.json of each --arm")
    parser.add_argument("--core", type=int, choices=range(1, 11), help="with --readiness: the core whose run records are used")
    parser.add_argument("--arm", type=int, choices=(1, 2, 3), action="append",
                        help="with --readiness: arm to check (repeat; default all three)")
    parser.add_argument("--stage", choices=tuple(STAGE_WAIVED), default="cycle",
                        help="pilot: before the pilot run (pilot_passed not yet possible); cycle: before measured cycles "
                        "(pilot_passed not required only for arm 3 of core 1, PILOT_IN_CYCLE)")
    args = parser.parse_args()
    if (args.manifest is None) == (args.readiness is None):
        parser.error("give either a merged ledger or --readiness")
    if args.readiness is not None:
        if args.core is None:
            parser.error("--readiness requires --core")
        failed = readiness_blockers(json.loads(args.readiness.read_text()), args.core, args.stage, tuple(args.arm or (1, 2, 3)))
    else:
        if args.core is not None or args.arm:
            parser.error("--core/--arm apply only with --readiness")
        failed = blockers(json.loads(args.manifest.read_text()), args.stage)
    print(json.dumps({"stage": args.stage, "ready": not failed, "blockers": failed}, ensure_ascii=False, indent=2))
    return 2 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
