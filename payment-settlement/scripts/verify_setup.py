#!/usr/bin/env python3
"""Verify shared preparation; these measurements are never experiment results."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lab

def main():
    evidence = ROOT / "results/setup"
    evidence.mkdir(parents=True, exist_ok=True)
    compose = ["docker", "compose", "-p", "payment-lab-smoke", "-f", str(ROOT / "compose.yaml")]
    for core in range(1, 11):
        lab.verify(core)
    checks = [
        ("seed", compose + ["exec", "-T", "db", "psql", "-U", "lab", "-d", "payment", "-At", "-c",
          "SELECT count(*), min(id), max(id), sum(balance_minor) FROM users; SELECT count(*), min(fee_bps), max(fee_bps), sum(settlement_balance_minor) FROM agencies;"], 0),
        ("k6-smoke", compose + ["--profile", "test", "run", "--rm", "k6", "run", "/tests/smoke.js"], 0),
        ("k6-negative-skeleton", compose + ["--profile", "test", "run", "--rm", "k6", "run", "/tests/payments.js"], 99),
    ]
    records = []
    for name, command, expected in checks:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (evidence / f"{name}.txt").write_text(result.stdout)
        valid = result.returncode == expected
        if name == "seed":
            valid = valid and result.stdout.splitlines() == ["1000000|1|1000000|1000000000000", "1000|100|100|0"]
        records.append(dict(check=name, command=command, exit_code=result.returncode,
                            expected_exit_code=expected, passed=valid))
    services = json.loads(subprocess.check_output(["docker", "inspect", "payment-lab-smoke-app-1",
                           "payment-lab-smoke-db-1", "payment-lab-smoke-bank-1"], text=True))
    resource_limits = [{"name": s["Name"], "image_id": s["Image"], "nano_cpus": s["HostConfig"]["NanoCpus"],
                        "memory_bytes": s["HostConfig"]["Memory"], "health": s["State"].get("Health", {}).get("Status")}
                       for s in services]
    passed = all(row["passed"] for row in records)
    (evidence / "verification.json").write_text(json.dumps(dict(
        scope="SETUP_ONLY", timestamp_utc=datetime.now(timezone.utc).isoformat(),
        starter_copies=30, all_starters_identical=True, passed=passed,
        checks=records, containers=resource_limits), indent=2) + "\n")
    print(json.dumps({"setup_passed": passed, "checks": records}, ensure_ascii=False, indent=2))
    return 0 if passed else 1

if __name__ == "__main__":
    sys.exit(main())
