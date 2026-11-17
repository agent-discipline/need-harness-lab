#!/usr/bin/env python3
"""Copy the evidence files of a finished smoke run from .runs/<run> (not tracked) into docs/results/setup/v3/ (tracked),
so that a freeze attestation (freeze_readiness.py --attest) can name and hash them.

Exported: the run's manifest.json (init self-test report) and, per turn, invocation.json (hook self-test before the
call), result.json and stdout.jsonl (the CLI's own stream). Only turns the runner finalised are accepted (result.json
with status ok/failed: written after the run-wide secret scan) and no turn may carry SECRET-LEAK.txt. Every file is
checked once more against the token of --env-file before anything is written; one hit refuses the whole export.
Existing files are never overwritten. No docker, no model call.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]           # payment-settlement/claude
sys.path.insert(0, str(ROOT / 'runner'))
import launch

TURN_FILES = ('invocation.json', 'result.json', 'stdout.jsonl')


def evidence_files(run):
    """[(source, exported name)] of one run; ValueError when the run is not a finished, leak-free run."""
    run = Path(run)
    if not (run / 'manifest.json').is_file():
        raise ValueError('%s has no manifest.json' % run)
    turns = sorted(p for p in (run / 'turns').iterdir() if p.is_dir()) if (run / 'turns').is_dir() else []
    if not turns:
        raise ValueError('%s has no turn' % run)
    files = [(run / 'manifest.json', '%s-manifest.json' % run.name)]
    for turn in turns:
        if (turn / 'SECRET-LEAK.txt').exists():
            raise ValueError('%s recorded a credential leak; nothing is exported' % turn)
        try:
            status = json.loads((turn / 'result.json').read_text()).get('status')
        except (OSError, ValueError):
            status = None
        if status not in ('ok', 'failed'):
            raise ValueError('%s is not finalised by the runner (result.json status %r)' % (turn, status))
        files += [(turn / name, '%s-turn-%s-%s' % (run.name, turn.name, name)) for name in TURN_FILES]
    return files


def export(runs, run_ids, dest, secret):
    planned = [item for run_id in run_ids for item in evidence_files(Path(runs) / launch.valid_run(run_id))]
    for source, name in planned:
        if not source.is_file():
            raise ValueError('missing evidence file %s' % source)
        if (Path(dest) / name).exists():
            raise ValueError('%s already exists; evidence is never overwritten' % (Path(dest) / name))
        if launch.contains_any(source, [secret.encode()]):
            raise ValueError('%s contains the credential; nothing is exported' % source)
    Path(dest).mkdir(parents=True, exist_ok=True)
    written = {}
    for source, name in planned:
        data = source.read_bytes()
        with (Path(dest) / name).open('xb') as stream:
            stream.write(data)
        written[name] = hashlib.sha256(data).hexdigest()
    return written


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', action='append', required=True, help='finished smoke run under --runs (repeat)')
    p.add_argument('--env-file', type=Path, required=True, help='the token file the runs used (only to check the copies)')
    p.add_argument('--runs', type=Path, default=ROOT / '.runs')
    p.add_argument('--dest', type=Path, default=ROOT / 'docs/results/setup/v3')
    a = p.parse_args()
    try:
        written = export(a.runs, a.run_id, a.dest, launch.read_token(a.env_file))
    except (ValueError, OSError) as exc:
        p.exit(1, str(exc) + '\n')
    print(json.dumps(written, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
