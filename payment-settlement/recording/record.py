#!/usr/bin/env python3
"""Append-only experiment transcript journal (Linux, Python stdlib)."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def snapshot(path):
    path = Path(path)
    data = path.read_bytes()
    try:
        content = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(data).decode("ascii")
        encoding = "base64"
    return dict(path=str(path), content=content, encoding=encoding, sha256=digest(data), bytes=len(data))


def verify_snapshot(item):
    data = item["content"].encode() if item["encoding"] == "utf-8" else base64.b64decode(item["content"], validate=True)
    if digest(data) != item["sha256"] or len(data) != item["bytes"]:
        raise ValueError("recorded source content was modified")


def read_events(path):
    if not path.exists():
        return []
    events = []
    previous = None
    for line in path.read_text().splitlines():
        if not line:
            raise ValueError("empty journal line")
        event = json.loads(line)
        recorded_hash = event["event_hash"]
        payload = {k: v for k, v in event.items() if k != "event_hash"}
        if digest(canonical(payload)) != recorded_hash or event["previous_hash"] != previous:
            raise ValueError("journal hash chain was modified")
        if event["sequence"] != len(events):
            raise ValueError("journal sequence is invalid")
        for item in event.get("files", {}).values():
            verify_snapshot(item)
        for item in event.get("evidence", []):
            verify_snapshot(item)
        events.append(event)
        previous = recorded_hash
    if not events or events[0]["kind"] != "init":
        raise ValueError("journal init event missing")
    return events


def fence(text):
    width = max([len(m.group()) for m in re.finditer(r"`+", text)] + [2]) + 1
    marker = "`" * width
    return marker + "text\n" + text + ("" if text.endswith("\n") else "\n") + marker + "\n"


def render(events):
    text = "# 실험 실행 기록\n\n최근 기록 상태: " + events[-1]["status"] + "\n\n"
    if len(events) == 1:
        return text + "실제 프롬프트 및 분석 기록 없음.\n"
    for event in events[1:]:
        n = event["sequence"]
        text += f"## {n}. 프롬프트 전문\n\n"
        text += f"UTC: {event['timestamp_utc']} · 역할: {event['role']} · 세션: {event['session_id']} · 상태: {event['status']}\n\n"
        text += fence(event["files"]["prompt"]["content"]) + "\n"
        if "response" in event["files"]:
            text += f"### {n}. 에이전트 응답 전문\n\n" + fence(event["files"]["response"]["content"]) + "\n"
        text += f"## {n}. 결과물 분석\n\n" + fence(event["files"]["analysis"]["content"]) + "\n"
        for item in event["evidence"]:
            text += "### 증거 파일\n\n" + fence(item["path"]) + "\n"
            text += f"SHA-256: {item['sha256']} · 인코딩: {item['encoding']}\n\n" + fence(item["content"]) + "\n"
    return text


def atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(run_dir):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ".record.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield run_dir


def existing(run_dir):
    events = read_events(run_dir / "events.jsonl")
    transcript = run_dir / "transcript.md"
    if transcript.exists():
        if not events or transcript.read_text() != render(events):
            raise ValueError("transcript modified or stale; preserve and inspect before recovery")
    return events


def append(run_dir, events, payload):
    payload.update(sequence=len(events), timestamp_utc=datetime.now(timezone.utc).isoformat(),
                   previous_hash=events[-1]["event_hash"] if events else None)
    payload["event_hash"] = digest(canonical(payload))
    with (run_dir / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    events.append(payload)
    atomic_write(run_dir / "transcript.md", render(events))
    return payload


def init_run(path):
    """Initialize idempotently; refuses corrupted existing history."""
    with locked(path) as run_dir:
        events = existing(run_dir)
        if events:
            return events[0]
        return append(run_dir, events, dict(kind="init", status="NOT_STARTED", schema_version=1))


def record_turn(run_dir, prompt_file, analysis_file, role, session_id, status, response_file=None, evidence_files=None):
    if not all((role, session_id, status)) or status == "NOT_STARTED":
        raise ValueError("turn role/session/status required; NOT_STARTED is init only")
    files = {"prompt": snapshot(prompt_file), "analysis": snapshot(analysis_file)}
    if response_file:
        files["response"] = snapshot(response_file)
    if any(item["encoding"] != "utf-8" for item in files.values()):
        raise ValueError("prompt/analysis/response must be UTF-8")
    evidence = [snapshot(path) for path in (evidence_files or [])]
    identity = dict(role=role, session_id=session_id, files={k: v["sha256"] for k, v in files.items()})
    submission_id = digest(canonical(identity))
    with locked(run_dir) as folder:
        events = existing(folder)
        if not events:
            raise ValueError("initialize run with init_run / --init first")
        if any(event.get("submission_id") == submission_id for event in events):
            raise ValueError("duplicate submission; no new event appended")
        return append(folder, events, dict(kind="turn", role=role, session_id=session_id, status=status,
                      files=files, evidence=evidence, submission_id=submission_id, schema_version=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--init", action="store_true")
    for name in ("prompt-file", "analysis-file", "response-file", "role", "session-id", "status"):
        parser.add_argument("--" + name)
    parser.add_argument("--evidence-files", nargs="*", default=[])
    args = parser.parse_args()
    try:
        if args.init:
            if any((args.prompt_file, args.analysis_file, args.response_file, args.role, args.session_id, args.status, args.evidence_files)):
                raise ValueError("--init takes only --run-dir")
            result = init_run(args.run_dir)
        else:
            if not all((args.prompt_file, args.analysis_file, args.role, args.session_id, args.status)):
                raise ValueError("prompt-file, analysis-file, role, session-id, status required")
            result = record_turn(args.run_dir, args.prompt_file, args.analysis_file, args.role, args.session_id,
                                 args.status, args.response_file, args.evidence_files)
        print(json.dumps({k: result[k] for k in ("sequence", "event_hash", "status")}))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
