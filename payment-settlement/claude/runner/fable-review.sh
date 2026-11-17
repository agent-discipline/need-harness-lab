#!/bin/bash
# Second reviewer for arm 3 (protocol payment-settlement-v3-claude): takes the place of the `codex exec`
# call in the harness review playbook. The experiment uses Claude only, so the independent reviewer is a
# fresh Claude session on the reviewer model instead of Codex.
#
# Usage (from the read-only review mirror):  fable-review < prompt.md > review.md
#   - prompt on stdin, final review text on stdout, exit code of the reviewer call; no arguments.
#   - this is only the PM-side client: it sends the current directory and the prompt to the reviewer helper over a
#     local socket and returns its answer. The helper (fable_review.py serve) runs under its own user account, holds
#     the token (the PM's user cannot read it) and runs one fixed command per call: a fresh session on the reviewer
#     model, effort high, read-only tools (Read, Grep, Glob), its own CLAUDE_CONFIG_DIR, every attempt logged with its
#     pinned session id before the CLI starts. Exit 96 = empty/error review, 97 = attempt not loggable, 94 = request
#     refused, 95 = helper unreachable, 98 = no reviewer in this run (arms 1/2).
set -u
if [ "$#" -ne 0 ]; then
  echo "[fable-review] takes no arguments; pass the review prompt on stdin" >&2
  exit 2
fi
exec python3 -I "${FABLE_REVIEW_LIB:-/usr/local/lib/fable-review}/fable_review.py" request
