#!/usr/bin/env python3
"""Runs the shared HTTP judge (evaluation/acceptance.py Evaluator, unchanged) with the pilot-04 adapter.
acceptance.py's own __main__ only knows the Codex pilot adapter, so this driver wires the same Evaluator to ours."""
import argparse, json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[5] / 'evaluation'))
sys.path.insert(0, str(HERE))
from acceptance import Evaluator          # shared judge, not modified
from adapter import Adapter

p = argparse.ArgumentParser()
p.add_argument('--base-url', required=True)
p.add_argument('--namespace', required=True)
p.add_argument('--project', required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--http-only', action='store_true')
a = p.parse_args()
if a.output.exists():
    raise SystemExit('refusing to overwrite evidence: %s' % a.output)
report = Evaluator(a.base_url, a.namespace, None if a.http_only else Adapter(a.project)).run()
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({k: v['status'] for k, v in report['cases'].items()}))
raise SystemExit(1 if any(v['status'] == 'FAIL' for v in report['cases'].values()) else 2)
