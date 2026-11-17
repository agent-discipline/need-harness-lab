#!/bin/bash
# Container start of arm 3 (protocol payment-settlement-v3-claude). Runs as root with only SETUID, SETGID, SETPCAP and
# DAC_READ_SEARCH (docker create --user 0:0 --cap-drop=ALL --cap-add ..., no-new-privileges); arms 1/2 never use it.
#   1. the reviewer helper (fable_review.py serve) starts under the reviewer account. It reads the token once from an
#      inherited pipe (--token-fd 4) and keeps it in memory: the token is in no environment and no file (its reviewer
#      CLI gets it through a pipe too). Its only capabilities (also its whole bounding set) are DAC_READ_SEARCH, to copy
#      the review mirror the PM prepares (mktemp -d is 0700) into its snapshot — it cannot write any of the PM's files —
#      and SETPCAP, to start each reviewer CLI with no capability at all. Its stdin is closed and its output goes to
#      stderr: the container stdout stays the PM's stream-json.
#   2. the PM (the command given as arguments) runs as node (uid 1000, no capabilities, empty bounding set) like arms
#      1/2. Its token comes through an inherited pipe (CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3, read once by the
#      CLI), never through its environment, so neither the PM nor its Bash children can read it from an environment
#      or a file.
# The reviewer model and config dir come from the runner (docker create --env) and are fixed here, before the PM starts.
set -u
say() { echo "[lab-arm3-start] $*" >&2; }
[ "$(id -u)" = 0 ] || { say "must start as root (arm 3 container)"; exit 90; }
[ "$#" -ge 1 ] || { say "no PM command given"; exit 90; }
TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
MODEL="${LAB_REVIEWER_MODEL:-}"
DIR="${LAB_REVIEWER_CONFIG_DIR:-}"
unset CLAUDE_CODE_OAUTH_TOKEN LAB_REVIEWER_MODEL LAB_REVIEWER_CONFIG_DIR
if [ -z "$TOKEN" ] || [ -z "$MODEL" ] || [ -z "$DIR" ]; then
  say "token, reviewer model and reviewer config dir are required; refusing"
  exit 90
fi
REVIEWER=fablereview
SOCK=/run/fable-review/socket
LIB=/usr/local/lib/fable-review/fable_review.py
# subshell + & : the helper is reparented to the init process, not left as a child of the PM
( exec 4< <(printf '%s\n' "$TOKEN"); exec setsid setpriv --reuid="$REVIEWER" --regid="$REVIEWER" --clear-groups \
    --bounding-set=-all,+dac_read_search,+setpcap --inh-caps=-all,+dac_read_search,+setpcap \
    --ambient-caps=+dac_read_search,+setpcap \
    python3 -I "$LIB" serve --socket "$SOCK" --model "$MODEL" --config-dir "$DIR" --home /home/fablereview \
    --root /tmp --root /workspace --peer-uid 1000 --token-fd 4 </dev/null >&2 2>&2 & )
for _ in $(seq 1 150); do
  [ -S "$SOCK" ] && break
  sleep 0.1
done
if [ ! -S "$SOCK" ]; then
  say "reviewer helper did not become ready; the PM is not started"
  exit 91
fi
say "reviewer helper ready"
exec 3< <(printf '%s\n' "$TOKEN")
unset TOKEN
export CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3
exec setpriv --reuid=node --regid=node --init-groups --bounding-set=-all --inh-caps=-all "$@"
