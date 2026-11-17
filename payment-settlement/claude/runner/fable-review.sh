#!/bin/bash
# Second reviewer for arm 3 (protocol payment-settlement-v3-claude): takes the place of the `codex exec`
# call in the harness review playbook. The experiment uses Claude only, so the independent reviewer is a
# fresh Claude session on the reviewer model instead of Codex.
#
# Usage (from the read-only review mirror):  fable-review < prompt.md > review.md
#   - prompt on stdin, final review text on stdout, exit code of the reviewer CLI.
#   - fresh session per call, effort high, read-only tools (Read, Grep, Glob) in the current directory.
#   - own CLAUDE_CONFIG_DIR: the reviewer does not load the PM's harness (CLAUDE.md, core.md, hooks) and its
#     transcripts stay apart from the PM's, so the runner can attribute its usage to the reviewer role.
#   - every attempt is logged with its pinned session id BEFORE the CLI starts, so an interrupted call
#     still leaves evidence; logging failure refuses the call (usage would be unobservable).
# Enabled only where the runner sets LAB_REVIEWER_MODEL and LAB_REVIEWER_CONFIG_DIR (arm 3).
set -u
MODEL="${LAB_REVIEWER_MODEL:-}"
DIR="${LAB_REVIEWER_CONFIG_DIR:-}"
if [ -z "$MODEL" ] || [ -z "$DIR" ]; then
  echo "[fable-review] no reviewer is configured in this run; refusing" >&2
  exit 98
fi
if [ "$#" -ne 0 ]; then
  echo "[fable-review] takes no arguments; pass the review prompt on stdin" >&2
  exit 2
fi
LOG="$DIR/reviewer-calls.log"
SID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null)"
CALL="$(date -u +%FT%TZ)-$$"
if [ -z "$SID" ] || ! mkdir -p "$DIR/raw" 2>/dev/null \
   || ! printf '%s started session=%s model=%s\n' "$CALL" "$SID" "$MODEL" >> "$LOG" 2>/dev/null; then
  echo "[fable-review] cannot record the attempt in $LOG; refusing to launch (usage would be unobservable)" >&2
  exit 97
fi
RAW="$DIR/raw/$SID.json"
CLAUDE_CONFIG_DIR="$DIR" claude -p --model "$MODEL" --effort high --session-id "$SID" \
  --tools Read,Grep,Glob --output-format json > "$RAW"
rc=$?
# final text for the caller. An unreadable result, an error result or an empty review is a failure (exit 96), never a
# silent success: the raw output goes to stderr so the PM sees why.
TEXT="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(3) if d.get("is_error") else print(d.get("result") or "")' "$RAW" 2>/dev/null)"
prc=$?
if [ "$rc" = 0 ] && { [ "$prc" != 0 ] || [ -z "${TEXT//[[:space:]]/}" ]; }; then
  echo "[fable-review] the reviewer returned no review text (empty, error or unreadable result); raw output follows" >&2
  cat "$RAW" >&2
  rc=96
fi
printf '%s exit=%s\n' "$CALL" "$rc" >> "$LOG" 2>/dev/null
[ "$prc" = 0 ] && printf '%s\n' "$TEXT" || cat "$RAW"
exit "$rc"
