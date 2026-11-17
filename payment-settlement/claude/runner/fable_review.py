#!/usr/bin/env python3
"""Arm 3 second reviewer (protocol payment-settlement-v3-claude): a helper under its own user account and the PM-side
client that `fable-review` runs.

  serve    started once per container by lab-arm3-start (as root, before the PM) under the reviewer account. It reads
           the subscription token once from an inherited descriptor (--token-fd) and keeps it in memory only: neither
           its environment nor any file holds it. Per request it runs ONE fixed reviewer command: a fresh `claude -p`
           session on the reviewer model, effort high, read-only tools (Read, Grep, Glob), its own CLAUDE_CONFIG_DIR,
           user settings only and no MCP servers, without any capability (the helper keeps DAC_READ_SEARCH only to copy
           the PM's 0700 mirror, and SETPCAP only to empty the CLI's bounding set); the token reaches that CLI through a pipe
           (CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR), never through its environment. Model, tools, effort and config dir
           are fixed at start; a request carries only the working directory and the prompt.
           The CLI does not run in the requested directory: the helper copies it into a snapshot it owns (regular files
           and directories only, opened without following symbolic links; .git, .claude and .mcp.json left out; a
           symbolic link or special file, or more than MAX_FILES files / MAX_BYTES bytes, refuses the request) and
           runs the CLI there, read-only. The snapshot is removed after the call. So nothing the PM placed in the
           directory (project settings, hooks, MCP servers, git configuration) is executed with the token's account.
           If the caller disconnects before the review is done (e.g. the PM's Bash timed out), the CLI is stopped and
           the call is logged as cancelled: its usage is unobserved (requests may have gone out), never zero.
  request  the PM side (fable-review): prompt on stdin, the request's working directory = the caller's cwd, review text
           on stdout, the helper's diagnostics on stderr, exit code of the call.

Properties kept from the former in-shell wrapper: every attempt is logged with its pinned session id BEFORE the CLI
starts (a logging failure refuses the call: exit 97); an empty, error or unreadable result is a failure (exit 96), never
a silent success; the raw result of every session is kept in <config dir>/raw/<session>.json; the reviewer's
transcripts live in its own config dir so the runner attributes their usage to the reviewer role.
Exit codes of the client: 2 arguments / unusable prompt, 94 request refused by the helper, 95 helper unreachable or
broken reply, 98 no reviewer in this run (no socket: arms 1/2).
Wire format: 8-byte big-endian length + UTF-8 JSON request; the client keeps the connection open until the reply
(JSON, until EOF) arrives — closing it earlier cancels the call. A connection closed without any byte is the start
script's readiness probe and is not recorded. Every other request that never reaches the CLI (malformed, wrong uid,
path refused, snapshot refused, caller gone before the CLI started) is recorded as '<call> refused reason=…' (no
session, no usage); only the requester's own readable files are ever copied.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import uuid

SOCKET = '/run/fable-review/socket'
TOOLS = 'Read,Grep,Glob'
MAX_REQUEST = 32 * 1024 * 1024                 # bytes of one JSON request (prompt included)
MAX_FILES = 20000                              # snapshot limits (a review mirror is a worktree copy)
MAX_BYTES = 256 * 1024 * 1024
EXCLUDED = ('.git', '.claude', '.mcp.json')    # project configuration a CLI or git would act on
CHILD_PATH = '/usr/local/bin:/usr/bin:/bin'
TOKEN_ENV_FD = 'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR'


def log(message):
    print('[fable-review-helper] %s %s' % (datetime.now(timezone.utc).isoformat(), message), file=sys.stderr, flush=True)


def recv_exact(conn, size):
    data = b''
    while len(data) < size:
        chunk = conn.recv(min(1 << 16, size - len(data)))
        if not chunk:
            raise ValueError('connection closed before the request was complete')
        data += chunk
    return data


def read_request(conn):
    size = struct.unpack('>Q', recv_exact(conn, 8))[0]
    if size > MAX_REQUEST:
        raise ValueError('request larger than %d bytes' % MAX_REQUEST)
    return recv_exact(conn, size)


def peer_uid(conn):
    data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', data)[1]


def validate(raw, roots):
    """The only accepted request: {"cwd": <absolute, normalized path under an allowed root>, "prompt": <non-empty
    text>}. Returns (cwd, prompt bytes) or raises ValueError naming the refusal. Whether the path holds symbolic links is
    decided when it is opened (open_dir), not here."""
    try:
        request = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('request is not UTF-8 JSON')
    if not isinstance(request, dict) or set(request) != {'cwd', 'prompt'}:
        raise ValueError('a request carries exactly "cwd" and "prompt"')
    cwd, prompt = request['cwd'], request['prompt']
    if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd:
        raise ValueError('cwd must be an absolute path')
    if os.path.normpath(cwd) != cwd or '..' in cwd.split('/'):
        raise ValueError('cwd %s is not a normalized path (no "..", "." or "//")' % cwd)
    if not any(cwd.startswith(root.rstrip('/') + '/') or cwd == root for root in roots):
        raise ValueError('cwd %s is outside %s' % (cwd, ', '.join(roots)))
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt must be non-empty text')
    return cwd, prompt.encode('utf-8')


def requester_may(st, uid, directory):
    """Whether the requester could read this itself: it owns it, or others may read it (a directory: read and
    search). The helper's DAC_READ_SEARCH is never lent to the requester."""
    if st.st_uid == uid:
        return True
    need = (stat.S_IROTH | stat.S_IXOTH) if directory else stat.S_IROTH
    return st.st_mode & need == need


def open_dir(path, uid=None):
    """Open path component by component from / without following any symbolic link; returns a directory fd. With uid
    (the requester): every directory on the way must be searchable by the requester and the requested directory
    readable and searchable by it."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = [p for p in path.split('/') if p]
        for i, part in enumerate(parts):
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise ValueError('cwd %s cannot be opened without following links (%s at %r)' % (path, exc.strerror, part))
            os.close(fd)
            fd = nxt
            if uid is not None:
                st = os.fstat(fd)
                last = i == len(parts) - 1
                ok = requester_may(st, uid, True) if last else (st.st_uid == uid or st.st_mode & stat.S_IXOTH)
                if not ok:
                    raise ValueError('the requester cannot %s %s itself' % ('read' if last else 'enter', '/' + '/'.join(parts[:i + 1])))
        return fd
    except BaseException:
        os.close(fd)
        raise


def snapshot(src_fd, dest, uid=None):
    """Copy the directory open at src_fd into dest (created, owned by this account): regular files and directories
    only, never through a symbolic link, never onto another file system (st_dev of the requested directory), and with
    uid only what the requester could read itself (requester_may, checked on the opened file or directory, so a hard
    link is judged by its inode). EXCLUDED names are left out. Raises ValueError naming the first refusal (a link, a
    special file, another file system, a file the requester cannot read, a limit) — nothing is left out silently.
    Returns {'files', 'bytes', 'excluded'}."""
    totals = {'files': 0, 'bytes': 0, 'excluded': []}
    device = os.fstat(src_fd).st_dev

    def check(st, path, directory):
        if st.st_dev != device:
            raise ValueError('another file system (mount point) in the review directory: %s' % path)
        if uid is not None and not requester_may(st, uid, directory):
            raise ValueError('the requester cannot read %s itself: %s' % ('the directory' if directory else 'the file', path))

    def walk(fd, target, rel):
        os.mkdir(target, 0o700)
        with os.scandir(fd) as entries:
            names = sorted(e.name for e in entries)
        for name in names:
            path = rel + '/' + name if rel else name
            if name in EXCLUDED:
                totals['excluded'].append(path)
                continue
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise ValueError('symbolic link in the review directory: %s' % path)
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    check(os.fstat(child), path, True)
                    walk(child, os.path.join(target, name), path)
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode):
                totals['files'] += 1
                if totals['files'] > MAX_FILES:
                    raise ValueError('review directory holds more than %d files' % MAX_FILES)
                src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    opened = os.fstat(src)
                    if not stat.S_ISREG(opened.st_mode):
                        raise ValueError('not a regular file when opened: %s' % path)
                    check(opened, path, False)
                    with open(src, 'rb', closefd=False) as reader, open(os.path.join(target, name), 'xb') as writer:
                        while True:
                            chunk = reader.read(1 << 20)
                            if not chunk:
                                break
                            totals['bytes'] += len(chunk)
                            if totals['bytes'] > MAX_BYTES:
                                raise ValueError('review directory holds more than %d bytes' % MAX_BYTES)
                            writer.write(chunk)
                finally:
                    os.close(src)
            else:
                raise ValueError('special file in the review directory: %s' % path)

    walk(src_fd, dest, '')
    for root, dirs, files in os.walk(dest, topdown=False):           # read-only like the PM's mirror
        for name in files:
            os.chmod(os.path.join(root, name), 0o444)
        os.chmod(root, 0o555)
    return totals


def remove_tree(path):
    if not os.path.lexists(path):
        return
    for root, dirs, files in os.walk(path):
        os.chmod(root, 0o700)
    shutil.rmtree(path)


class Reviewer:
    def __init__(self, model, config_dir, home, cli, token, setpriv='/usr/bin/setpriv'):
        self.model, self.dir, self.home, self.cli, self.token = model, Path(config_dir), home, cli, token
        self.setpriv = setpriv
        self.lock = threading.Lock()
        self.count = 0

    def call_id(self):
        with self.lock:
            self.count += 1
            return '%s-%d-%d' % (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), os.getpid(), self.count)

    def append(self, line):
        with self.lock, (self.dir / 'reviewer-calls.log').open('a') as stream:
            stream.write(line + '\n')

    def refused(self, reason):
        """Record a request that never reached the CLI (no session, no usage): '<call> refused reason=<text>'."""
        call = self.call_id()
        try:
            self.append('%s refused reason=%s' % (call, ' '.join(str(reason).split())[:300]))
        except OSError as exc:
            log('cannot record the refusal %s: %s' % (call, exc))

    def run(self, src_fd, prompt, cancelled, uid=None):
        """One reviewer attempt on a snapshot of the directory open at src_fd. Returns (exit code, stdout, stderr), or
        None when cancelled() became true before the CLI finished (the CLI is stopped and the call logged as cancelled)."""
        sid = str(uuid.uuid4())
        work = os.path.join(self.home, 'snapshots', sid)
        os.makedirs(os.path.dirname(work), mode=0o700, exist_ok=True)
        try:
            os.mkdir(work, 0o700)
            cwd = os.path.join(work, 'review')
            try:
                copied = snapshot(src_fd, cwd, uid)
            except (ValueError, OSError) as exc:
                self.refused('snapshot: %s' % exc)
                return 94, '', '[fable-review] request refused: the review directory cannot be snapshotted: %s\n' % exc
            log('snapshot %s files=%d bytes=%d excluded=%s' % (sid, copied['files'], copied['bytes'], copied['excluded']))
            if cancelled():                             # the caller left while the snapshot was made: no CLI
                self.refused('client disconnected before the reviewer started')
                log('caller disconnected during the snapshot %s; CLI not started' % sid)
                return None
            return self.call(sid, cwd, prompt, cancelled)
        finally:
            try:
                remove_tree(work)
            except OSError as exc:
                log('snapshot %s not removed: %s' % (sid, exc))

    def call(self, sid, cwd, prompt, cancelled):
        call = self.call_id()
        try:
            (self.dir / 'raw').mkdir(parents=True, exist_ok=True)
            self.append('%s started session=%s model=%s' % (call, sid, self.model))
        except OSError as exc:
            return 97, '', ('[fable-review] cannot record the attempt in %s/reviewer-calls.log; refusing to launch (usage '
                            'would be unobservable): %s\n' % (self.dir, exc))
        raw = self.dir / 'raw' / (sid + '.json')
        read_end, write_end = os.pipe()                 # the token travels through a pipe the CLI reads once
        os.write(write_end, self.token.encode() + b'\n')
        os.close(write_end)
        env = {'PATH': CHILD_PATH, 'HOME': self.home, 'CLAUDE_CONFIG_DIR': str(self.dir), 'DISABLE_AUTOUPDATER': '1',
               TOKEN_ENV_FD: str(read_end), 'LANG': 'C.UTF-8'}
        # the CLI runs without any capability (bounding, inheritable and ambient emptied): only the helper needs
        # DAC_READ_SEARCH, to copy the PM's mirror; the reviewer reads nothing but its snapshot
        command = [self.setpriv, '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', self.cli, '-p', '--model', self.model, '--effort', 'high', '--session-id', sid,
                   '--tools', TOOLS, '--setting-sources', 'user', '--strict-mcp-config', '--output-format', 'json']
        err_path = os.path.join(os.path.dirname(cwd), 'stderr.txt')
        try:
            with raw.open('wb') as out, open(err_path, 'wb') as err:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=cwd, env=env,
                                        pass_fds=(read_end,), start_new_session=True)
                os.close(read_end)
                read_end = None
                feeder = threading.Thread(target=self.feed, args=(proc, prompt), daemon=True)
                feeder.start()
                while proc.poll() is None:
                    if cancelled():
                        self.stop(proc)
                        self.append('%s cancelled session=%s reason=client-disconnected' % (call, sid))
                        log('call %s cancelled: client disconnected; CLI stopped' % call)
                        return None
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
                rc = proc.returncode
            with open(err_path, 'rb') as stream:
                err_text = stream.read().decode('utf-8', 'replace')
        except OSError as exc:
            rc, err_text = 127, '[fable-review] reviewer CLI could not be started: %s\n' % exc
        finally:
            if read_end is not None:
                os.close(read_end)
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
            err_text += ('[fable-review] the reviewer returned no review text (empty, error or unreadable result); raw '
                         'output follows\n' + data.decode('utf-8', 'replace'))
            rc = 96
        try:
            self.append('%s exit=%s' % (call, rc))
        except OSError as exc:
            log('cannot record the exit of %s: %s' % (call, exc))
        return rc, ((text + '\n') if readable else data.decode('utf-8', 'replace')), err_text

    @staticmethod
    def feed(proc, prompt):
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

    @staticmethod
    def stop(proc):
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue


def read_token(fd):
    chunks = []
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b'\n' in chunk:
            break
    os.close(fd)
    return b''.join(chunks).split(b'\n')[0].decode().strip()


def serve(args):
    global MAX_FILES, MAX_BYTES
    MAX_FILES, MAX_BYTES = args.max_files, args.max_bytes
    try:
        token = read_token(args.token_fd)
    except OSError as exc:
        log('token descriptor %s unreadable (%s); not serving' % (args.token_fd, exc))
        return 90
    if not token:
        log('no token on the descriptor; not serving')
        return 90
    if 'CLAUDE_CODE_OAUTH_TOKEN' in os.environ:
        log('a token in the helper environment is refused (it would be readable by the reviewer); not serving')
        return 90
    reviewer = Reviewer(args.model, args.config_dir, args.home, args.cli, token, args.setpriv)
    roots = args.root

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            conn = self.request
            src_fd = None
            try:
                head = conn.recv(8, socket.MSG_PEEK)
            except OSError:
                head = b''
            if not head:                                  # a connection without any byte is a readiness probe, not a request
                return
            try:
                raw = read_request(conn)
                uid = peer_uid(conn)
                if args.peer_uid is not None and uid != args.peer_uid:
                    raise ValueError('requests are accepted only from uid %d (got %d)' % (args.peer_uid, uid))
                cwd, prompt = validate(raw, roots)
                src_fd = open_dir(cwd, args.peer_uid)
            except (ValueError, OSError, struct.error) as exc:
                log('refused: %s' % exc)
                reviewer.refused(exc)
                reply = {'exit': 94, 'stdout': '', 'stderr': '[fable-review] request refused: %s\n' % exc}
            else:
                try:
                    done = reviewer.run(src_fd, prompt, lambda: disconnected(conn), args.peer_uid)
                finally:
                    os.close(src_fd)
                if done is None:
                    return
                rc, out, err = done
                log('call in %s exit=%s' % (cwd, rc))
                reply = {'exit': rc, 'stdout': out, 'stderr': err}
            try:
                conn.sendall(json.dumps(reply).encode('utf-8'))
            except OSError as exc:                    # the caller went away; the attempt is logged either way
                log('reply not delivered: %s' % exc)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    final = Path(args.socket)
    staging = final.with_name('.%s.%d' % (final.name, os.getpid()))
    for path in (staging, final):
        if path.exists():
            path.unlink()
    server = Server(str(staging), Handler)            # bind and open permissions under a private name, then publish
    os.chmod(staging, 0o666)
    os.rename(staging, final)
    log('ready model=%s config_dir=%s roots=%s' % (args.model, args.config_dir, ','.join(roots)))
    server.serve_forever()
    return 0


def disconnected(conn):
    """The client keeps its end open until the reply; any readability (EOF, reset or extra bytes) means it went away."""
    ready, _, _ = select.select([conn], [], [], 0)
    return bool(ready)


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
            body = json.dumps({'cwd': os.getcwd(), 'prompt': prompt}).encode('utf-8')
            conn.sendall(struct.pack('>Q', len(body)) + body)
            chunks = []
            while True:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
            reply = json.loads(b''.join(chunks).decode('utf-8'))
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
    s.add_argument('--home', required=True, help='the helper account home; snapshots live under <home>/snapshots')
    s.add_argument('--cli', default='/usr/local/bin/claude')
    s.add_argument('--setpriv', default='/usr/bin/setpriv', help=argparse.SUPPRESS)   # tests only (a recording stand-in)
    s.add_argument('--root', action='append', required=True, help='allowed working-directory root (repeat)')
    s.add_argument('--peer-uid', type=int, help='accept requests only from this uid')
    s.add_argument('--token-fd', type=int, required=True, help='descriptor the token is read from once, then closed')
    s.add_argument('--max-files', type=int, default=MAX_FILES, help=argparse.SUPPRESS)    # tests only; the start
    s.add_argument('--max-bytes', type=int, default=MAX_BYTES, help=argparse.SUPPRESS)    # script never sets them
    sub.add_parser('request')
    args = p.parse_args(argv)
    return serve(args) if args.mode == 'serve' else request(args)


if __name__ == '__main__':
    sys.exit(main())
