#!/bin/bash
# Experiment wrapper (payment-settlement Claude arm 3, decision D2 2026-09-23):
# drop `--ephemeral` so codex review sessions persist under ~/.codex/sessions and their usage stays
# observable, and log every invocation (time, exit code, argument count — never arguments or stdin)
# so an attempted review that produced no session file is still visible to the runner.
args=()
stripped=0
for a in "$@"; do
  if [ "$a" = "--ephemeral" ]; then stripped=1; else args+=("$a"); fi
done
[ "$stripped" = 1 ] && echo "[codex-wrapper] --ephemeral removed for usage observability (experiment D2)" >&2
LOG="${HOME:-/home/node}/.codex/wrapper-calls.log"
mkdir -p "${HOME:-/home/node}/.codex" 2>/dev/null
CALL="$(date -u +%FT%TZ)-$$"
# the attempt is durable BEFORE codex launches, so an interrupted call still leaves evidence
if ! printf '%s started argc=%s stripped=%s\n' "$CALL" "$#" "$stripped" >> "$LOG"; then
  echo "[codex-wrapper] cannot record the attempt in $LOG — refusing to launch codex (usage would be unobservable)" >&2
  exit 97
fi
/usr/local/bin/codex-real "${args[@]}"
rc=$?
printf '%s exit=%s\n' "$CALL" "$rc" >> "$LOG" 2>/dev/null
exit $rc
