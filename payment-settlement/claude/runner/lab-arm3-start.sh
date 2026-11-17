#!/bin/bash
# Container start of arm 3 (protocol payment-settlement-v3-claude). Runs as root with only SETUID, SETGID and
# DAC_READ_SEARCH (docker create --user 0:0 --cap-drop=ALL --cap-add ..., no-new-privileges); arms 1/2 never use it.
#   1. the reviewer helper (fable_review.py serve) starts under the reviewer account. The token is only in ITS
#      environment (another uid cannot read /proc/<pid>/environ). It gets DAC_READ_SEARCH as its only capability so the
#      reviewer can read the review mirror the PM prepares (mktemp -d is 0700) but cannot write any of the PM's files.
#      Its stdin is closed and its output goes to stderr: the container stdout stays the PM's stream-json.
#   2. the PM (the command given as arguments) runs as node (uid 1000, no capabilities) like arms 1/2. Its token comes
#      through an inherited pipe (CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3, read once by the CLI), never through its
#      environment, so neither the PM nor its Bash children can read it from an environment or a file.
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
( CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" exec setsid setpriv --reuid="$REVIEWER" --regid="$REVIEWER" --clear-groups \
    --inh-caps=-all,+dac_read_search --ambient-caps=+dac_read_search \
    python3 -I "$LIB" serve --socket "$SOCK" --model "$MODEL" --config-dir "$DIR" --home /home/fablereview \
    --root /tmp --root /workspace --peer-uid 1000 </dev/null >&2 2>&2 & )
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
exec setpriv --reuid=node --regid=node --init-groups --inh-caps=-all "$@"
