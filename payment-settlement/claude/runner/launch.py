#!/usr/bin/env python3
"""Prepare an isolated Claude Code run; an explicit turn executes a paid subscription call."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]           # payment-settlement/claude
REPO = ROOT.parents[1]                                # need-haness-lab (env-file must live outside)
SKIP = {'.git', '.gradle', 'build', '.runs', '.codex', '.claude', 'node_modules', 'logs', 'artifacts', 'experiment', '__pycache__'}
CLI_VERSION = '2.1.326'
HOME_DST = '/home/node'
CONFIG_DST = HOME_DST + '/.claude'                    # CLAUDE_CONFIG_DIR, passed explicitly (never implied)
# Arm 3 second reviewer (replaces codex in the review playbook): fresh Claude sessions run by the reviewer helper
# (runner/fable_review.py serve) under its own account, with their own config dir on a per-run volume, so reviewer
# transcripts are separable from the PM's. The arm 3 container starts as root through runner/lab-arm3-start.sh, which
# starts the helper (the only holder of the token in its environment) and then the PM as node with the token on an
# inherited pipe (2.1.326 strips CLAUDE_CODE_OAUTH_TOKEN from Bash children: pilot 01). Arms 1/2 start as before.
REVIEWER_DST = HOME_DST + '/.fable-review'
REVIEWER_MODEL = 'claude-fable-5-2'
REVIEWER_UID = 2001                                   # image user fablereview; owns the reviewer volume (mode 0700)
ARM3_START = '/usr/local/bin/lab-arm3-start'
# only for lab-arm3-start: switch users, empty the bounding sets (SETPCAP); the reviewer helper reads the mirror
ARM3_CAPS = ('SETUID', 'SETGID', 'SETPCAP', 'DAC_READ_SEARCH')
USER_PROXY_MODEL = 'claude-opus-5-5'                 # agreed 2026-11-17 (scripts/consult_contract.py, arm 3 only)
# Harness v4.3 explorer subagent keeps the model of its own definition (agents/explorer.md: sonnet). Claude Code 2.1.326
# resolves a subagent's model as: explicit model (call parameter, else agent definition) before CLAUDE_CODE_SUBAGENT_MODEL,
# unless CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set (read from the CLI bundle, not yet observed in a paid run): the runner
# therefore never sets FORCE. Explorer requests are identified by the agentType of their transcript's .meta.json.
EXPLORER_AGENT = 'explorer'
EXPLORER_MODEL_PREFIX = 'claude-sonnet-'
TOKEN_KEY = 'CLAUDE_CODE_OAUTH_TOKEN'
USAGE_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
EXPECTED_UID = 1000                                   # image USER node; workspace .git must be owned by the same uid
# Agreed model control (protocol payment-settlement-v3-claude, 2026-11-17): implementers Opus 5.5 high;
# arm 3 PM Fable 5.2 high with Opus 5.5 workers (CLAUDE_CODE_SUBAGENT_MODEL). Requested ids are aliases;
# the provider-reported model per request is recorded separately (record_models / transcript_models).
# Arms 1/2 have no subagents.
MODELS = {1: ('claude-opus-5-5', None), 2: ('claude-opus-5-5', None), 3: ('claude-fable-5-2', 'claude-opus-5-5')}
# = the deploy manifest of the harness (hooks/deploy.sh v4.3: core.md HISTORY.md hooks playbooks templates agents)
HARNESS_SEED = [('src/core.md', 'core.md'), ('HISTORY.md', 'HISTORY.md'), ('hooks', 'hooks'), ('playbooks', 'playbooks'),
                ('templates', 'templates'), ('agents', 'agents')]
RUNNER_SEED = [('harness-bootstrap-CLAUDE.md', 'CLAUDE.md'), ('harness-settings.json', 'settings.json')]
ROLES = ('pm', 'implementer', 'worker', 'reviewer', 'user-proxy')   # user-proxy: scripts/consult_contract.py (consults/)
NO_SUBAGENT_TOOLS = ('Agent', 'Task', 'Workflow')   # fan-out surfaces that must never appear in arms 1/2 (asserted on the init event)
# Arms 1/2 run with an ALLOW-LIST of built-in tools (shell + file tools; no agents, skills, schedulers, messaging,
# worktrees). Web tools (decision 2026-11-17): allowed in all three arms and recorded (result.web_requests,
# manifest observed_tools); the selftest asserts they are on every arm's observed tool surface.
WEB_TOOLS = ('WebSearch', 'WebFetch')
ALLOWED_TOOLS_ARM12 = ('Bash', 'Read', 'Edit', 'Write', 'NotebookEdit', 'Glob', 'Grep', 'ToolSearch') + WEB_TOOLS
# Arm 3 = the same list + the subagent surface only (the CLI names its subagent tool 'Task'; '--tools ...,Task' yields it
# on the init event, checked on 2.1.326 without network). Nothing else: no skills, schedulers, messaging, worktrees.
SUBAGENT_TOOLS = ('Task',)
ALLOWED_TOOLS_ARM3 = ALLOWED_TOOLS_ARM12 + SUBAGENT_TOOLS

def allowed_tools(manifest):
    return ALLOWED_TOOLS_ARM3 if manifest.get('arm') == 3 else ALLOWED_TOOLS_ARM12
# Run caps (protocol v3-claude): every role of the run counts (pm/implementer/worker/reviewer/user-proxy), tokens are
# input + cache read + cache creation + output. Feedback = evaluator feedback deliveries only (decision 2026-11-17):
# successful turns declared 'feedback' (or a recovery of one). Arm 3 answer turns (the user proxy's answers delivered to
# the PM) are counted separately and never capped. Whichever cap is reached first ends the run as CENSORED_* (censored.json).
LIMITS = {'token_limit': 20000000, 'time_limit_seconds': 28800, 'feedback_limit': 30}
# Every turn records its kind in invocation.json (turn_kind). The first turn is 'initial'; a --resume turn must declare
# one of TURN_KINDS: 'feedback' (evaluator judging result, prompts/feedback.md), 'answer' (arm 3: proxy answers to the
# PM's questions, consults/), 'recovery' (continues the directly preceding turn that did not end ok; it counts as the
# kind of the turn it recovers, so a recovered feedback delivery is still one feedback).
TURN_KINDS = ('feedback', 'answer', 'recovery')
CENSORED = (('token_limit', 'CENSORED_BUDGET'), ('time_limit_seconds', 'CENSORED_TIME'), ('feedback_limit', 'CENSORED_TURNS'))
HELPER_TIMEOUT = 900                                  # seconds for any no-cost helper container (selftest, seeding, listing)
MIN_HOOKS = 8                                         # hooks wired in runner/harness-settings.json (fail if fewer are extracted)

def utc():
    return datetime.now(timezone.utc).isoformat()

def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')

def valid_run(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('run-id must be 1..64 safe ASCII characters')
    return value

def location(args):
    runs = Path(getattr(args, 'runs', None) or ROOT / '.runs')
    return runs.resolve() / valid_run(args.run_id)

def sha256_tree(path):
    path = Path(path)
    if path.is_file():
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}

def git_init(workspace):
    env = dict(os.environ, GIT_AUTHOR_NAME='lab', GIT_AUTHOR_EMAIL='lab@local',
               GIT_COMMITTER_NAME='lab', GIT_COMMITTER_EMAIL='lab@local', HOME=str(workspace))
    for cmd in (['git', 'init', '-q', '-b', 'main'], ['git', 'add', '-A'],
                ['git', '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'starter']):
        done = subprocess.run(cmd, cwd=workspace, env=env, check=False, capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError('workspace git init failed: ' + done.stderr[-500:])

def helper(manifest, script, mounts, network_none=True, user=None):
    """Run a short no-token helper container against this run's volumes; returns (rc, stdout, stderr).
    user: run as that uid instead of the image user (the reviewer volume belongs to REVIEWER_UID)."""
    name = 'payment-claude-helper-' + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--env', 'HOME=' + HOME_DST, '--env', 'CLAUDE_CONFIG_DIR=' + CONFIG_DST]
    if user is not None:
        command += ['--user', '%d:%d' % (user, user)]
    if network_none:
        command += ['--network', 'none']
    for mount in mounts:
        command += ['--mount', mount]
    command += [manifest['image'], 'bash', '-c', script]
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=HELPER_TIMEOUT)
    except subprocess.TimeoutExpired as expired:
        # the docker client died; the daemon-side container must be stopped explicitly (--rm only removes after exit)
        stopped = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, check=False)
        partial = expired.stdout.decode(errors='replace') if isinstance(expired.stdout, bytes) else (expired.stdout or '')
        return 124, partial, 'helper timed out after %ss; container %s removed rc=%s %s' % (HELPER_TIMEOUT, name, stopped.returncode, stopped.stderr[-200:])
    return done.returncode, done.stdout, done.stderr

def seed_harness(manifest):
    script = ' && '.join(
        ['set -e', 'mkdir -p ' + CONFIG_DST] +
        ['cp -r /harness/%s %s/%s' % (src, CONFIG_DST, dst) for src, dst in HARNESS_SEED] +
        ['cp /runner/%s %s/%s' % (src, CONFIG_DST, dst) for src, dst in RUNNER_SEED] +
        ['chmod 755 %s/hooks/*.sh' % CONFIG_DST, 'ls -la ' + CONFIG_DST])
    rc, out, err = helper(manifest, script, [
        'type=volume,src=%s,dst=%s' % (manifest['config_volume'], CONFIG_DST),
        'type=bind,src=%s,dst=/harness,readonly' % manifest['harness'],
        'type=bind,src=%s,dst=/runner,readonly' % str(ROOT / 'runner')])
    if rc:
        raise RuntimeError('harness seeding failed: ' + err[-2000:])
    return out

SELFTEST = r'''
set -u
fail=0
report() { echo "$1=$2"; }
# CLI argument acceptance without network or token (throwaway config dir; no model call can succeed).
export CLAUDE_CONFIG_DIR=/tmp/selftest-cfg; mkdir -p "$CLAUDE_CONFIG_DIR"
# $EXTRA_ARGS (a variadic --disallowedTools list) goes first so it cannot swallow the prompt argument
cd /tmp && timeout 180 claude -p $EXTRA_ARGS --verbose --output-format stream-json --model "$MODEL" --effort high --dangerously-skip-permissions \
  --session-id 00000000-0000-4000-8000-000000000000 --max-turns 1 "selftest" </dev/null >/tmp/cli.out 2>/tmp/cli.err; rc=$?
if grep -qiE "unknown option|too many arguments|invalid (value|argument)|error: (option|required)" /tmp/cli.err; then report cli_args_rejected 1; fail=1; else report cli_args_rejected 0; fi
report cli_exit "$rc"
case "$rc" in 126|127) report cli_launch_failed 1; fail=1;; esac
report cli_result_lines "$(grep -c '"type":"result"' /tmp/cli.out || true)"
# tool surface observed on the init event (arms 1/2 must not expose fan-out tools)
TOOLS=$(python3 -c "
import json,sys
for l in open('/tmp/cli.out'):
    try: e=json.loads(l)
    except Exception: continue
    if e.get('type')=='system' and e.get('subtype')=='init':
        print(','.join(e.get('tools') or [])); break
")
report cli_tools "$TOOLS"
if [ -z "$TOOLS" ]; then report cli_init_missing 1; fail=1; fi
# web tools are part of every arm's tool surface (same allowance in all three arms)
for t in $WEB_TOOLS; do case ",$TOOLS," in *",$t,"*) ;; *) report web_tool_missing "$t"; fail=1;; esac; done
if [ "$HARNESS" = 0 ]; then
  for t in $DENIED_TOOLS; do case ",$TOOLS," in *",$t,"*) report denied_tool_present "$t"; fail=1;; esac; done
fi
# every arm: observed surface must be a subset of its allow-list (a tool nobody enumerated is a failure, not a silent leak)
IFS=',' read -ra OBSERVED <<< "$TOOLS"
for t in "${OBSERVED[@]}"; do [ -n "$t" ] || continue; case ",$ALLOWED_TOOLS," in *",$t,"*) ;; *) report tool_outside_allowlist "$t"; fail=1;; esac; done
# arm 3 = arms 1/2 list + subagent surface: the subagent tools must really be there, and nothing beyond them is extra
if [ "$HARNESS" = 1 ]; then
  for t in $SUBAGENT_TOOLS; do case ",$TOOLS," in *",$t,"*) ;; *) report subagent_tool_missing "$t"; fail=1;; esac; done
  for t in "${OBSERVED[@]}"; do [ -n "$t" ] || continue; case " $SUBAGENT_TOOLS " in *" $t "*) continue;; esac
    case ",$BASE_TOOLS," in *",$t,"*) ;; *) report tool_beyond_base_and_subagents "$t"; fail=1;; esac; done
fi
# Workspace must be a readable git repository for the user inside the container (hooks classify by toplevel).
git -C /workspace rev-parse --show-toplevel >/dev/null 2>&1; rc=$?; report workspace_git_exit "$rc"; [ "$rc" = 0 ] || fail=1
if [ "$HARNESS" = 1 ]; then
  export CLAUDE_CONFIG_DIR=/home/node/.claude
  # deployed wiring must be byte-identical to the approved runner/harness-settings.json
  if [ "$(sha256sum "$CLAUDE_CONFIG_DIR/settings.json" | cut -d" " -f1)" = "$SETTINGS_SHA" ]; then report settings_sha_match 1; else report settings_sha_match 0; fail=1; fi
  # every hook wired in settings.json must exist, be executable and parse; an empty extraction is a failure
  HOOKLIST=$(python3 - <<'PY' 2>/dev/null
import json,re,os
d=json.load(open(os.environ['CLAUDE_CONFIG_DIR']+'/settings.json'))
c=' '.join(h['command'] for gs in d['hooks'].values() for g in gs for h in g['hooks'])
print(' '.join(sorted(set(re.findall(r'hooks/[A-Za-z0-9_.-]+\.sh', c)))))
PY
) || HOOKLIST=""
  n=0
  for f in $HOOKLIST; do
    n=$((n+1)); p="$CLAUDE_CONFIG_DIR/$f"; if [ -x "$p" ] && bash -n "$p"; then :; else report hook_missing "$f"; fail=1; fi
  done
  report hooks_checked "$n"
  [ "$n" -ge "$MIN_HOOKS" ] || { report hooks_too_few 1; fail=1; }
  # seeded harness content must still equal the init-time hashes (the agent must not rewrite its own hooks)
  CHANGED=$(python3 - <<'PY'
import json,hashlib,os,sys
exp=json.load(open('/expected/harness-hashes.json'))
root=os.environ['CLAUDE_CONFIG_DIR']
if not exp: print('EXPECTED-EMPTY'); sys.exit(3)
bad=[]
for rel,sha in exp.items():
    p=os.path.join(root,rel)
    try:
        if hashlib.sha256(open(p,'rb').read()).hexdigest()!=sha: bad.append(rel)
    except OSError as e:
        bad.append(rel+'(unreadable)')
print(' '.join(bad)); print('VERIFIED', len(exp), file=sys.stderr)
PY
); vrc=$?
  if [ "$vrc" != 0 ]; then report harness_verifier_exit "$vrc"; fail=1
  elif [ -n "$CHANGED" ]; then report harness_changed "$CHANGED"; fail=1
  else report harness_hash_match 1; fi
  # additions are tampering too: unexpected files inside seeded subtrees, override files, project-level settings
  EXTRA=$(python3 - <<'PY'
import json,os
exp=set(json.load(open('/expected/harness-hashes.json')))
root=os.environ['CLAUDE_CONFIG_DIR']
extra=[]
for sub in ('hooks','playbooks','templates','agents'):
    for d,_,files in os.walk(os.path.join(root,sub)):
        for f in files:
            rel=os.path.relpath(os.path.join(d,f),root)
            if rel not in exp and not rel.endswith('.pyc') and '__pycache__' not in rel.split(os.sep): extra.append(rel)
for name in ('settings.local.json','CLAUDE.local.md','hooks.json','managed-settings.json'):
    if os.path.exists(os.path.join(root,name)): extra.append(name)
for name in ('settings.json','settings.local.json'):
    if os.path.exists(os.path.join('/workspace/.claude',name)): extra.append('workspace/.claude/'+name)
print(' '.join(extra))
PY
); erc=$?
  if [ "$erc" != 0 ]; then report harness_extra_check_exit "$erc"; fail=1
  elif [ -n "$EXTRA" ]; then report harness_extra "$EXTRA"; fail=1
  else report harness_no_extras 1; fi
  # gate-guard must block an L1 write while SPEC=0 (exit 2) and pass a docs write (exit 0) in a fresh repo
  T=$(mktemp -d); git -C "$T" init -q -b main; (cd "$T" && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m init); mkdir -p "$T/docs/plans/x"
  printf '%s' "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$T/src/Main.kt\",\"content\":\"x\"},\"cwd\":\"$T\",\"session_id\":\"selftest0000\"}" | bash "$CLAUDE_CONFIG_DIR/hooks/gate-guard.sh" >/dev/null 2>/tmp/gg1.err; rc=$?
  report gate_l1_write_exit "$rc"; [ "$rc" = 2 ] || fail=1
  printf '%s' "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$T/docs/plans/x/requirement-spec.md\",\"content\":\"x\"},\"cwd\":\"$T\",\"session_id\":\"selftest0000\"}" | bash "$CLAUDE_CONFIG_DIR/hooks/gate-guard.sh" >/dev/null 2>/tmp/gg2.err; rc=$?
  report gate_docs_write_exit "$rc"; [ "$rc" = 0 ] || fail=1
  # Bash PreToolUse wiring: a benign build command must pass all three hooks; a state-file write must be blocked
  BENIGN="{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"./gradlew test\"},\"cwd\":\"$T\",\"session_id\":\"selftest0000\"}"
  codes=""
  for h in git-guard codex-scan gate-guard; do printf '%s' "$BENIGN" | bash "$CLAUDE_CONFIG_DIR/hooks/$h.sh" >/dev/null 2>&1; codes="$codes$h=$?;"; done
  report bash_hooks_benign "$codes"; [ "$codes" = "git-guard=0;codex-scan=0;gate-guard=0;" ] || fail=1
  TEETH="{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo MODE=auto | tee $T/.claude/lazymode/selftest0000\"},\"cwd\":\"$T\",\"session_id\":\"selftest0000\"}"
  printf '%s' "$TEETH" | bash "$CLAUDE_CONFIG_DIR/hooks/gate-guard.sh" >/dev/null 2>&1; rc=$?; report bash_hooks_teeth_exit "$rc"; [ "$rc" = 2 ] || fail=1
  PUSH="{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git push origin main\"},\"cwd\":\"$T\",\"session_id\":\"selftest0000\"}"
  PUSHOUT=$(printf '%s' "$PUSH" | bash "$CLAUDE_CONFIG_DIR/hooks/git-guard.sh" 2>/dev/null); rc=$?
  case "$PUSHOUT" in *'"ask"'*) report git_guard_push ask;; *) report git_guard_push "exit$rc"; fail=1;; esac
  # second reviewer entry point (replaces codex): present, executable and parseable
  R=$(command -v fable-review); if [ -n "$R" ] && [ -x "$R" ] && bash -n "$R"; then report reviewer_on_path 1; else report reviewer_on_path 0; fail=1; fi
  # the reviewer helper behind it: container start script, helper module and the reviewer account (no token, no call)
  if [ -x /usr/local/bin/lab-arm3-start ] && bash -n /usr/local/bin/lab-arm3-start \
     && python3 -I -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' /usr/local/lib/fable-review/fable_review.py 2>/dev/null \
     && [ "$(id -u fablereview 2>/dev/null)" = 2001 ]; then report reviewer_helper_present 1; else report reviewer_helper_present 0; fail=1; fi
fi
report selftest_failed "$fail"
'''

def expected_harness_hashes(manifest):
    """Flatten manifest.harness_seed into {path relative to CLAUDE_CONFIG_DIR: sha256}."""
    seed = manifest.get('harness_seed') or {}
    flat = {}
    for dst, tree in (seed.get('sources') or {}).items():
        for rel, sha in tree.items():
            flat[dst if dst.endswith('.md') else dst + '/' + rel] = sha
    for dst, tree in (seed.get('runner') or {}).items():
        for rel, sha in tree.items():
            flat[dst] = sha
    return flat

def selftest(manifest):
    """No-network, no-token assertions: CLI args/tool surface, workspace git, arm 3 hooks wired, unchanged and enforcing."""
    harness = bool(manifest.get('harness'))
    mounts = ['type=bind,src=%s,dst=/workspace,readonly' % manifest['workspace'],
              'type=volume,src=%s,dst=%s' % (manifest['config_volume'], CONFIG_DST)]
    runs = Path(manifest['workspace']).resolve().parent.parent          # <runs>/<run>/workspace → <runs>
    runs.mkdir(parents=True, exist_ok=True)
    expected = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, dir=runs)
    json.dump(expected_harness_hashes(manifest) if harness else {}, expected)
    expected.close()
    os.chmod(expected.name, 0o644)
    mounts.append('type=bind,src=%s,dst=/expected/harness-hashes.json,readonly' % expected.name)
    settings_sha = hashlib.sha256((ROOT / 'runner/harness-settings.json').read_bytes()).hexdigest()
    extra = '--tools ' + ','.join(allowed_tools(manifest))
    script = ('export MODEL=%s HARNESS=%d SETTINGS_SHA=%s MIN_HOOKS=%d DENIED_TOOLS="%s" ALLOWED_TOOLS="%s" BASE_TOOLS="%s" '
              'SUBAGENT_TOOLS="%s" WEB_TOOLS="%s" EXTRA_ARGS="%s"\n') % (
        manifest['requested_model'], 1 if harness else 0, settings_sha, MIN_HOOKS, ' '.join(NO_SUBAGENT_TOOLS),
        ','.join(allowed_tools(manifest)), ','.join(ALLOWED_TOOLS_ARM12), ' '.join(SUBAGENT_TOOLS), ' '.join(WEB_TOOLS),
        extra) + SELFTEST
    try:
        rc, out, err = helper(manifest, script, mounts)
    finally:
        os.unlink(expected.name)
    report = {'at': utc(), 'rc': rc, 'stderr_tail': err[-1000:]}
    for line in out.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            if key in ('hook_missing', 'denied_tool_present', 'tool_outside_allowlist', 'web_tool_missing',
                       'subagent_tool_missing', 'tool_beyond_base_and_subagents'):
                report.setdefault(key, []).append(value)
            else:
                report.setdefault(key, value)
    report['passed'] = rc == 0 and report.get('selftest_failed') == '0'
    return report

def default_readiness():
    return ROOT / 'docs/results/readiness.json'

def runner_image(args):
    """The image a new run uses. Before the freeze (smoke runs): --image or the CLI-version tag. Once readiness exists
    (pilot and cycles): the frozen runner digest, and any other --image is refused. Returns (image, readiness or None).
    A pre-freeze run is possible only when the DEFAULT readiness file is absent: an explicitly named readiness that does
    not exist is refused (a mistyped path never turns a frozen run into an unfrozen one)."""
    explicit = getattr(args, 'readiness', None)
    path = Path(explicit or default_readiness())
    requested = getattr(args, 'image', None)
    if not path.exists():
        if explicit:
            raise ValueError('readiness %s does not exist; omit --readiness only for a run before the freeze' % path)
        return requested or 'payment-claude-runner:' + CLI_VERSION, None
    ready = json.loads(path.read_text())
    digest = (ready.get('image_digests') or {}).get('runner') if isinstance(ready, dict) else None
    if not digest:
        raise ValueError('readiness %s carries no frozen runner image digest' % path)
    if requested and requested != digest:
        raise ValueError('image %s differs from the frozen runner digest %s (readiness %s); a different image needs a '
                         'new protocol version' % (requested, digest, path))
    return digest, dict(ready, path=path.resolve())

PILOT_RUN = re.compile(r'claude-pilot-\d{2}')

def pilot_run_id(run_id, pilot):
    """A pilot (init --pilot) is named claude-pilot-NN; no other run may carry that prefix."""
    if pilot and not PILOT_RUN.fullmatch(run_id):
        raise ValueError('a pilot run id must be claude-pilot-NN, not %s' % run_id)
    if not pilot and run_id.startswith('claude-pilot-'):
        raise ValueError('run id %s is a pilot id; use init --pilot' % run_id)

def record_folder(manifest, root=None):
    """Where the run's records live, chosen by the manifest: a pilot under docs/pilots/<run id>/, a measured run under
    docs/experiments/<arm>/core<N>/."""
    root = Path(root or ROOT)
    if manifest.get('pilot') is True:
        return root / 'docs/pilots' / valid_run(manifest['run_id'])
    return root / 'docs/experiments' / str(manifest['arm']) / ('core%d' % manifest['core'])

def open_record(manifest, run_id, root=None):
    """The record folder of run_id, refused when its run.json belongs to another run. A pilot's folder and run.json are
    created on first use; a measured run's run.json must already exist (scripts/prepare.py)."""
    if manifest.get('run_id') != run_id:
        raise ValueError('manifest belongs to %s, not %s' % (manifest.get('run_id'), run_id))
    folder = record_folder(manifest, root)
    plan = folder / 'run.json'
    if not plan.exists() and manifest.get('pilot') is True:
        folder.mkdir(parents=True, exist_ok=True)
        plan.write_text(json.dumps({'run_id': run_id, 'pilot': True, 'scope': 'PILOT_EXCLUDED', 'counts_toward_30_runs': False,
                                    'arm': manifest.get('arm'), 'core': manifest.get('core'), 'status': 'RUNNING',
                                    'readiness': manifest.get('readiness'), 'contract_sha256': manifest.get('contract_sha256'),
                                    'created_at': utc()}, ensure_ascii=False, indent=2) + '\n')
    if not plan.is_file():
        raise ValueError('record %s does not exist' % plan)
    owner = json.loads(plan.read_text()).get('run_id')
    if owner != run_id:
        raise ValueError('record folder %s belongs to %s, not %s' % (folder, owner, run_id))
    return folder

def initialize(args):
    dest = location(args)
    pilot_run_id(args.run_id, bool(getattr(args, 'pilot', False)))
    harness = Path(args.harness) if getattr(args, 'harness', None) else None
    args.harness = harness
    if harness and (args.arm != 3 or harness.resolve() != ROOT / 'harness-source' or not harness.is_dir()):
        raise ValueError('--harness requires arm 3 and the frozen claude/harness-source directory')
    if args.arm == 3 and not args.harness:
        raise ValueError('arm 3 requires --harness with the frozen source')
    network = getattr(args, 'network', None)
    if network and (network in ('host', 'container') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', network)):
        raise ValueError('use this run own Compose network; host network forbidden')
    if os.getuid() != EXPECTED_UID:
        raise ValueError('host uid must be %d so the container user can read the workspace git repository' % EXPECTED_UID)
    source = ROOT / str(args.arm) / ('core' + str(args.core))
    if not source.is_dir():
        raise ValueError('starter does not exist: ' + str(source))
    if dest.exists():
        raise ValueError('run already exists; overwrite refused')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('starter symlinks are not allowed')
    image, frozen = runner_image(args)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=dest.parent))   # same filesystem: final rename is atomic
    try:
        workspace = staging / 'workspace'
        shutil.copytree(source, workspace, ignore=shutil.ignore_patterns(*SKIP))
        # Hooks classify docs/** by git toplevel and git-guard needs a repo: every arm gets the same one-commit history.
        git_init(workspace)
        (staging / 'turns').mkdir()
        model, worker = MODELS[args.arm]
        manifest = {
            'run_id': args.run_id, 'arm': args.arm, 'core': args.core, 'provider': 'claude-code',
            'pilot': bool(getattr(args, 'pilot', False)),     # records go to docs/pilots/<run id>/ (record_folder)
            'cli_version': CLI_VERSION, 'requested_model': model, 'requested_worker_model': worker,
            'context_variant': 'default', 'subagents': 'opus-high' if worker else 'disabled',
            'actual_model': None, 'effort': 'high', 'session_id': str(uuid.uuid4()),
            'cache_policy': 'observed-cache', 'strict_zero_cache_supported': False,
            'image': image, 'image_frozen': frozen is not None,
            'readiness': str(frozen['path']) if frozen else None,
            # the frozen contract this run is measured under (None before the freeze); freeze_readiness ties pilot evidence to it
            'contract_sha256': frozen.get('contract_sha256') if frozen else None, 'config_volume': 'payment-claude-' + uuid.uuid4().hex,
            'reviewer_volume': ('payment-claude-reviewer-' + uuid.uuid4().hex) if args.arm == 3 else None,
            'requested_reviewer_model': REVIEWER_MODEL if args.arm == 3 else None,
            'reviewer_uid': REVIEWER_UID if args.arm == 3 else None,    # owner of the reviewer volume (helper account)
            'source': str(source), 'workspace': str(dest / 'workspace'), 'network': network,
            'workspace_git_init': True, 'harness': str(args.harness.resolve()) if args.harness else None,
            'harness_seed': None, 'selftest': None, 'created_at': utc(),
        }
        if args.arm == 3:
            manifest['harness_seed'] = {
                'sources': {dst: sha256_tree(Path(manifest['harness']) / src) for src, dst in HARNESS_SEED},
                'runner': {dst: sha256_tree(ROOT / 'runner' / src) for src, dst in RUNNER_SEED},
                'listing': seed_harness(manifest),
            }
        manifest['selftest'] = selftest(dict(manifest, workspace=str(workspace)))
        manifest['observed_tools'] = (manifest['selftest'].get('cli_tools') or '').split(',')
        if not manifest['selftest']['passed']:
            raise RuntimeError('selftest failed: ' + json.dumps(manifest['selftest'], ensure_ascii=False))
        write_json(staging / 'manifest.json', manifest)
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        for volume in (manifest.get('config_volume'), manifest.get('reviewer_volume')) if 'manifest' in locals() else ():
            if volume:
                subprocess.run(['docker', 'volume', 'rm', '-f', volume], capture_output=True, check=False)
        raise
    print(dest)

def claude_command(manifest, resume, max_turns=None, max_budget_usd=None):
    command = ['claude', '-p', '--verbose', '--output-format', 'stream-json', '--model', manifest['requested_model'],
               '--effort', 'high', '--dangerously-skip-permissions']
    command += ['--resume', manifest['session_id']] if resume else ['--session-id', manifest['session_id']]
    if max_turns:
        command += ['--max-turns', str(max_turns)]
    if max_budget_usd:
        command += ['--max-budget-usd', str(max_budget_usd)]
    command += ['--tools', ','.join(allowed_tools(manifest))]              # every arm: allow-list (arm 3 adds subagents)
    return command

def docker_command(manifest, env_file, name, resume, max_turns=None, max_budget_usd=None):
    # Writable: workspace bind and this run's own state volumes. The token travels only through --env-file (no other
    # credential is mounted). Arms 1/2: the image user node runs claude directly. Arm 3: the container starts as root
    # with three capabilities only and lab-arm3-start hands the token to the reviewer helper (its own account) and to
    # the PM (node, through a pipe, not its environment) — see runner/lab-arm3-start.sh.
    arm3 = manifest.get('arm') == 3
    command = ['docker', 'create', '--name', name, '--interactive', '--init',
               '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=512',
               '--cpus=2', '--memory=3g', '--workdir=/workspace', '--env-file', str(env_file),
               '--env', 'HOME=' + HOME_DST, '--env', 'CLAUDE_CONFIG_DIR=' + CONFIG_DST,
               '--mount', 'type=bind,src=' + manifest['workspace'] + ',dst=/workspace',
               '--mount', 'type=volume,src=' + manifest['config_volume'] + ',dst=' + CONFIG_DST]
    if manifest.get('requested_worker_model'):
        # workers: agreed model and effort high (CLI 2.1.326 reads CLAUDE_CODE_SUBAGENT_EFFORT). This is a setting, not
        # evidence: the worker model is checked per request in the pilot, and effort only where transcripts record
        # perTurnEffort (observed_tools_and_effort); otherwise effort propagation stays unverified.
        command += ['--env', 'CLAUDE_CODE_SUBAGENT_MODEL=' + manifest['requested_worker_model'],
                    '--env', 'CLAUDE_CODE_SUBAGENT_EFFORT=high']
    if arm3:
        if not manifest.get('reviewer_volume') or not manifest.get('requested_reviewer_model'):
            raise ValueError('arm 3 requires a reviewer volume and reviewer model (dual review second reviewer)')
        # read by lab-arm3-start before the PM starts (fixed for the helper, removed from the PM's environment)
        command += ['--mount', 'type=volume,src=' + manifest['reviewer_volume'] + ',dst=' + REVIEWER_DST,
                    '--env', 'LAB_REVIEWER_MODEL=' + manifest['requested_reviewer_model'],
                    '--env', 'LAB_REVIEWER_CONFIG_DIR=' + REVIEWER_DST, '--user', '0:0']
        for cap in ARM3_CAPS:
            command += ['--cap-add', cap]
    if manifest.get('network'):
        command += ['--network', manifest['network'], '--env', 'DB_URL=jdbc:postgresql://db:5432/payment',
                    '--env', 'BANK_BASE_URL=http://bank:8081']
    return command + [manifest['image']] + ([ARM3_START] if arm3 else []) + claude_command(manifest, resume, max_turns, max_budget_usd)

def read_token(env_file):
    env_file = Path(env_file)
    if not env_file.is_file():
        raise ValueError('env-file must exist')
    resolved = env_file.resolve()
    if REPO in resolved.parents:
        raise ValueError('env-file must live outside the repository')
    if stat.S_IMODE(env_file.stat().st_mode) & 0o077:
        raise ValueError('env-file must not be group/world accessible (chmod 600)')
    if ',' in str(resolved):
        raise ValueError('env-file path must not contain commas')
    assignments = []
    for line in env_file.read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            key, _, value = line.partition('=')
            if key.strip() != TOKEN_KEY or not value.strip():
                raise ValueError('env-file may contain only ' + TOKEN_KEY + '=<token>')
            assignments.append(value.strip())
    if len(assignments) != 1:
        raise ValueError('env-file must contain exactly one ' + TOKEN_KEY + ' assignment')
    return assignments[0]

RESULT_FIELDS = ('subtype', 'is_error', 'session_id', 'total_cost_usd', 'usage', 'modelUsage', 'num_turns', 'duration_ms',
                 'duration_api_ms')

def combine_results(results):
    """One turn may emit several result events (pilot 01 turn 0002: the session resumed after a background worker
    finished and reported a second result). Each event's usage covers its own part of the turn, so the four counters are
    added; total_cost_usd and modelUsage are cumulative over the session (2.1.326, pilot 01), so the last event's values
    stand for the turn. usage is None when any event lacks one of the four counters (never a partial sum)."""
    if not results:
        return None
    last = results[-1]
    combined = dict(last)
    usages = [r.get('usage') if isinstance(r.get('usage'), dict) else {} for r in results]
    if all(all(isinstance(u.get(k), int) and not isinstance(u.get(k), bool) for k in USAGE_KEYS) for u in usages):
        total = {k: sum(u[k] for u in usages) for k in USAGE_KEYS}
        tools = [u.get('server_tool_use') for u in usages]
        if all(isinstance(t, dict) for t in tools):
            total['server_tool_use'] = {k: sum(t.get(k) or 0 for t in tools if isinstance(t.get(k), int))
                                        for k in sorted({k for t in tools for k in t})}
        combined['usage'] = total
    else:
        combined['usage'] = None if len(results) > 1 else last.get('usage')
    combined['session_ids'] = sorted({r.get('session_id') for r in results if r.get('session_id')})
    if len(combined['session_ids']) > 1:
        combined['session_id'] = None                   # never one session's id standing for several
    combined['result_events'] = len(results)
    combined['subtypes'] = [r.get('subtype') for r in results]
    combined['any_error'] = any(r.get('is_error') for r in results)
    return combined

def collect(raw):
    """Parse the preserved stream-json; return the turn's result (all result events combined: combine_results), each
    result event as reported (results) and stream statistics."""
    raw = Path(raw)
    summary = {'result': None, 'results': [], 'assistant_messages': 0, 'stream_models': [], 'result_count': 0, 'unparsed_lines': 0}
    models = set()
    with raw.open('rb') as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                summary['unparsed_lines'] += 1
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'assistant':
                summary['assistant_messages'] += 1
                message = event.get('message') or {}
                if isinstance(message.get('model'), str):
                    models.add(message['model'])
            if event.get('type') == 'result':
                summary['result_count'] += 1
                summary['results'].append({k: event.get(k) for k in RESULT_FIELDS})
    summary['result'] = combine_results(summary['results'])
    summary['stream_models'] = sorted(models)
    return summary

def build_events(projects, seen, run_id, turn_id, arm, role=None):
    """One anthropic-adapter event per new requestId across all transcript copies; copies must agree.
    role fixes the role of every request (reviewer transcripts live in their own tree and have no PM/worker split)."""
    main_role = 'pm' if arm == 3 else 'implementer'
    entries = {}
    stats = {'files': 0, 'unparsable_lines': 0, 'assistant_lines': 0, 'assistant_without_requestId': 0, 'session_ids': set()}
    for file in sorted(Path(projects).rglob('*.jsonl')):
        stats['files'] += 1
        rel = str(file.relative_to(projects))
        agent_type = None                               # subagent transcripts have <name>.meta.json with agentType
        try:
            meta = json.loads(file.with_suffix('.meta.json').read_text())
            agent_type = meta.get('agentType') if isinstance(meta, dict) and isinstance(meta.get('agentType'), str) else None
        except (OSError, ValueError):
            pass
        for number, line in enumerate(file.read_bytes().splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                stats['unparsable_lines'] += 1
                continue
            if not isinstance(item, dict) or item.get('type') != 'assistant':
                continue
            stats['assistant_lines'] += 1
            if item.get('sessionId'):
                stats['session_ids'].add(item['sessionId'])
            rid = item.get('requestId')
            if not rid:
                stats['assistant_without_requestId'] += 1
                continue
            message = item.get('message') if isinstance(item.get('message'), dict) else {}
            usage = message.get('usage')
            entry = entries.setdefault(rid, {'usages': [], 'models': set(), 'worker': False, 'files': set(), 'tools': set(),
                                             'efforts': set(), 'sessions': set(), 'missing': False, 'first': (rel, number),
                                             'timestamp': item.get('timestamp'), 'agent_types': set()})
            if agent_type:
                entry['agent_types'].add(agent_type)
            # tool calls of this response (unique by tool_use id across lines and copies) and the CLI-recorded effort
            for block in message.get('content') if isinstance(message.get('content'), list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    entry['tools'].add((block.get('id'), block.get('name')))
            if isinstance(item.get('perTurnEffort'), str):
                entry['efforts'].add(item['perTurnEffort'])
            entry['worker'] = entry['worker'] or bool(item.get('isSidechain')) or bool(item.get('agentId')) or bool(item.get('parent_tool_use_id'))
            entry['files'].add(rel)
            if item.get('sessionId'):
                entry['sessions'].add(item['sessionId'])
            if isinstance(message.get('model'), str):
                entry['models'].add(message['model'])
            if isinstance(usage, dict) and all(isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and usage.get(k) >= 0 for k in USAGE_KEYS):
                entry['usages'].append({k: usage[k] for k in USAGE_KEYS})
            else:
                entry['missing'] = True
    events, incomplete, new_ids = [], [], []
    for rid, entry in entries.items():
        if rid in seen:
            continue
        new_ids.append(rid)
        where = {'requestId': rid, 'file': entry['first'][0], 'line': entry['first'][1]}
        if entry['missing'] or not entry['usages']:
            incomplete.append(dict(where, reason='usage or cache counters missing'))
            continue
        if any(u != entry['usages'][0] for u in entry['usages']):
            incomplete.append(dict(where, reason='usage differs between transcript copies', copies=sorted(entry['files'])))
            continue
        if len(entry['models']) != 1:
            incomplete.append(dict(where, reason='model missing or ambiguous', models=sorted(entry['models'])))
            continue
        events.append({'type': 'message', 'id': rid, 'event_id': rid, 'model': next(iter(entry['models'])),
                       'usage': entry['usages'][0], 'usage_scope': 'own_request',
                       'role': role or ('worker' if entry['worker'] else main_role), 'run_id': run_id, 'turn': turn_id,
                       'file': entry['first'][0], 'line': entry['first'][1], 'copies': len(entry['files']),
                       'transcript_sessions': sorted(entry['sessions']), 'timestamp': entry['timestamp'],
                       'web_tool_uses': sum(1 for _, n in entry['tools'] if n in WEB_TOOLS),
                       'effort': next(iter(entry['efforts'])) if len(entry['efforts']) == 1 else None,
                       'agent_type': next(iter(entry['agent_types'])) if len(entry['agent_types']) == 1 else None})
    stats['session_ids'] = sorted(stats['session_ids'])
    return events, sorted(new_ids), incomplete, stats

def observations(events, complete):
    """Per role: WebSearch/WebFetch tool calls seen in the transcripts and the efforts the CLI recorded per request.
    A request without a recorded effort shows as 'unobserved'; web counts cover only collected requests, so they are
    marked incomplete whenever the turn or run is (never presented as an observed zero)."""
    by_role = {}
    for event in events:
        role = by_role.setdefault(event['role'], {'web_tool_uses': 0, 'efforts': set()})
        role['web_tool_uses'] += event.get('web_tool_uses') or 0
        role['efforts'].add(event.get('effort') or 'unobserved')
    return {'by_role': {r: {'web_tool_uses': v['web_tool_uses'], 'efforts': sorted(v['efforts'])} for r, v in sorted(by_role.items())},
            'web_tool_uses_complete': bool(complete),
            'source': 'transcript tool_use blocks (WebSearch/WebFetch) and assistant perTurnEffort'}

def sum_usage(events):
    total = {k: 0 for k in USAGE_KEYS}
    for event in events:
        for k in USAGE_KEYS:
            total[k] += event['usage'][k]
    return total

def redact_inspect(raw):
    """Container inspect output carries Config.Env: keep variable names, never their values."""
    try:
        data = json.loads(raw)
    except ValueError:
        return b'<inspect output was not JSON; dropped to avoid leaking environment values>'
    for container in data if isinstance(data, list) else []:
        config = container.get('Config') or {}
        if isinstance(config.get('Env'), list):
            config['Env'] = [e.split('=', 1)[0] + '=<redacted>' for e in config['Env']]
    return json.dumps(data, ensure_ascii=False, indent=1).encode()

SCAN_CHUNK = 1 << 20

def contains_any(file, needles):
    """Stream the file in bounded chunks (overlap = longest needle - 1) so memory does not scale with file size."""
    overlap = max(len(n) for n in needles) - 1
    tail = b''
    with file.open('rb') as stream:
        while True:
            chunk = stream.read(SCAN_CHUNK)
            if not chunk:
                return False
            window = tail + chunk
            if any(n in window for n in needles):
                return True
            tail = window[-overlap:] if overlap else b''

def scrub(directory, secret):
    """Defensive last line: no credential may persist in any preserved file or file name under the run directory
    (workspace included). Accepts one secret or a list."""
    secrets = [s for s in ([secret] if isinstance(secret, str) else secret) if s]
    if not secrets:
        return []
    needles = [s.encode() for s in secrets]
    hits = []
    def mask(text):
        for s in secrets:
            text = text.replace(s, '<REDACTED-SECRET>')
        return text
    for file in sorted(Path(directory).rglob('*'), key=lambda p: -len(p.parts)):   # deepest first: renames stay valid
        rel = str(file.relative_to(directory))
        if any(s in file.name for s in secrets):
            new_name = file.name
            for s in secrets:
                new_name = new_name.replace(s, 'REDACTED-SECRET')
            target = file.with_name(new_name)
            file.rename(target)
            hits.append(mask(rel))
            file = target
        if file.is_file() and not file.is_symlink() and contains_any(file, needles):
            data = file.read_bytes()
            for n in needles:
                data = data.replace(n, b'<REDACTED-SECRET>')
            file.write_bytes(data)
            if mask(rel) not in hits:
                hits.append(mask(rel))
    return hits

def volume_listing(manifest):
    """Names only (never contents) of what the CLI left in the config volume outside transcripts."""
    rc, out, err = helper(manifest, "cd %s && find . -maxdepth 3 -not -path './projects/*' -printf '%%y %%p\\n' | sort" % CONFIG_DST,
                          ['type=volume,src=%s,dst=%s' % (manifest['config_volume'], CONFIG_DST)])
    names = out.splitlines() if rc == 0 else []
    return {'rc': rc, 'entries': names,
            'credentials_file_in_volume': any(n.endswith('/.credentials.json') for n in names) if rc == 0 else None}

def session_exists(manifest):
    """Does the run's config volume already hold a transcript for the pinned session id? (ground truth for --resume)"""
    rc, out, err = helper(manifest, "find %s/projects -name '%s.jsonl' 2>/dev/null | head -1" % (CONFIG_DST, manifest['session_id']),
                          ['type=volume,src=%s,dst=%s' % (manifest['config_volume'], CONFIG_DST)])
    if rc != 0:
        raise RuntimeError('cannot inspect the config volume for the session transcript: ' + err[-300:])
    return bool(out.strip())

def record_models(dest, manifest, events):
    """Persist client-observed model identities per role into the manifest (requested alias is never substituted)."""
    observed = manifest.get('actual_role_models') or {}
    for event in events:
        observed.setdefault(event['role'], [])
        if event['model'] not in observed[event['role']]:
            observed[event['role']].append(event['model'])
    manifest['actual_role_models'] = observed
    main = observed.get('pm') or observed.get('implementer') or []
    manifest['actual_model'] = main[0] if len(main) == 1 else None
    manifest['actual_model_note'] = 'client-observed in transcripts (message.model); null when absent or ambiguous'
    tmp = dest / 'manifest.json.tmp'
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(dest / 'manifest.json')

def mask_text(text, secrets):
    """Replace every credential value in free text (exception messages, diagnostics) before it is persisted."""
    if not text:
        return text
    for value in secrets:
        if value:
            text = text.replace(value, '<REDACTED-SECRET>')
    return text

def finalize_result(output, result, status, error, hits):
    """Overall turn status is decided last (after collection and the secret scan), written atomically."""
    path = output / 'result.json'
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update({'status': status, 'finished_at': utc(), 'secret_redactions': hits})
    if error:
        current['error'] = error
        current.setdefault('note', 'turn did not complete; raw files preserved; the next turn gets a new number')
    tmp = output / 'result.json.tmp'
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)

def reconcile_events(dest):
    """Merge every turn's events by requestId (earliest turn wins the usage; worker role is monotone;
    conflicting usage is excluded and reported) so partially recovered turns cannot double count."""
    merged, conflicts = {}, []
    sources = sorted(p / 'events.jsonl' for p in (dest / 'turns').iterdir() if p.is_dir())
    if (dest / 'consults').is_dir():                    # user-proxy calls of this run (scripts/consult_contract.py)
        sources += sorted(p / 'events.jsonl' for p in (dest / 'consults').iterdir() if p.is_dir())
    for events_file in sources:
        if not events_file.exists():
            continue
        for line in events_file.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            rid = event['id']
            if rid not in merged:
                merged[rid] = event
                continue
            first = merged[rid]
            if first['usage'] != event['usage'] or first['model'] != event['model']:
                conflicts.append({'requestId': rid, 'turns': [first['turn'], event['turn']]})
                first['conflict'] = True
            elif event['role'] == 'worker':
                first['role'] = 'worker'
    events = [e for e in merged.values() if not e.get('conflict')]
    return events, conflicts

def read_seen(dest):
    ledger = dest / 'request-ids.txt'
    return set(ledger.read_text().split()) if ledger.exists() else set()

def write_role_files(output, events):
    output = Path(output)
    mapping = {}
    for role in ROLES:
        subset = [e for e in events if e['role'] == role]
        if subset:
            name = 'events.role-%s.jsonl' % role
            with (output / name).open('x') as stream:
                for event in subset:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
            mapping[role] = name
    return mapping

CALL_STARTED = re.compile(r'(\S+) started session=([0-9a-f-]{36}) model=(\S+)')
CALL_EXIT = re.compile(r'(\S+) exit=(-?\d+)')
# the helper stopped the reviewer CLI because the caller disconnected (runner/fable_review.py): usage unobserved
CALL_CANCELLED = re.compile(r'(\S+) cancelled session=([0-9a-f-]{36}) reason=(\S+)')
# a request the helper refused before any CLI started (no session, no usage): counted, never an attempt with usage
CALL_REFUSED = re.compile(r'(\S+) refused reason=(.*)')

def reviewer_dir_names(name, output):
    """Names directly inside the reviewer directory of the stopped container, or None when they cannot be established.
    The directory is the mount point of the run's reviewer volume (and is created in the image), so it exists in every
    arm 3 container and copying it succeeds whether or not the reviewer was ever called: a name missing from the copy
    is absent. The answer never depends on the wording of docker's error for a missing path (it differs between Docker
    versions). The copy is temporary and removed at once; it sits inside the turn folder so the secret scan covers any
    leftover. Returns (names, docker rc, docker stderr, leftover): leftover is the copy's path relative to output when it
    could not be removed (the caller records the turn incomplete), else None."""
    probe = Path(tempfile.mkdtemp(prefix='.reviewer-dir-', dir=output))
    try:
        copied = subprocess.run(['docker', 'cp', name + ':' + REVIEWER_DST + '/.', str(probe)], capture_output=True, check=False)
        names = sorted(p.name for p in probe.iterdir()) if copied.returncode == 0 else None
    finally:
        try:
            shutil.rmtree(probe)
        except OSError:
            pass                                        # reported below through the path that is still there
    return names, copied.returncode, copied.stderr, (str(probe.relative_to(output)) if probe.exists() else None)

def collect_reviewer(name, output, dest, seen, run_id, turn_id, arm):
    """Arm 3: copy the second reviewer's call log, transcripts and raw results out of the stopped container (docker cp
    runs on the daemon side, so the reviewer account's 0700 directory is no obstacle) and account for them
    (account_reviewer). A log or transcript folder that could not be copied counts as absent only when the reviewer
    directory itself was read and does not hold it; otherwise it is unknown and the turn stays incomplete.
    Returns (events, new_ids, incomplete, summary, calls_seen)."""
    log = output / 'reviewer-calls.log'
    calls = subprocess.run(['docker', 'cp', name + ':' + REVIEWER_DST + '/reviewer-calls.log', str(log)],
                           capture_output=True, check=False)
    copied = subprocess.run(['docker', 'cp', name + ':' + REVIEWER_DST + '/projects', str(output / 'reviewer-projects')],
                            capture_output=True, check=False)
    names, dir_rc, dir_stderr, leftover = (None, None, b'', None)
    if calls.returncode != 0 or copied.returncode != 0:
        names, dir_rc, dir_stderr, leftover = reviewer_dir_names(name, output)
    (output / 'reviewer-copy.stderr').write_bytes(calls.stderr + copied.stderr + dir_stderr)
    raw = subprocess.run(['docker', 'cp', name + ':' + REVIEWER_DST + '/raw', str(output / 'reviewer-raw')],
                         capture_output=True, check=False)
    return account_reviewer(output, dest, seen, run_id, turn_id, arm, calls.returncode, copied.returncode, raw.returncode,
                            names, dir_rc, leftover)

SYNTHETIC_MODEL = '<synthetic>'                       # assistant lines the CLI writes itself (no API request behind them)

def transcript_requests(file):
    """(lines with a requestId, assistant lines without one, whether every line without one is a synthetic zero-usage
    line) of one transcript file."""
    with_rid, without, synthetic_only = 0, 0, True
    for line in Path(file).read_bytes().splitlines():
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return with_rid, without, False
        if not isinstance(item, dict) or item.get('type') != 'assistant':
            continue
        if item.get('requestId'):
            with_rid += 1
            continue
        without += 1
        message = item.get('message') if isinstance(item.get('message'), dict) else {}
        usage = message.get('usage') if isinstance(message.get('usage'), dict) else {}
        if message.get('model') != SYNTHETIC_MODEL or any(usage.get(k) not in (0, None) for k in USAGE_KEYS):
            synthetic_only = False
    return with_rid, without, synthetic_only

def failed_without_requests(raw_file, transcripts):
    """A reviewer attempt that failed before any API request (pilot 01: "Not logged in"): its raw result is an error
    with all four counters 0 (and cost 0 when given), and its transcript holds no line with a requestId, only synthetic
    zero-usage assistant lines. Such an attempt is a failed attempt with usage 0, not unobserved usage. Anything else
    (no raw result, no transcript, a real request) is not this case."""
    try:
        raw = json.loads(Path(raw_file).read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    usage = raw.get('usage') if isinstance(raw, dict) and isinstance(raw.get('usage'), dict) else None
    if not usage or raw.get('is_error') is not True or any(usage.get(k) != 0 for k in USAGE_KEYS):
        return False
    if raw.get('total_cost_usd') not in (None, 0):
        return False
    if not transcripts:
        return False
    counts = [transcript_requests(f) for f in transcripts]
    return all(rid == 0 and synthetic for rid, _, synthetic in counts)

def account_reviewer(output, dest, seen, run_id, turn_id, arm, log_rc, projects_rc, raw_rc, names=None, dir_rc=None,
                     leftover=None):
    """Account for every reviewer attempt from the copied files in the turn folder (reviewer-calls.log,
    reviewer-projects/, reviewer-raw/). Reviewer requests become role 'reviewer' events (request-level usage, never mixed
    into the PM's or workers' sums). An attempt that failed before any API request (failed_without_requests) is counted
    as a failed attempt with usage 0 and does not make the turn incomplete; an attempt that made requests that could not
    be collected keeps the turn incomplete. Returns (events, new_ids, incomplete, summary, calls_seen)."""
    incomplete = []
    if leftover:
        incomplete.append({'reason': 'temporary copy of the reviewer directory could not be removed from the turn folder',
                           'path': leftover})
    log = output / 'reviewer-calls.log'
    log_absent = log_rc != 0 and names is not None and 'reviewer-calls.log' not in names
    log_unknown = log_rc != 0 and not log_absent
    projects_absent = projects_rc != 0 and names is not None and 'projects' not in names
    started, finished, cancelled, refused = {}, {}, {}, {}
    for line in (log.read_text().splitlines() if log_rc == 0 else []):
        hit = CALL_STARTED.fullmatch(line)
        if hit:
            started[hit.group(1)] = hit.group(2)
        elif CALL_REFUSED.fullmatch(line):
            refused[CALL_REFUSED.fullmatch(line).group(1)] = CALL_REFUSED.fullmatch(line).group(2)
        elif CALL_CANCELLED.fullmatch(line):
            cancelled[CALL_CANCELLED.fullmatch(line).group(1)] = CALL_CANCELLED.fullmatch(line).group(3)
        elif CALL_EXIT.fullmatch(line):
            finished[CALL_EXIT.fullmatch(line).group(1)] = int(CALL_EXIT.fullmatch(line).group(2))
    # The log is cumulative inside the run's reviewer volume; calls already accounted for in earlier turns are skipped.
    ledger = dest / 'reviewer-calls.json'
    recorded = set(json.loads(ledger.read_text())['calls']) if ledger.exists() else set()
    new_calls = sorted(set(started) - recorded)
    new_refused = sorted(set(refused) - recorded)
    events, new_ids, stats, transcripts = [], [], {}, {}
    projects = output / 'reviewer-projects'
    if projects_rc == 0:
        for file in sorted(projects.rglob('*.jsonl')):
            transcripts.setdefault(file.stem, []).append(file)
    # attempts (this turn or earlier: the transcript tree is cumulative) that failed before any API request
    # (a cancelled call is never one of them: it was stopped, its requests may have gone out)
    zero = {sid for call, sid in started.items() if call not in cancelled
            and failed_without_requests(output / 'reviewer-raw' / (sid + '.json'), transcripts.get(sid))}
    if projects_rc == 0:
        events, new_ids, found, stats = build_events(projects, seen, run_id, turn_id, arm, role='reviewer')
        incomplete += found
        excused = sum(transcript_requests(f)[1] for sid in zero for f in transcripts.get(sid, []))
        without = (stats.get('assistant_without_requestId') or 0) - excused
        if stats.get('unparsable_lines') or without:
            incomplete.append({'reason': 'reviewer transcript lines without parseable usage or requestId',
                               'unparsable_lines': stats.get('unparsable_lines'), 'assistant_without_requestId': without})
    elif not projects_absent:
        incomplete.append({'reason': 'reviewer transcript copy failed (docker cp rc=%s)' % projects_rc})
    if log_unknown:
        incomplete.append({'reason': 'reviewer call log copy failed; reviewer attempts unknown (docker cp rc=%s)' % log_rc})
    elif transcripts and not started:
        incomplete.append({'reason': 'reviewer transcripts exist without any logged reviewer call'})
    for call in new_calls:
        if call in cancelled:
            incomplete.append({'reason': 'reviewer call cancelled (%s); usage unobserved' % cancelled[call], 'call': call,
                               'session': started[call]})
            continue
        if call not in finished:
            incomplete.append({'reason': 'reviewer call did not finish', 'call': call, 'session': started[call]})
        if started[call] not in transcripts:
            incomplete.append({'reason': 'reviewer attempt without transcript (usage unobservable)', 'call': call, 'session': started[call]})
    costs = reviewer_call_costs(output / 'reviewer-raw', [started[c] for c in new_calls])
    failed = [c for c in new_calls if finished.get(c, 1) != 0]
    summary = {'log_rc': log_rc, 'log_absent': log_absent, 'log_unknown': log_unknown,
               'calls_total': len(started), 'calls_this_turn': len(new_calls),
               'calls_finished_total': len(set(finished) & set(started)),
               'calls_succeeded_this_turn': sum(1 for c in new_calls if finished.get(c) == 0),
               'calls_failed_this_turn': len(failed),        # exit != 0, cancelled or no exit line
               'requests_refused_this_turn': [{'call': c, 'reason': refused[c]} for c in new_refused],
               'calls_cancelled_this_turn': [{'call': c, 'session': started[c], 'reason': cancelled[c]}
                                             for c in new_calls if c in cancelled],
               'failed_attempts_without_requests': [{'call': c, 'session': started[c]} for c in failed if started[c] in zero],
               'transcript_files': sum(len(v) for v in transcripts.values()),
               'projects_rc': projects_rc, 'projects_absent': projects_absent, 'reviewer_dir_rc': dir_rc,
               'reviewer_dir_names': names, 'reviewer_dir_leftover': leftover, 'transcript_stats': stats,
               'event_count': len(events), 'incomplete': len(incomplete), 'raw_rc': raw_rc, **costs}
    return events, new_ids, incomplete, summary, (None if log_unknown else sorted(set(started) | set(refused)))

def reviewer_call_costs(raw_dir, sessions):
    """CLI cost estimate of each reviewer call this turn, from the helper's raw/<session>.json (copied to reviewer-raw/).
    Each call is a fresh session, so its total_cost_usd is that call's cost. A call whose raw result cannot be read or
    has no numeric total_cost_usd stays listed as unobserved; the turn's reviewer cost is then None, never a partial sum
    presented as complete."""
    calls, unobserved = {}, []
    for sid in sessions:
        cost = None
        try:
            cost = number(json.loads((Path(raw_dir) / (sid + '.json')).read_text()).get('total_cost_usd'))
        except (OSError, ValueError, AttributeError):
            pass
        calls[sid] = cost
        if cost is None:
            unobserved.append(sid)
    return {'call_costs_usd': calls, 'cost_unobserved': unobserved,
            'cost_usd': None if unobserved else round(sum(calls.values()), 6)}

def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

# How the CLI reports cost (measured in pilot 01, 2.1.326): with --resume, total_cost_usd (and modelUsage) of a result
# event is CUMULATIVE over the session, not per invocation (turn 1 reported 1.9925, turn 2 reported 7.8483 including
# turn 1). Reviewer and user-proxy calls are fresh sessions, so their value is the call's own cost.
COST_SEMANTICS = ('pinned session: total_cost_usd of the turn\'s last result event is cumulative over the session (observed '
                  'only when every result event of the turn is the pinned session\'s); a turn\'s cost is its increase over '
                  'the previous observed turn; after an unobserved turn the next increase is an unassigned interval, not '
                  'that turn\'s cost; the run\'s pinned-session '
                  'cost is the last cumulative value. Reviewer calls and user-proxy consultations are fresh sessions '
                  '(their total_cost_usd is the call\'s cost). Unobserved values stay null, never 0.')

def turn_cost(stream_result, reviewer, session_id=None):
    """CLI cost estimates of one turn: the pinned session's cumulative total_cost_usd at the end of the turn (the run
    ledger derives the turn's increase) and (arm 3) the reviewer calls' sum. The value counts as observed only when
    every result event of the turn belongs to the pinned session (a forked or new session's total is another
    cumulative series). Unobserved parts stay None and make cost_complete false."""
    sessions = stream_result.get('session_ids')
    own = session_id is None or sessions == [session_id]
    pinned = number(stream_result.get('total_cost_usd')) if own else None
    reviewer_usd = None if reviewer is None else reviewer.get('cost_usd')
    return {'pinned_session_usd': pinned, 'pinned_session_usd_semantics': 'cumulative',
            **({} if own else {'pinned_session_unobserved': 'result events of other sessions: %s' % sessions}),
            'reviewer_usd': reviewer_usd, 'reviewer_applicable': reviewer is not None,
            'cost_complete': pinned is not None and (reviewer is None or reviewer_usd is not None)}

def account_turn(output, manifest, run_id, turn_id, seen, exit_code, copied_rc, reviewer_part=None):
    """The turn's accounting from the files already copied into the turn folder (stdout.jsonl, projects/ and, arm 3,
    the reviewer part from collect_reviewer/account_reviewer); no docker call, so a recorded turn can be re-accounted.
    Returns (result fields, events)."""
    r_events, r_ids, r_incomplete, reviewer = reviewer_part or ([], [], [], None)
    result = collect(output / 'stdout.jsonl')
    events, new_ids, incomplete, stats = [], [], [{'reason': 'transcript copy failed (docker cp rc=%s)' % copied_rc}], {}
    if copied_rc == 0 and (output / 'projects').exists():
        events, new_ids, incomplete, stats = build_events(output / 'projects', seen, run_id, turn_id, manifest['arm'])
    if reviewer is not None:
        events, new_ids, incomplete = events + r_events, sorted(new_ids + r_ids), incomplete + r_incomplete
    foreign = [s for s in ((result['result'] or {}).get('session_ids') or []) if s != manifest['session_id']]
    if foreign:
        incomplete.append({'reason': 'result session_id differs from the pinned session (forked or new session)',
                           'observed': foreign})
    if manifest['arm'] != 3:
        leaked = [e['id'] for e in events if e['role'] == 'worker']
        if leaked:
            incomplete.append({'reason': 'sidechain request in an arm that forbids subagents', 'requestIds': leaked})
    stream_result = result['result'] or {}
    reported = stream_result.get('usage') if isinstance(stream_result.get('usage'), dict) else None
    # The stream result covers the pinned session only: reviewer sessions are separate CLI processes and never part of it.
    # A sum over transcripts that could not be read is unknown, not zero (None); with incomplete requests the
    # sums cover only the observed requests (sums_are_lower_bounds).
    main_seen = copied_rc == 0 and (output / 'projects').exists()
    reviewer_seen = reviewer is not None and not reviewer['log_unknown'] and (reviewer['projects_rc'] == 0 or reviewer['projects_absent'])
    main_sum = sum_usage([e for e in events if e['role'] not in ('worker', 'reviewer')]) if main_seen else None
    all_sum = sum_usage([e for e in events if e['role'] != 'reviewer']) if main_seen else None
    cross = {'reported': reported, 'main_sum': main_sum, 'all_sum': all_sum, 'main_delta': None, 'all_delta': None,
             'reviewer_sum': sum_usage([e for e in events if e['role'] == 'reviewer']) if reviewer_seen else None,
             'sums_are_lower_bounds': bool(incomplete), 'result_events': result['result_count'],
             'note': 'reported = the four counters added over every result event of the turn (a turn may emit several); '
                     'result.usage does not include subagent requests (pilot 01), so main_delta is the expected match '
                     'and the run caps use only the request-level ledger; reviewer_sum is outside reported (None in '
                     'arms 1/2: no reviewer; None in arm 3: reviewer log/transcripts unreadable)'}
    if main_seen and reported and all(isinstance(reported.get(k), int) for k in USAGE_KEYS):
        cross['main_delta'] = {k: reported[k] - main_sum[k] for k in USAGE_KEYS}
        cross['all_delta'] = {k: reported[k] - all_sum[k] for k in USAGE_KEYS}
    result.update({
        'exit_code': exit_code, 'session_id': manifest['session_id'],
        'session_id_confirmed': stream_result.get('session_id') == manifest['session_id'],
        'request_ids': new_ids, 'event_count': len(events), 'incomplete': incomplete, 'transcript_stats': stats,
        'reviewer': reviewer,
        'cost': turn_cost(stream_result, reviewer, manifest['session_id']),
        'reviewer_usage_pending': bool(reviewer is not None and (r_incomplete or reviewer['log_unknown'])),
        'web_requests': reported.get('server_tool_use') if reported else None,
        'observed_tools_and_effort': observations(events, not incomplete and copied_rc == 0),
        'usage_complete': (not incomplete and result['result'] is not None and exit_code == 0
                           and copied_rc == 0 and bool(events)
                           and stats.get('unparsable_lines', 1) == 0 and result['unparsed_lines'] == 0
                           and stats.get('assistant_without_requestId', 1) == 0
                           and cross['main_delta'] is not None
                           and (all(v == 0 for v in cross['main_delta'].values()) or all(v == 0 for v in cross['all_delta'].values()))),
        'usage_complete_note': 'arm 3: every reviewer attempt this turn must have finished and left a transcript whose requests all carry usage (see reviewer / incomplete)',
        'usage_cross_check': cross,
        'transcript_models': sorted({e['model'] for e in events}), 'roles': sorted({e['role'] for e in events}),
        'actual_model': None,
        'actual_model_note': 'transcript models/modelUsage are client-observed; requested model is not proof.',
    })
    return result, events

def consult_completed(folder):
    """The single definition of a completed user-proxy consultation (consults/NNNN, scripts/consult_contract.py): the
    proxy call exited 0, the call recorded no error, no credential leak was found, and the response is non-empty.
    Only completed consultations permit an answer turn and count as consultations delivered by the proxy."""
    folder = Path(folder)
    try:
        usage = json.loads((folder / 'usage.json').read_text())
        response = (folder / 'response.txt').read_text()
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return (isinstance(usage, dict) and usage.get('exit_code') == 0 and not isinstance(usage.get('exit_code'), bool)
            and not usage.get('error') and not (folder / 'SECRET-LEAK.txt').exists() and bool(response.strip()))

def consult_folders(dest):
    return sorted(p for p in (Path(dest) / 'consults').iterdir() if p.is_dir()) if (Path(dest) / 'consults').is_dir() else []

def cost_value(value):
    """A cost argument (USD): a finite number >= 0, nothing else (argparse type)."""
    try:
        number_ = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('must be a number')
    if not math.isfinite(number_) or number_ < 0:
        raise argparse.ArgumentTypeError('must be a finite number >= 0')
    return number_

# Evaluation cost (experiment conductor, judging adapter) is not part of any run: it is recorded experiment-wide in
# docs/results/evaluation-cost.jsonl (scripts/record_evaluation_cost.py), never in a run ledger, cap or arm cost.

def total_tokens(usage):
    return sum(usage[k] for k in USAGE_KEYS)           # input + cache read + cache creation + output (cached input counts)

def run_ledger(dest):
    """The run's single usage ledger over every role: pm/implementer/worker/reviewer from turns and user-proxy from
    consults. Token sums are request-level and deduplicated; anything unobserved keeps the ledger incomplete (the
    sums are then lower bounds) and unobserved costs make cost_usd None instead of a partial number."""
    events, conflicts = reconcile_events(dest)
    roles = {}
    for event in events:
        roles.setdefault(event['role'], []).append(event)
    calls, unreadable = [], []
    for kind, folder, name in (('turn', 'turns', 'result.json'), ('consult', 'consults', 'usage.json')):
        for path in sorted((dest / folder).glob('*/' + name)) if (dest / folder).is_dir() else []:
            try:
                calls.append((kind, path.parent.name, json.loads(path.read_text())))
            except ValueError:
                unreadable.append(str(path.relative_to(dest)))
    if (dest / 'turns').is_dir():                       # a turn directory without any result means the turn was cut short
        unreadable += [str(p.relative_to(dest)) for p in sorted((dest / 'turns').iterdir()) if p.is_dir() and not (p / 'result.json').exists()]
    consults = consult_folders(dest)                    # likewise a consultation without usage.json: incomplete, not skipped
    unreadable += [str(p.relative_to(dest)) for p in consults if not (p / 'usage.json').exists()]
    costs, cost_unobserved, cost_by_call, cumulative, failed_attempts, gap = [], [], [], 0.0, 0, None
    for kind, ident, data in calls:
        row = {'call': '%s/%s' % (kind, ident)}
        if kind == 'turn':
            cost = data.get('cost') or {}
            reported = number(cost.get('pinned_session_usd'))
            reviewer_usd = cost.get('reviewer_usd') if cost.get('reviewer_applicable') else 0.0
            increase = None if reported is None else reported - cumulative
            if increase is not None and increase < -1e-9:
                row['anomaly'] = 'cumulative session cost decreased (%s after %s)' % (reported, cumulative)
                increase = None
            parts = [increase, reviewer_usd]
            if reported is None or increase is None:
                gap = gap or row['call']                  # the session's cost from here on is not attributable per turn
            elif gap:
                # after an unobserved turn the increase spans that turn and this one: an unassigned interval, never
                # this turn's cost (the run total stays null because the gap turn is unobserved)
                row['unassigned_interval'] = {'from_after': gap, 'usd': round(increase, 6)}
                increase, parts, gap = None, [reviewer_usd], None
            if reported is not None and 'anomaly' not in row:
                cumulative = reported
            row.update(pinned_session_cumulative_usd=reported,
                       pinned_session_increase_usd=None if increase is None else round(increase, 6),
                       reviewer_usd=number(reviewer_usd) if cost.get('reviewer_applicable') else None)
            failed_attempts += len(((data.get('reviewer') or {}).get('failed_attempts_without_requests')) or [])
        else:
            parts = [data.get('cost_usd')]
            row['consult_usd'] = number(data.get('cost_usd'))
        cost_by_call.append(row)
        if any(number(v) is None for v in parts):
            cost_unobserved.append(row['call'])
        else:
            costs += parts
    complete = (not conflicts and not unreadable and all(data.get('usage_complete') is True for _, _, data in calls))
    try:                                                # feedback (capped) and answer (arm 3, separate) deliveries
        kinds, kinds_error = turn_counts(turn_records(dest)), None
    except RuntimeError as exc:                         # never a zero count: the limit gate raises on the same records
        kinds, kinds_error = None, str(exc)
    return {
        'turn_kinds': kinds, 'turn_kinds_error': kinds_error,
        'roles': {role: {'requests': len(evs), 'usage': sum_usage(evs), 'total_tokens': total_tokens(sum_usage(evs))}
                  for role, evs in sorted(roles.items())},
        'total_tokens': total_tokens(sum_usage(events)), 'usage_complete': complete, 'tokens_are_lower_bound': not complete,
        'incomplete_calls': ['%s/%s' % (k, i) for k, i, d in calls if d.get('usage_complete') is not True] + unreadable,
        'observed_tools_and_effort': observations(events, complete),
        'conflicts': conflicts, 'turns': sum(1 for k, _, _ in calls if k == 'turn'),
        'consults': len(consults), 'consults_completed': sum(1 for p in consults if consult_completed(p)),
        'consults_not_completed': [p.name for p in consults if not consult_completed(p)],
        'cost_usd': None if cost_unobserved else round(sum(costs), 6), 'cost_unobserved': cost_unobserved,
        'cost_by_call': cost_by_call, 'cost_semantics': COST_SEMANTICS,
        'cost_note': 'CLI total_cost_usd estimates (subscription: billed_cost null)',
        'reviewer_calls_failed': sum(((d.get('reviewer') or {}).get('calls_failed_this_turn') or 0) for k, _, d in calls if k == 'turn'),
        'reviewer_failed_attempts_without_requests': failed_attempts,
        'reviewer_requests_refused': sum(len((d.get('reviewer') or {}).get('requests_refused_this_turn') or [])
                                         for k, _, d in calls if k == 'turn'),
        'updated_at': utc()}

def write_ledger(dest):
    value = run_ledger(dest)
    tmp = dest / 'ledger.json.tmp'
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(dest / 'ledger.json')
    return value

def parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

def turn_records(dest):
    """(turn id, kind, final status, started_at) of every turn directory in order. A turn without a readable
    invocation.json carrying a known turn_kind makes the count irreproducible: fail loudly, never skip it."""
    records = []
    for folder in sorted(p for p in (dest / 'turns').iterdir() if p.is_dir()) if (dest / 'turns').is_dir() else []:
        try:
            invocation = json.loads((folder / 'invocation.json').read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError('turn %s has no readable invocation.json; turn kinds cannot be counted (%s)' % (folder.name, exc))
        kind = invocation.get('turn_kind') if isinstance(invocation, dict) else None
        if kind not in ('initial',) + TURN_KINDS:
            raise RuntimeError('turn %s does not record a known turn_kind (%r); turn kinds cannot be counted' % (folder.name, kind))
        try:
            status = json.loads((folder / 'result.json').read_text()).get('status')
        except (OSError, ValueError):
            status = None                               # cut short or unreadable: not a successful delivery
        records.append((folder.name, kind, status, parse_time(invocation.get('started_at'))))
    return records

def turn_counts(records):
    """Successful deliveries per effective kind (a recovery counts as the kind of the turn it continues) and the
    declared kinds of every attempt. Only delivered['feedback'] is capped."""
    delivered = {'initial': 0, 'feedback': 0, 'answer': 0}
    declared = {k: 0 for k in ('initial',) + TURN_KINDS}
    effective = None
    for ident, kind, status, _ in records:
        declared[kind] += 1
        if kind != 'recovery':
            effective = kind
        elif effective is None:
            raise RuntimeError('recovery turn %s has no earlier turn to recover' % ident)
        if status == 'ok':
            delivered[effective] += 1
    return {'delivered': delivered, 'declared': declared}

def next_turn_kind(manifest, dest, records, resume, kind):
    """Validate the declared kind of the next turn before any paid call; return (kind, kind it counts as)."""
    if not resume:
        if kind:
            raise ValueError('a first turn is always initial; --kind applies to --resume turns only')
        return 'initial', 'initial'
    if kind not in TURN_KINDS:
        raise ValueError('a --resume turn must declare --kind %s (feedback = evaluator judging result; answer = arm 3 '
                         'proxy answers; recovery = continue the preceding turn that did not end ok)' % '|'.join(TURN_KINDS))
    # A turn that did not end ok may still have reached the model (timeout, CLI failure, failed secret scan). The next
    # turn continues it as a recovery, which counts as that turn's kind: a failed feedback is never left uncounted.
    # (A failed first turn that left no session is restarted without --resume and never reaches this check.)
    if records and records[-1][2] != 'ok' and kind != 'recovery':
        raise ValueError('the last turn %s did not end ok (status %s); the next --resume turn must be --kind recovery '
                         '(it counts as the kind of the turn it continues)' % (records[-1][0], records[-1][2]))
    if kind == 'answer':
        if manifest.get('arm') != 3:
            raise ValueError('answer turns exist only in arm 3 (user proxy); this run is arm %s' % manifest.get('arm'))
        consults = sum(1 for p in consult_folders(dest) if consult_completed(p))
        answers = turn_counts(records)['declared']['answer']
        if consults <= answers:
            raise ValueError('answer turn without an undelivered completed user-proxy consultation (%d completed consults, '
                             '%d answer turns); a failed or empty consultation permits no answer turn; a failed answer '
                             'delivery is continued with --kind recovery' % (consults, answers))
        return kind, kind
    if kind == 'recovery':
        if not records or records[-1][2] == 'ok':
            raise ValueError('recovery continues only a preceding turn that did not end ok; the last turn is %s'
                             % ('absent' if not records else records[-1][0] + ' (ok)'))
        effective = next(k for _, k, _, _ in reversed(records) if k != 'recovery')
        return kind, effective
    return kind, kind

def limit_state(dest, next_is_feedback=False, now=None):
    """Where the run stands against its caps, from the run ledger (all roles) and the turn records."""
    ledger = run_ledger(dest)
    records = turn_records(dest)
    counts = turn_counts(records)
    starts = [start for _, _, _, start in records if start]
    now = now or datetime.now(timezone.utc)
    elapsed = (now - min(starts)).total_seconds() if starts else 0.0
    used = {'token_limit': ledger['total_tokens'], 'time_limit_seconds': elapsed, 'feedback_limit': counts['delivered']['feedback']}
    # tokens/time: a run that has used its whole budget gets no further call; feedback: only a next feedback delivery counts
    reached = [status for key, status in CENSORED
               if used[key] >= LIMITS[key] and (key != 'feedback_limit' or next_is_feedback)]
    return {'used': used, 'limits': dict(LIMITS), 'tokens_are_lower_bound': ledger['tokens_are_lower_bound'],
            'answer_turns': counts['delivered']['answer'], 'turn_kinds': counts,
            'overshoot': {k: used[k] - LIMITS[k] for k in LIMITS if used[k] > LIMITS[k]}, 'reached': reached, 'at': utc()}

CALL_KINDS = ('turn', 'user-proxy')                   # the kinds of paid call a cap record names (censored.json 'call')

def censor(dest, state, when, call):
    """Write censored.json once (the first cap reached decides the status); later overshoot is appended, never erased.
    call names the kind of paid call that was refused or went past the cap ('turn' or 'user-proxy'); scripts/finish_run.py
    copies it into run.json as annotation (pass/fail comes from the verdict table)."""
    if call not in CALL_KINDS:
        raise ValueError('call must be one of ' + ', '.join(CALL_KINDS))
    path = dest / 'censored.json'
    if path.exists():
        record = json.loads(path.read_text())
        record.setdefault('later', []).append(dict(state, when=when, call=call))
    else:
        record = {'status': state['reached'][0], 'first': dict(state, when=when, call=call)}
    tmp = dest / 'censored.json.tmp'
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)
    return record

def limit_gate(dest, next_is_feedback, write=True, call='turn'):
    """Single gate before every paid call of a run (turns and user-proxy consultations). A censored run accepts no
    further call; reaching a cap now censors the run. Artifacts are never removed."""
    if (dest / 'censored.json').exists():
        raise RuntimeError('run is %s; no further paid call (see censored.json)' % json.loads((dest / 'censored.json').read_text())['status'])
    state = limit_state(dest, next_is_feedback)
    if state['reached']:
        if write:
            censor(dest, state, 'before call', call)
        raise RuntimeError('run cap reached before the call: %s %s' % (state['reached'][0], json.dumps(state['used'])))
    return state

def call_timeout(requested, state):
    """A single paid call never runs past the run's wall-clock cap: the requested timeout reduced to the remainder."""
    return max(1, min(requested, int(LIMITS['time_limit_seconds'] - state['used']['time_limit_seconds'])))

def record_overshoot(dest, call):
    """After a call of kind call ('turn' or 'user-proxy'): if the run went past a cap during it, censor the run and
    record by how much and in which kind of call."""
    state = limit_state(dest)
    if state['overshoot']:
        state['reached'] = [status for key, status in CENSORED if key in state['overshoot']]
        return censor(dest, state, 'after call', call)
    return None

def recovery_prompt_check(dest, records, prompt):
    """A recovery turn continues the turn before it, so it sends exactly that turn's prompt (same sha256)."""
    previous = dest / 'turns' / records[-1][0] / 'invocation.json'
    try:
        expected = json.loads(previous.read_text()).get('prompt_sha256')
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError('cannot read the prompt hash of the turn being recovered (%s): %s' % (previous, exc))
    if not expected or hashlib.sha256(prompt).hexdigest() != expected:
        raise ValueError('a recovery turn sends the prompt of the turn it continues (%s, sha256 %s); this prompt differs'
                         % (records[-1][0], expected))

# Arm 3 hook self-tests that failed before a call. No turn directory exists for them (no call went out, the turn number
# is not used) so they live beside turns/, one file each; scripts/freeze_readiness.run_isolation_check reads them.
PRECALL_SELFTEST_FAILURES = 'selftest-failures-before-call'

def record_precall_selftest_failure(dest, report, turn_kind, counts_as):
    """Persist a pre-call hook self-test that did not pass (or could not run). It is not a turn: turn records, the
    ledger and the next turn's kind are unaffected (the next attempt is a normal turn, not a recovery)."""
    folder = dest / PRECALL_SELFTEST_FAILURES
    folder.mkdir(exist_ok=True)
    path = folder / ('%s-%s.json' % (utc().replace(':', '-'), uuid.uuid4().hex[:8]))
    write_json(path, {'at': utc(), 'paid_call': False, 'turn_kind_attempted': turn_kind, 'counts_as': counts_as,
                      'hook_selftest_before_call': report})
    return path

def precall_selftest_failures(dest):
    """Every recorded pre-call self-test failure of the run (an unreadable record is still a failure)."""
    folder = Path(dest) / PRECALL_SELFTEST_FAILURES
    found = []
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            found.append({'unreadable': str(path)})
    return found

def turn(args):
    dest = location(args)
    manifest = json.loads((dest / 'manifest.json').read_text())
    if manifest['run_id'] != args.run_id or Path(manifest['workspace']).resolve() != dest / 'workspace':
        raise ValueError('manifest identity/workspace mismatch')
    if ',' in str(dest):
        raise ValueError('Docker mount paths must not contain commas')
    env_file = Path(args.env_file).resolve()
    secret = read_token(env_file)                      # validated in dry-run too
    needles = [secret]                                 # the only credential placed in the blast radius
    turn_dirs = sorted(p for p in (dest / 'turns').iterdir() if p.is_dir())
    successes = []
    for p in turn_dirs:
        try:
            if (p / 'result.json').exists() and json.loads((p / 'result.json').read_text()).get('status') == 'ok':
                successes.append(p)
        except ValueError as broken:
            print('unreadable result treated as failed: %s (%s)' % (p / 'result.json', broken), file=sys.stderr)
    # the turn's kind decides whether it is a capped feedback delivery; validated before any paid call
    records = turn_records(dest)
    kind, counts_as = next_turn_kind(manifest, dest, records, args.resume, getattr(args, 'kind', None))
    prompt = Path(args.prompt).read_bytes()
    if kind == 'recovery':
        recovery_prompt_check(dest, records, prompt)
    if args.dry_run:
        exists = bool(successes)
    else:
        exists = session_exists(manifest)       # a failed turn may still have persisted the session
    if args.resume != exists:
        raise ValueError('first turn requires no --resume; subsequent turns require --resume '
                         '(session transcript %s in the config volume; %d turn dirs, %d successful)'
                         % ('present' if exists else 'absent', len(turn_dirs), len(successes)))
    limits = limit_gate(dest, next_is_feedback=counts_as == 'feedback', write=not args.dry_run)
    timeout = call_timeout(args.timeout_seconds, limits)
    seen = read_seen(dest)
    turn_id = f'{len(turn_dirs) + 1:04d}'   # failed turns keep their directory and number; retries get a new one
    name = 'payment-claude-call-' + uuid.uuid4().hex
    command = docker_command(manifest, env_file, name, args.resume, args.max_turns, args.max_budget_usd)
    if args.dry_run:
        print(json.dumps({'create': command, 'start': ['docker', 'start', '--attach', '--interactive', name],
                          'prompt_bytes': len(prompt), 'session_id': manifest['session_id'], 'resume': args.resume,
                          'turn_kind': kind, 'counts_as': counts_as}, indent=2))
        return
    if args.cache_policy != 'observed-cache':
        raise ValueError('explicit --cache-policy observed-cache required; strict cache control unavailable')
    pre = None
    if manifest.get('harness'):
        try:
            pre = selftest(manifest)
        except Exception as exc:                        # the self-test could not run: recorded as a failure too
            record_precall_selftest_failure(dest, {'passed': False, 'error': str(exc)[:2000]}, kind, counts_as)
            raise
        if not pre.get('passed'):
            record_precall_selftest_failure(dest, pre, kind, counts_as)
            raise RuntimeError('hook self-test failed before the call: ' + json.dumps(pre, ensure_ascii=False))
    if pre is not None:
        # the self-test took wall-clock time: gate again and reduce the call to what remains after it
        limits = limit_gate(dest, next_is_feedback=counts_as == 'feedback')
        timeout = call_timeout(args.timeout_seconds, limits)
    output = dest / 'turns' / turn_id
    output.mkdir()
    (output / 'prompt.txt').write_bytes(prompt)
    write_json(output / 'invocation.json', {'create': command, 'session_id': manifest['session_id'], 'resume': args.resume,
        'turn_kind': kind, 'counts_as': counts_as, 'prompt_sha256': hashlib.sha256(prompt).hexdigest(), 'cache_policy': args.cache_policy,
        'env_file': str(env_file), 'started_at': utc(), 'timeout_seconds': args.timeout_seconds,
        'max_turns': args.max_turns, 'max_budget_usd': args.max_budget_usd, 'hook_selftest_before_call': pre,
        'effective_timeout_seconds': timeout, 'limits_before_call': limits})
    result = None
    failure = None
    try:
        created = subprocess.run(command, capture_output=True, check=False)
        (output / 'create.stdout').write_bytes(created.stdout)
        (output / 'create.stderr').write_bytes(created.stderr)
        if created.returncode:
            raise RuntimeError('docker create failed; see preserved stderr')
        with (output / 'stdout.jsonl').open('xb') as stdout, (output / 'stderr.txt').open('xb') as stderr:
            try:
                proc = subprocess.run(['docker', 'start', '--attach', '--interactive', name], input=prompt,
                                      stdout=stdout, stderr=stderr, check=False, timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(['docker', 'stop', '--time', '5', name], capture_output=True, check=False)
                proc = SimpleNamespace(returncode=124)
        inspected = subprocess.run(['docker', 'inspect', name], capture_output=True, check=False)
        (output / 'container-inspect.json').write_bytes(redact_inspect(inspected.stdout))
        copied = subprocess.run(['docker', 'cp', name + ':' + CONFIG_DST + '/projects', str(output / 'projects')],
                                capture_output=True, check=False)
        (output / 'projects-copy.stderr').write_bytes(copied.stderr)
        reviewer_part, reviewer_calls = None, None
        if manifest.get('arm') == 3:
            r_events, r_ids, r_incomplete, reviewer, reviewer_calls = collect_reviewer(
                name, output, dest, seen, args.run_id, turn_id, manifest['arm'])
            reviewer_part = (r_events, r_ids, r_incomplete, reviewer)
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)
        result, events = account_turn(output, manifest, args.run_id, turn_id, seen, proc.returncode, copied.returncode,
                                      reviewer_part)
        with (output / 'events.jsonl').open('x') as stream:
            for event in events:
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        result.update({'finished_at': utc(), 'role_files': write_role_files(output, events),
                       'config_volume': volume_listing(manifest)})
        result['cli_status'] = 'ok' if proc.returncode == 0 else 'failed'
        result['status'] = 'collected'                 # finalized in the finally block after the secret scan
        write_json(output / 'result.json', result)
        # Only after the result is durable: fully accounted requests enter the ledger (incomplete ones stay open).
        with (dest / 'request-ids.txt').open('a') as ledger:
            for event in events:
                ledger.write(event['id'] + '\n')
        if reviewer_part is not None and reviewer_calls is not None:
            (dest / 'reviewer-calls.json').write_text(json.dumps({'calls': reviewer_calls}) + '\n')
        record_models(dest, manifest, events)
        print(json.dumps({k: result[k] for k in ('exit_code', 'session_id_confirmed', 'event_count', 'usage_complete',
                                                 'transcript_models', 'roles')}))
        if proc.returncode:
            raise RuntimeError('Claude call failed or timed out; raw logs preserved')
    except BaseException as exc:
        failure = str(exc)[:2000]
        raise
    finally:
        try:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)
        except OSError as cleanup_error:
            print('container cleanup failed: %s' % cleanup_error, file=sys.stderr)
        scan_error = None
        try:
            hits = scrub(dest, needles)
        except Exception as exc:                       # the scan itself failed: coverage unknown → the turn cannot be ok
            hits, scan_error = [], mask_text(str(exc)[:500], needles)
        if hits:
            (output / 'SECRET-LEAK.txt').write_text('credential found and redacted in: ' + ', '.join(hits) + '\nrotate the token now\n')
            print('SECRET LEAK: credential found in ' + ', '.join(hits) + ' (redacted); rotate the token', file=sys.stderr)
        in_flight = sys.exc_info()[1]
        final = 'failed' if (hits or scan_error or in_flight is not None or (result and result.get('cli_status') != 'ok')) else 'ok'
        error = mask_text(failure, needles) if in_flight is not None else None
        if scan_error:
            error = (error + ' | ' if error else '') + 'secret scan failed: ' + scan_error
        try:
            finalize_result(output, result, final, error, hits)
            write_ledger(dest)
            record_overshoot(dest, 'turn')
        finally:
            if (hits or scan_error) and in_flight is None:
                raise RuntimeError('credential leaked into preserved files or the scan failed; see result.json / SECRET-LEAK.txt')

RAW_ENTRY = re.compile(r'f /vol/raw/([0-9a-f-]{36})\.json')

def discard(args):
    """End of run: list what the run's volumes hold (names only), check that every reviewer call result in the
    reviewer volume was recovered into the run directory, then remove the volumes. The run directory stays.
    Refuses (removing nothing) when a reviewer result was never recovered; fails loudly when a removal fails, and
    writes discard-<time>.json either way (complete=true only when every volume is gone)."""
    dest = location(args)
    manifest = json.loads((dest / 'manifest.json').read_text())
    record = {'at': utc(), 'volumes': {}, 'refused': None}
    listings = {}
    for key in ('config_volume', 'reviewer_volume', 'codex_volume'):   # codex_volume: manifests created before v3-claude
        volume = manifest.get(key)
        if volume:
            # the reviewer volume belongs to the reviewer account (mode 0700): list it as that uid (manifests from before
            # the reviewer helper carry no reviewer_uid; their volume belongs to the image user as before)
            user = manifest.get('reviewer_uid') if key == 'reviewer_volume' else None
            rc, out, err = helper(manifest, "find /vol -printf '%y %p\\n' | sort", ['type=volume,src=%s,dst=/vol' % volume],
                                  **({'user': user} if user is not None else {}))
            listings[key] = (volume, rc, out.splitlines() if rc == 0 else None)
    if 'reviewer_volume' in listings:
        _, rc, entries = listings['reviewer_volume']
        recovered = {p.stem for p in dest.glob('turns/*/reviewer-raw/*.json')}
        in_volume = {m.group(1) for line in entries or [] for m in [RAW_ENTRY.fullmatch(line)] if m}
        if rc != 0:
            record['refused'] = 'reviewer volume could not be listed; recovery of reviewer results unknown'
        elif in_volume - recovered:
            record['refused'] = 'reviewer results not recovered into the run: ' + ', '.join(sorted(in_volume - recovered))
    for key, (volume, rc, entries) in listings.items():
        entry = {'listing_rc': rc, 'entries': entries, 'removed': False, 'stderr': ''}
        if not record['refused']:
            removed = subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True, text=True, check=False)
            entry.update(removed=removed.returncode == 0, stderr=removed.stderr[-500:])
        record['volumes'][volume] = entry
    record['complete'] = not record['refused'] and all(v['removed'] for v in record['volumes'].values())
    write_json(dest / ('discard-%s.json' % utc().replace(':', '-')), record)
    print(json.dumps({v: r['removed'] for v, r in record['volumes'].items()}))
    if not record['complete']:
        raise RuntimeError('volume cleanup incomplete: ' + (record['refused'] or 'a volume could not be removed (see discard-*.json)'))

def discard_complete(dest):
    """Evidence for the next cycle: the run's latest discard record removed every volume."""
    records = sorted(Path(dest).glob('discard-*.json'))
    return bool(records) and json.loads(records[-1].read_text()).get('complete') is True

def role_model(manifest, role, events=()):
    """Requested model label per role (normalization label only; actual models come from the transcripts)."""
    if role == 'user-proxy':
        return next((e.get('requested_model') for e in events if e.get('requested_model')), 'unspecified')
    if role == 'worker':
        return manifest.get('requested_worker_model') or 'unspecified'
    if role == 'reviewer':
        return manifest.get('requested_reviewer_model') or 'unspecified'
    return manifest['requested_model']

def normalize(args):
    """Run the shared anthropic adapter once per role file, then summarize the whole run."""
    dest = location(args)
    measurement = ROOT.parent / 'measurement'
    manifest = json.loads((dest / 'manifest.json').read_text())
    outputs = []
    events, conflicts = reconcile_events(dest)          # one event per requestId across ALL turns (failed ones included)
    (dest / 'reconciled-events.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events))
    write_json(dest / ('reconcile-%s.json' % utc().replace(':', '-')), {'events': len(events), 'conflicts': conflicts})
    for role in ROLES:
        subset = [e for e in events if e['role'] == role]
        if not subset:
            continue
        source = dest / ('reconciled.role-%s.jsonl' % role)
        source.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in subset))
        target = dest / ('normalized.role-%s.jsonl' % role)
        if target.exists():
            target.unlink()
        subprocess.run([sys.executable, str(measurement / 'normalize.py'), '--provider', 'anthropic',
                        '--run-id', manifest['run_id'], '--session-id', manifest['session_id'] + '/' + role,
                        '--role', role, '--model', role_model(manifest, role, subset),
                        '--input', str(source), '--output', str(target)], check=True)
        outputs.append(target)
    if conflicts:
        print('WARNING: %d requestId conflicts excluded from normalization (see reconcile-*.json)' % len(conflicts), file=sys.stderr)
    combined = dest / 'usage-normalized.jsonl'
    combined.write_text(''.join(p.read_text() for p in outputs))
    subprocess.run([sys.executable, str(measurement / 'summarize.py'), str(combined), '--output', str(dest / 'usage-summary.json')], check=True)
    write_ledger(dest)
    print((dest / 'usage-summary.json').read_text())

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, default=ROOT / '.runs')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--run-id', required=True)
    init.add_argument('--arm', type=int, choices=(1, 2, 3), required=True)
    init.add_argument('--core', type=int, choices=range(1, 11), required=True)
    init.add_argument('--harness', type=Path, help='Arm 3 only: frozen harness snapshot seeded into the config volume')
    init.add_argument('--network', help='Own run Compose network; never host networking')
    init.add_argument('--image', help='default: the frozen runner digest of --readiness once it exists (any other image '
                      'is refused), else payment-claude-runner:' + CLI_VERSION + ' (smoke runs before the freeze)')
    init.add_argument('--readiness', type=Path, help='default: claude/docs/results/readiness.json (a run before the freeze '
                      'only when that default is absent; a named readiness that does not exist is refused)')
    init.add_argument('--pilot', action='store_true', help='pilot run: id claude-pilot-NN, records under docs/pilots/<run id>/')
    run = sub.add_parser('turn')
    run.add_argument('--run-id', required=True)
    run.add_argument('--prompt', type=Path, required=True)
    run.add_argument('--env-file', type=Path, required=True, help='0600 file outside the repo with only CLAUDE_CODE_OAUTH_TOKEN=...')
    run.add_argument('--timeout-seconds', type=int, default=LIMITS['time_limit_seconds'],
                     help='default: the run cap (no per-turn limit, decision 2026-11-18); always reduced to the run time left')
    run.add_argument('--max-turns', type=int)
    run.add_argument('--max-budget-usd', type=cost_value)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--kind', choices=TURN_KINDS, help='required with --resume: feedback (evaluator judging result; '
                     'capped at 30) | answer (arm 3: user-proxy answers to the PM) | recovery (continue the preceding '
                     'turn that did not end ok; counts as that turn\'s kind)')
    run.add_argument('--dry-run', action='store_true')
    run.add_argument('--cache-policy', choices=('observed-cache',))
    norm = sub.add_parser('normalize')
    norm.add_argument('--run-id', required=True)
    disc = sub.add_parser('discard')
    disc.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        {'init': initialize, 'turn': turn, 'normalize': normalize, 'discard': discard}[args.command](args)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + '\n')

if __name__ == '__main__':
    main()
