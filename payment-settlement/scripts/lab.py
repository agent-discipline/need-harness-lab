#!/usr/bin/env python3
"""Prepare byte-identical starters; never launch an experimental model."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".gradle", "build", ".git", "__pycache__", "experiment"}


def inventory(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()
            and not (set(p.relative_to(path).parts) & IGNORED)}


def clone(core):
    if core < 1:
        raise ValueError("core must be positive")
    targets = [ROOT / str(arm) / f"core{core}" for arm in (1, 2, 3)]
    if any(p.exists() for p in targets):
        raise ValueError("target exists; refusing to overwrite any arm")
    for dest in targets:
        shutil.copytree(ROOT / "template", dest, ignore=shutil.ignore_patterns(*IGNORED))
    print(f"Created all three core{core} starters")


def verify(core):
    expected = inventory(ROOT / "template")
    if not expected or "gradlew" not in expected:
        raise ValueError("starter or Gradle wrapper is missing")
    for arm in (1, 2, 3):
        actual = inventory(ROOT / str(arm) / f"core{core}")
        if actual != expected:
            changed = sorted(k for k in expected.keys() | actual.keys()
                             if expected.get(k) != actual.get(k))
            raise ValueError(f"arm {arm} differs: {changed}")
    print(f"PASS: three core{core} copies equal template ({len(expected)} files)")


def export(arm, core, dest):
    if dest.exists():
        raise ValueError("export destination exists")
    source = ROOT / str(arm) / f"core{core}"
    if not source.is_dir():
        raise ValueError("source does not exist")
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*IGNORED))
    print(f"Exported arm {arm} core{core} to {dest}; OS isolation is still required")


def prompt(arm):
    common = (ROOT / "prompts/common.md").read_text()
    if arm == 1:
        return common + "\n\n" + (ROOT / "prompts/1-minimal.md").read_text()
    detailed = "\n\n".join((ROOT / p).read_text() for p in
                            ("docs/requirements.md", "docs/resources.md", "prompts/slo.md"))
    extra = "" if arm == 2 else "\n\n" + (ROOT / "prompts/3-harness.md").read_text()
    return common + "\n\n" + detailed + extra


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("clone", "verify"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--core", type=int, default=1)
    cmd = sub.add_parser("export")
    cmd.add_argument("--arm", type=int, choices=(1, 2, 3), required=True)
    cmd.add_argument("--core", type=int, default=1)
    cmd.add_argument("--dest", type=Path, required=True)
    cmd = sub.add_parser("prompt")
    cmd.add_argument("--arm", type=int, choices=(1, 2, 3), required=True)
    sub.add_parser("manifest")
    args = parser.parse_args()
    try:
        if args.command == "clone": clone(args.core)
        elif args.command == "verify": verify(args.core)
        elif args.command == "export": export(args.arm, args.core, args.dest)
        elif args.command == "prompt": print(prompt(args.arm))
        else:
            paths = ["template", "docs", "prompts", "infra", "evaluation", "harness-source"]
            manifest = {p: inventory(ROOT / p) for p in paths}
            manifest["compose"] = hashlib.sha256((ROOT / "compose.yaml").read_bytes()).hexdigest()
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
