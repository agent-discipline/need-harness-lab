#!/usr/bin/env python3
"""Prepare an isolated Claude Code run; an explicit turn executes a paid subscription call."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
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
# Arm 3 second reviewer (replaces codex in the review playbook): fresh Claude sessions through runner/fable-review.sh
# with their own config dir on a per-run volume, so reviewer transcripts are separable from the PM's.
REVIEWER_DST = HOME_DST + '/.fable-review'
REVIEWER_MODEL = 'claude-fable-5-2'
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

def helper(manifest, script, mounts, network_none=True):
    """Run a short no-token helper container against this run's volumes; returns (rc, stdout, stderr)."""
    name = 'payment-claude-helper-' + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--env', 'HOME=' + HOME_DST, '--env', 'CLAUDE_CONFIG_DIR=' + CONFIG_DST]
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
  # observed surface must be a subset of the allow-list (a tool nobody enumerated is a failure, not a silent leak)
  IFS=',' read -ra OBSERVED <<< "$TOOLS"
  for t in "${OBSERVED[@]}"; do [ -n "$t" ] || continue; case ",$ALLOWED_TOOLS," in *",$t,"*) ;; *) report tool_outside_allowlist "$t"; fail=1;; esac; done
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
    extra = '' if manifest.get('requested_worker_model') else '--tools ' + ','.join(ALLOWED_TOOLS_ARM12)
    script = 'export MODEL=%s HARNESS=%d SETTINGS_SHA=%s MIN_HOOKS=%d DENIED_TOOLS="%s" ALLOWED_TOOLS="%s" WEB_TOOLS="%s" EXTRA_ARGS="%s"\n' % (
        manifest['requested_model'], 1 if harness else 0, settings_sha, MIN_HOOKS, ' '.join(NO_SUBAGENT_TOOLS),
        ','.join(ALLOWED_TOOLS_ARM12), ' '.join(WEB_TOOLS), extra) + SELFTEST
    try:
        rc, out, err = helper(manifest, script, mounts)
    finally:
        os.unlink(expected.name)
    report = {'at': utc(), 'rc': rc, 'stderr_tail': err[-1000:]}
    for line in out.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            if key in ('hook_missing', 'denied_tool_present', 'tool_outside_allowlist', 'web_tool_missing'):
                report.setdefault(key, []).append(value)
            else:
                report.setdefault(key, value)
    report['passed'] = rc == 0 and report.get('selftest_failed') == '0'
    return report

def initialize(args):
    dest = location(args)
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
            'cli_version': CLI_VERSION, 'requested_model': model, 'requested_worker_model': worker,
            'context_variant': 'default', 'subagents': 'opus-high' if worker else 'disabled',
            'actual_model': None, 'effort': 'high', 'session_id': str(uuid.uuid4()),
            'cache_policy': 'observed-cache', 'strict_zero_cache_supported': False,
            'image': args.image, 'config_volume': 'payment-claude-' + uuid.uuid4().hex,
            'reviewer_volume': ('payment-claude-reviewer-' + uuid.uuid4().hex) if args.arm == 3 else None,
            'requested_reviewer_model': REVIEWER_MODEL if args.arm == 3 else None,
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
    if not manifest.get('requested_worker_model'):
        command += ['--tools', ','.join(ALLOWED_TOOLS_ARM12)]              # arms 1/2: allow-list (Codex parity)
    return command

def docker_command(manifest, env_file, name, resume, max_turns=None, max_budget_usd=None):
    # Writable: workspace bind and this run's own state volumes. The token travels only through --env-file
    # (the arm 3 reviewer CLI inherits it from the container environment; no other credential is mounted).
    command = ['docker', 'create', '--name', name, '--interactive', '--init',
               '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=512',
               '--cpus=2', '--memory=3g', '--workdir=/workspace', '--env-file', str(env_file),
               '--env', 'HOME=' + HOME_DST, '--env', 'CLAUDE_CONFIG_DIR=' + CONFIG_DST,
               '--mount', 'type=bind,src=' + manifest['workspace'] + ',dst=/workspace',
               '--mount', 'type=volume,src=' + manifest['config_volume'] + ',dst=' + CONFIG_DST]
    if manifest.get('requested_worker_model'):
        # workers: agreed model and effort high (CLI 2.1.326 reads CLAUDE_CODE_SUBAGENT_EFFORT; propagation is
        # confirmed only by the pilot's per-request transcript models, never assumed)
        command += ['--env', 'CLAUDE_CODE_SUBAGENT_MODEL=' + manifest['requested_worker_model'],
                    '--env', 'CLAUDE_CODE_SUBAGENT_EFFORT=high']
    if manifest.get('arm') == 3:
        if not manifest.get('reviewer_volume') or not manifest.get('requested_reviewer_model'):
            raise ValueError('arm 3 requires a reviewer volume and reviewer model (dual review second reviewer)')
        command += ['--mount', 'type=volume,src=' + manifest['reviewer_volume'] + ',dst=' + REVIEWER_DST,
                    '--env', 'LAB_REVIEWER_MODEL=' + manifest['requested_reviewer_model'],
                    '--env', 'LAB_REVIEWER_CONFIG_DIR=' + REVIEWER_DST]
    if manifest.get('network'):
        command += ['--network', manifest['network'], '--env', 'DB_URL=jdbc:postgresql://db:5432/payment',
                    '--env', 'BANK_BASE_URL=http://bank:8081']
    return command + [manifest['image']] + claude_command(manifest, resume, max_turns, max_budget_usd)

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

def collect(raw):
    """Parse the preserved stream-json; return the final result event and stream statistics."""
    raw = Path(raw)
    summary = {'result': None, 'assistant_messages': 0, 'stream_models': [], 'result_count': 0, 'unparsed_lines': 0}
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
                summary['result'] = {k: event.get(k) for k in ('subtype', 'is_error', 'session_id', 'total_cost_usd',
                                     'usage', 'modelUsage', 'num_turns', 'duration_ms', 'duration_api_ms')}
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
            entry = entries.setdefault(rid, {'usages': [], 'models': set(), 'worker': False, 'files': set(),
                                             'sessions': set(), 'missing': False, 'first': (rel, number), 'timestamp': item.get('timestamp')})
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
                       'transcript_sessions': sorted(entry['sessions']), 'timestamp': entry['timestamp']})
    stats['session_ids'] = sorted(stats['session_ids'])
    return events, sorted(new_ids), incomplete, stats

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

def stream_result_session(result):
    final = (result or {}).get('result') or {}
    return final.get('session_id')

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

def collect_reviewer(name, output, dest, seen, run_id, turn_id, arm):
    """Arm 3: copy the second reviewer's call log and transcripts out of the stopped container and account for every
    attempt made this turn. Reviewer requests become role 'reviewer' events (request-level usage, never mixed into
    the PM's or workers' sums). Returns (events, new_ids, incomplete, summary, calls_seen)."""
    incomplete = []
    log = output / 'reviewer-calls.log'
    calls = subprocess.run(['docker', 'cp', name + ':' + REVIEWER_DST + '/reviewer-calls.log', str(log)],
                           capture_output=True, check=False)
    log_absent = calls.returncode != 0 and b'No such file' in calls.stderr
    log_unknown = calls.returncode != 0 and not log_absent
    started, finished = {}, set()
    for line in (log.read_text().splitlines() if calls.returncode == 0 else []):
        hit = CALL_STARTED.fullmatch(line)
        if hit:
            started[hit.group(1)] = hit.group(2)
        elif CALL_EXIT.fullmatch(line):
            finished.add(CALL_EXIT.fullmatch(line).group(1))
    # The log is cumulative inside the run's reviewer volume; calls already accounted for in earlier turns are skipped.
    ledger = dest / 'reviewer-calls.json'
    recorded = set(json.loads(ledger.read_text())['calls']) if ledger.exists() else set()
    new_calls = sorted(set(started) - recorded)
    copied = subprocess.run(['docker', 'cp', name + ':' + REVIEWER_DST + '/projects', str(output / 'reviewer-projects')],
                            capture_output=True, check=False)
    (output / 'reviewer-copy.stderr').write_bytes(calls.stderr + copied.stderr)
    projects_absent = copied.returncode != 0 and b'No such file' in copied.stderr
    events, new_ids, stats, transcripts = [], [], {}, set()
    if copied.returncode == 0:
        events, new_ids, found, stats = build_events(output / 'reviewer-projects', seen, run_id, turn_id, arm, role='reviewer')
        incomplete += found
        transcripts = {p.stem for p in (output / 'reviewer-projects').rglob('*.jsonl')}
        if stats.get('unparsable_lines') or stats.get('assistant_without_requestId'):
            incomplete.append({'reason': 'reviewer transcript lines without parseable usage or requestId',
                               'unparsable_lines': stats.get('unparsable_lines'),
                               'assistant_without_requestId': stats.get('assistant_without_requestId')})
    elif not projects_absent:
        incomplete.append({'reason': 'reviewer transcript copy failed (docker cp rc=%s)' % copied.returncode})
    if log_unknown:
        incomplete.append({'reason': 'reviewer call log copy failed; reviewer attempts unknown (docker cp rc=%s)' % calls.returncode})
    elif transcripts and not started:
        incomplete.append({'reason': 'reviewer transcripts exist without any logged reviewer call'})
    for call in new_calls:
        if call not in finished:
            incomplete.append({'reason': 'reviewer call did not finish', 'call': call, 'session': started[call]})
        if started[call] not in transcripts:
            incomplete.append({'reason': 'reviewer attempt without transcript (usage unobservable)', 'call': call, 'session': started[call]})
    costs = reviewer_costs(name, output, [started[c] for c in new_calls])
    summary = {'log_rc': calls.returncode, 'log_absent': log_absent, 'log_unknown': log_unknown,
               'calls_total': len(started), 'calls_this_turn': len(new_calls),
               'calls_finished_total': len(finished & set(started)), 'transcript_files': len(transcripts),
               'projects_rc': copied.returncode, 'projects_absent': projects_absent, 'transcript_stats': stats,
               'event_count': len(events), 'incomplete': len(incomplete), **costs}
    return events, new_ids, incomplete, summary, (None if log_unknown else sorted(started))

def reviewer_costs(name, output, sessions):
    """CLI cost estimate of each reviewer call this turn, from the wrapper's raw/<session>.json in the reviewer volume.
    A call whose raw result cannot be recovered or has no numeric total_cost_usd stays listed as unobserved; the turn's
    reviewer cost is then None, never a partial sum presented as complete."""
    raw_dir = output / 'reviewer-raw'
    copied = subprocess.run(['docker', 'cp', name + ':' + REVIEWER_DST + '/raw', str(raw_dir)], capture_output=True, check=False)
    calls, unobserved = {}, []
    for sid in sessions:
        cost = None
        try:
            value = json.loads((raw_dir / (sid + '.json')).read_text()).get('total_cost_usd')
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                cost = value
        except (OSError, ValueError, AttributeError):
            pass
        calls[sid] = cost
        if cost is None:
            unobserved.append(sid)
    return {'raw_rc': copied.returncode, 'call_costs_usd': calls, 'cost_unobserved': unobserved,
            'cost_usd': None if unobserved else round(sum(calls.values()), 6)}

def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

def turn_cost(stream_result, reviewer):
    """CLI cost estimates of one turn: the pinned session's total_cost_usd and (arm 3) the reviewer calls' sum.
    Unobserved parts stay None and make cost_complete false."""
    pinned = number(stream_result.get('total_cost_usd'))
    reviewer_usd = None if reviewer is None else reviewer.get('cost_usd')
    return {'pinned_session_usd': pinned, 'reviewer_usd': reviewer_usd, 'reviewer_applicable': reviewer is not None,
            'cost_complete': pinned is not None and (reviewer is None or reviewer_usd is not None)}

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
    costs, cost_unobserved = [], []
    for kind, ident, data in calls:
        if kind == 'turn':
            cost = data.get('cost') or {}
            parts = [cost.get('pinned_session_usd')] + ([cost.get('reviewer_usd')] if cost.get('reviewer_applicable') else [])
        else:
            parts = [data.get('cost_usd')]
        if any(number(v) is None for v in parts):
            cost_unobserved.append('%s/%s' % (kind, ident))
        else:
            costs += parts
    complete = (not conflicts and not unreadable and all(data.get('usage_complete') is True for _, _, data in calls))
    return {
        'roles': {role: {'requests': len(evs), 'usage': sum_usage(evs), 'total_tokens': total_tokens(sum_usage(evs))}
                  for role, evs in sorted(roles.items())},
        'total_tokens': total_tokens(sum_usage(events)), 'usage_complete': complete, 'tokens_are_lower_bound': not complete,
        'incomplete_calls': ['%s/%s' % (k, i) for k, i, d in calls if d.get('usage_complete') is not True] + unreadable,
        'conflicts': conflicts, 'turns': sum(1 for k, _, _ in calls if k == 'turn'),
        'consults': sum(1 for k, _, _ in calls if k == 'consult'),
        'cost_usd': None if cost_unobserved else round(sum(costs), 6), 'cost_unobserved': cost_unobserved,
        'cost_note': 'CLI total_cost_usd estimates (subscription: billed_cost null); whether a resumed session reports '
                     'per-invocation or cumulative cost is not verified before the pilot',
        'updated_at': utc()}

def write_ledger(dest):
    value = run_ledger(dest)
    tmp = dest / 'ledger.json.tmp'
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(dest / 'ledger.json')
    return value

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
    if args.dry_run:
        exists = bool(successes)
    else:
        exists = session_exists(manifest)       # a failed turn may still have persisted the session
    if args.resume != exists:
        raise ValueError('first turn requires no --resume; subsequent turns require --resume '
                         '(session transcript %s in the config volume; %d turn dirs, %d successful)'
                         % ('present' if exists else 'absent', len(turn_dirs), len(successes)))
    seen = read_seen(dest)
    turn_id = f'{len(turn_dirs) + 1:04d}'   # failed turns keep their directory and number; retries get a new one
    name = 'payment-claude-call-' + uuid.uuid4().hex
    command = docker_command(manifest, env_file, name, args.resume, args.max_turns, args.max_budget_usd)
    prompt = Path(args.prompt).read_bytes()
    if args.dry_run:
        print(json.dumps({'create': command, 'start': ['docker', 'start', '--attach', '--interactive', name],
                          'prompt_bytes': len(prompt), 'session_id': manifest['session_id'], 'resume': args.resume}, indent=2))
        return
    if args.cache_policy != 'observed-cache':
        raise ValueError('explicit --cache-policy observed-cache required; strict cache control unavailable')
    pre = selftest(manifest) if manifest.get('harness') else None
    if pre and not pre['passed']:
        raise RuntimeError('hook self-test failed before the call: ' + json.dumps(pre, ensure_ascii=False))
    output = dest / 'turns' / turn_id
    output.mkdir()
    (output / 'prompt.txt').write_bytes(prompt)
    write_json(output / 'invocation.json', {'create': command, 'session_id': manifest['session_id'], 'resume': args.resume,
        'prompt_sha256': hashlib.sha256(prompt).hexdigest(), 'cache_policy': args.cache_policy,
        'env_file': str(env_file), 'started_at': utc(), 'timeout_seconds': args.timeout_seconds,
        'max_turns': args.max_turns, 'max_budget_usd': args.max_budget_usd, 'hook_selftest_before_call': pre})
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
                                      stdout=stdout, stderr=stderr, check=False, timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                subprocess.run(['docker', 'stop', '--time', '5', name], capture_output=True, check=False)
                proc = SimpleNamespace(returncode=124)
        inspected = subprocess.run(['docker', 'inspect', name], capture_output=True, check=False)
        (output / 'container-inspect.json').write_bytes(redact_inspect(inspected.stdout))
        copied = subprocess.run(['docker', 'cp', name + ':' + CONFIG_DST + '/projects', str(output / 'projects')],
                                capture_output=True, check=False)
        (output / 'projects-copy.stderr').write_bytes(copied.stderr)
        reviewer = None
        if manifest.get('arm') == 3:
            r_events, r_ids, r_incomplete, reviewer, reviewer_calls = collect_reviewer(
                name, output, dest, seen, args.run_id, turn_id, manifest['arm'])
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)
        result = collect(output / 'stdout.jsonl')
        events, new_ids, incomplete, stats = [], [], [{'reason': 'transcript copy failed (docker cp rc=%s)' % copied.returncode}], {}
        if copied.returncode == 0 and (output / 'projects').exists():
            events, new_ids, incomplete, stats = build_events(output / 'projects', seen, args.run_id, turn_id, manifest['arm'])
        if reviewer is not None:
            events, new_ids, incomplete = events + r_events, sorted(new_ids + r_ids), incomplete + r_incomplete
        with (output / 'events.jsonl').open('x') as stream:
            for event in events:
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        if stream_result_session(result) not in (None, manifest['session_id']):
            incomplete.append({'reason': 'result session_id differs from the pinned session (forked or new session)',
                               'observed': stream_result_session(result)})
        if manifest['arm'] != 3:
            leaked = [e['id'] for e in events if e['role'] == 'worker']
            if leaked:
                incomplete.append({'reason': 'sidechain request in an arm that forbids subagents', 'requestIds': leaked})
        stream_result = result['result'] or {}
        reported = stream_result.get('usage') if isinstance(stream_result.get('usage'), dict) else None
        # The stream result covers the pinned session only: reviewer sessions are separate CLI processes and never part of it.
        # A sum over transcripts that could not be read is unknown, not zero (None); with incomplete requests the
        # sums cover only the observed requests (sums_are_lower_bounds).
        main_seen = copied.returncode == 0 and (output / 'projects').exists()
        reviewer_seen = reviewer is not None and not reviewer['log_unknown'] and (reviewer['projects_rc'] == 0 or reviewer['projects_absent'])
        main_sum = sum_usage([e for e in events if e['role'] not in ('worker', 'reviewer')]) if main_seen else None
        all_sum = sum_usage([e for e in events if e['role'] != 'reviewer']) if main_seen else None
        cross = {'reported': reported, 'main_sum': main_sum, 'all_sum': all_sum, 'main_delta': None, 'all_delta': None,
                 'reviewer_sum': sum_usage([e for e in events if e['role'] == 'reviewer']) if reviewer_seen else None,
                 'sums_are_lower_bounds': bool(incomplete),
                 'note': 'observation only until the smoke fixes whether result.usage includes sidechains; reviewer_sum is '
                         'outside reported (None in arms 1/2: no reviewer; None in arm 3: reviewer log/transcripts unreadable)'}
        if main_seen and reported and all(isinstance(reported.get(k), int) for k in USAGE_KEYS):
            cross['main_delta'] = {k: reported[k] - main_sum[k] for k in USAGE_KEYS}
            cross['all_delta'] = {k: reported[k] - all_sum[k] for k in USAGE_KEYS}
        result.update({
            'exit_code': proc.returncode, 'finished_at': utc(), 'session_id': manifest['session_id'],
            'session_id_confirmed': stream_result.get('session_id') == manifest['session_id'],
            'request_ids': new_ids, 'event_count': len(events), 'incomplete': incomplete, 'transcript_stats': stats,
            'reviewer': reviewer,
            'cost': turn_cost(stream_result, reviewer),
            'reviewer_usage_pending': bool(reviewer is not None and (r_incomplete or reviewer['log_unknown'])),
            'web_requests': reported.get('server_tool_use') if reported else None,
            'usage_complete': (not incomplete and result['result'] is not None and proc.returncode == 0
                               and copied.returncode == 0 and bool(events)
                               and stats.get('unparsable_lines', 1) == 0 and result['unparsed_lines'] == 0
                               and stats.get('assistant_without_requestId', 1) == 0
                               and cross['main_delta'] is not None
                               and (all(v == 0 for v in cross['main_delta'].values()) or all(v == 0 for v in cross['all_delta'].values()))),
            'usage_complete_note': 'arm 3: every reviewer attempt this turn must have finished and left a transcript whose requests all carry usage (see reviewer / incomplete)',
            'usage_cross_check': cross, 'role_files': write_role_files(output, events),
            'transcript_models': sorted({e['model'] for e in events}), 'roles': sorted({e['role'] for e in events}),
            'config_volume': volume_listing(manifest), 'actual_model': None,
            'actual_model_note': 'transcript models/modelUsage are client-observed; requested model is not proof.',
        })
        result['cli_status'] = 'ok' if proc.returncode == 0 else 'failed'
        result['status'] = 'collected'                 # finalized in the finally block after the secret scan
        write_json(output / 'result.json', result)
        # Only after the result is durable: fully accounted requests enter the ledger (incomplete ones stay open).
        with (dest / 'request-ids.txt').open('a') as ledger:
            for event in events:
                ledger.write(event['id'] + '\n')
        if reviewer is not None and reviewer_calls is not None:
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
        finally:
            if (hits or scan_error) and in_flight is None:
                raise RuntimeError('credential leaked into preserved files or the scan failed; see result.json / SECRET-LEAK.txt')

def discard(args):
    """Record what the run's volumes hold (names only), then remove the volumes. The run directory stays."""
    dest = location(args)
    manifest = json.loads((dest / 'manifest.json').read_text())
    record = {'at': utc(), 'volumes': {}}
    for key in ('config_volume', 'reviewer_volume', 'codex_volume'):   # codex_volume: manifests created before v3-claude
        volume = manifest.get(key)
        if not volume:
            continue
        rc, out, err = helper(manifest, "find /vol -printf '%y %p\\n' | sort", ['type=volume,src=%s,dst=/vol' % volume])
        removed = subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True, text=True, check=False)
        record['volumes'][volume] = {'listing_rc': rc, 'entries': out.splitlines() if rc == 0 else None,
                                     'removed': removed.returncode == 0, 'stderr': removed.stderr[-500:]}
    write_json(dest / ('discard-%s.json' % utc().replace(':', '-')), record)
    print(json.dumps({v: r['removed'] for v, r in record['volumes'].items()}))

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
    init.add_argument('--image', default='payment-claude-runner:' + CLI_VERSION)
    run = sub.add_parser('turn')
    run.add_argument('--run-id', required=True)
    run.add_argument('--prompt', type=Path, required=True)
    run.add_argument('--env-file', type=Path, required=True, help='0600 file outside the repo with only CLAUDE_CODE_OAUTH_TOKEN=...')
    run.add_argument('--timeout-seconds', type=int, default=7200)
    run.add_argument('--max-turns', type=int)
    run.add_argument('--max-budget-usd', type=float)
    run.add_argument('--resume', action='store_true')
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
