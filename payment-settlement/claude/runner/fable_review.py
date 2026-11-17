#!/usr/bin/env python3
"""Arm 3 second reviewer (protocol payment-settlement-v3-claude): a helper under its own user account and the PM-side
client that `fable-review` runs.

  serve    started once per container by lab-arm3-start (as root, before the PM) under the reviewer account. It alone
           holds the subscription token (in its own environment; another uid cannot read /proc/<pid>/environ). It
           listens on a local socket and, per request, runs ONE fixed reviewer command: a fresh `claude -p` session on
           the reviewer model, effort high, read-only tools (Read, Grep, Glob), its own CLAUDE_CONFIG_DIR. Model, tools,
           effort and config dir are fixed at start; a request carries only the working directory and the prompt.
  request  the PM side (fable-review): prompt on stdin, the request's working directory = the caller's cwd, review text
           on stdout, the helper's diagnostics on stderr, exit code of the call.

Properties kept from the former in-shell wrapper: every attempt is logged with its pinned session id BEFORE the CLI
starts (a logging failure refuses the call: exit 97); an empty, error or unreadable result is a failure (exit 96), never
a silent success; the raw result of every session is kept in <config dir>/raw/<session>.json; the reviewer's
transcripts live in its own config dir so the runner attributes their usage to the reviewer role.
Exit codes of the client: 2 arguments / unusable prompt, 94 request refused by the helper, 95 helper unreachable or
broken reply, 98 no reviewer in this run (no socket: arms 1/2).
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import uuid

SOCKET = '/run/fable-review/socket'
TOOLS = 'Read,Grep,Glob'
MAX_REQUEST = 32 * 1024 * 1024                 # bytes of one JSON request (prompt included)
CHILD_PATH = '/usr/local/bin:/usr/bin:/bin'


def log(message):
    print('[fable-review-helper] %s %s' % (datetime.now(timezone.utc).isoformat(), message), file=sys.stderr, flush=True)


def read_all(conn, limit):
    chunks, size = [], 0
    while True:
        chunk = conn.recv(1 << 16)
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > limit:
            raise ValueError('request larger than %d bytes' % limit)
        chunks.append(chunk)


def peer_uid(conn):
    data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', data)[1]


def validate(raw, roots):
    """The only accepted request: {"cwd": <existing directory under an allowed root>, "prompt": <non-empty text>}.
    Returns (cwd, prompt bytes) or raises ValueError naming the refusal."""
    try:
        request = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('request is not UTF-8 JSON')
    if not isinstance(request, dict) or set(request) != {'cwd', 'prompt'}:
        raise ValueError('a request carries exactly "cwd" and "prompt"')
    cwd, prompt = request['cwd'], request['prompt']
    if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd:
        raise ValueError('cwd must be an absolute path')
    real = os.path.realpath(cwd)
    if not any(real == root or real.startswith(root.rstrip('/') + '/') for root in roots):
        raise ValueError('cwd %s is outside %s' % (real, ', '.join(roots)))
    if not os.path.isdir(real):
        raise ValueError('cwd %s is not a directory' % real)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt must be non-empty text')
    return real, prompt.encode('utf-8')


class Reviewer:
    def __init__(self, model, config_dir, home, cli, token):
        self.model, self.dir, self.home, self.cli, self.token = model, Path(config_dir), home, cli, token
        self.lock = threading.Lock()
        self.count = 0

    def call_id(self):
        with self.lock:
            self.count += 1
            return '%s-%d-%d' % (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), os.getpid(), self.count)

    def append(self, line):
        with self.lock, (self.dir / 'reviewer-calls.log').open('a') as stream:
            stream.write(line + '\n')

    def run(self, cwd, prompt):
        """One reviewer attempt. Returns (exit code, stdout text, stderr text)."""
        sid, call = str(uuid.uuid4()), self.call_id()
        try:
            (self.dir / 'raw').mkdir(parents=True, exist_ok=True)
            self.append('%s started session=%s model=%s' % (call, sid, self.model))
        except OSError as exc:
            return 97, '', ('[fable-review] cannot record the attempt in %s/reviewer-calls.log; refusing to launch (usage '
                            'would be unobservable): %s\n' % (self.dir, exc))
        raw = self.dir / 'raw' / (sid + '.json')
        env = {'PATH': CHILD_PATH, 'HOME': self.home, 'CLAUDE_CONFIG_DIR': str(self.dir), 'DISABLE_AUTOUPDATER': '1',
               'CLAUDE_CODE_OAUTH_TOKEN': self.token, 'LANG': 'C.UTF-8',
               # the reviewed files belong to the PM's user: let git inside the reviewer read them without ownership errors
               'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'safe.directory', 'GIT_CONFIG_VALUE_0': '*'}
        command = [self.cli, '-p', '--model', self.model, '--effort', 'high', '--session-id', sid,
                   '--tools', TOOLS, '--output-format', 'json']
        try:
            with raw.open('wb') as out:
                done = subprocess.run(command, input=prompt, stdout=out, stderr=subprocess.PIPE, cwd=cwd, env=env, check=False)
            rc, err = done.returncode, done.stderr.decode('utf-8', 'replace')
        except OSError as exc:
            rc, err = 127, '[fable-review] reviewer CLI could not be started: %s\n' % exc
        try:
            data = raw.read_bytes()
        except OSError:
            data = b''
        text, readable = None, False
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict) and not parsed.get('is_error'):
                text, readable = parsed.get('result') or '', True
        except ValueError:
            pass
        if rc == 0 and (not readable or not (text or '').strip()):
            err += ('[fable-review] the reviewer returned no review text (empty, error or unreadable result); raw output '
                    'follows\n' + data.decode('utf-8', 'replace'))
            rc = 96
        try:
            self.append('%s exit=%s' % (call, rc))
        except OSError as exc:
            log('cannot record the exit of %s: %s' % (call, exc))
        out_text = (text + '\n') if readable else data.decode('utf-8', 'replace')
        return rc, out_text, err


def serve(args):
    token = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN', '')
    if not token:
        log('no token in the helper environment; not serving')
        return 90
    reviewer = Reviewer(args.model, args.config_dir, args.home, args.cli, token)
    roots = args.root

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            conn = self.request
            try:
                raw = read_all(conn, MAX_REQUEST)
                uid = peer_uid(conn)
                if args.peer_uid is not None and uid != args.peer_uid:
                    raise ValueError('requests are accepted only from uid %d (got %d)' % (args.peer_uid, uid))
                cwd, prompt = validate(raw, roots)
            except (ValueError, OSError) as exc:
                log('refused: %s' % exc)
                reply = {'exit': 94, 'stdout': '', 'stderr': '[fable-review] request refused: %s\n' % exc}
            else:
                rc, out, err = reviewer.run(cwd, prompt)
                log('call in %s exit=%s' % (cwd, rc))
                reply = {'exit': rc, 'stdout': out, 'stderr': err}
            try:
                conn.sendall(json.dumps(reply).encode('utf-8'))
            except OSError as exc:                    # the caller went away; the attempt is logged either way
                log('reply not delivered: %s' % exc)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    path = Path(args.socket)
    if path.exists():
        path.unlink()
    server = Server(str(path), Handler)
    os.chmod(path, 0o666)
    log('ready model=%s config_dir=%s roots=%s' % (args.model, args.config_dir, ','.join(roots)))
    server.serve_forever()
    return 0


def request(args):
    path = os.environ.get('FABLE_REVIEW_SOCKET') or SOCKET
    if not os.path.exists(path):
        print('[fable-review] no reviewer is configured in this run; refusing', file=sys.stderr)
        return 98
    try:
        prompt = sys.stdin.buffer.read().decode('utf-8')
    except UnicodeDecodeError:
        print('[fable-review] the review prompt on stdin must be UTF-8 text', file=sys.stderr)
        return 2
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(path)
            conn.sendall(json.dumps({'cwd': os.getcwd(), 'prompt': prompt}).encode('utf-8'))
            conn.shutdown(socket.SHUT_WR)
            reply = json.loads(read_all(conn, 1 << 30).decode('utf-8'))
        code, out, err = reply['exit'], reply['stdout'], reply['stderr']
        if not isinstance(code, int) or not isinstance(out, str) or not isinstance(err, str):
            raise ValueError('malformed reply')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('[fable-review] reviewer helper unreachable or broken reply: %s' % exc, file=sys.stderr)
        return 95
    sys.stderr.write(err)
    sys.stdout.write(out)
    sys.stdout.flush()
    return code


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='mode', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--socket', default=SOCKET)
    s.add_argument('--model', required=True)
    s.add_argument('--config-dir', required=True)
    s.add_argument('--home', required=True)
    s.add_argument('--cli', default='/usr/local/bin/claude')
    s.add_argument('--root', action='append', required=True, help='allowed working-directory root (repeat)')
    s.add_argument('--peer-uid', type=int, help='accept requests only from this uid')
    sub.add_parser('request')
    args = p.parse_args(argv)
    return serve(args) if args.mode == 'serve' else request(args)


if __name__ == '__main__':
    sys.exit(main())
