#!/usr/bin/env python3
"""Fresh isolated Claude user proxy: frozen contract + questions only, no tools, its own session and config.

The proxy model is an explicit argument (no default): the experiment has not fixed it yet. When readiness.json
records user_proxy_model, the argument must match it.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runner'))
import launch

CONTRACT = ('requirements.md', 'resources.md', 'acceptance.md', 'answer-policy.md')
INSTRUCTION = ('당신은 구현자와 분리된 대리 사용자입니다. 제공한 계약만 근거로 질문에 답하세요. 구현 코드나 다른 실험을 보지 마세요. '
               '구현 방법은 구현자의 판단에 맡기세요. 명세 승인 요청이면 여섯 필드와 계약 일치 여부를 검토하고 APPROVED 또는 '
               'NEEDS_REVISION을 명시하세요. 변경해야 할 실제 모순만 지적하세요. 도구는 제공되지 않습니다.\n\n')


def build_prompt(questions):
    prompt, packet = INSTRUCTION, {}
    for name in CONTRACT:
        content = (ROOT.parent / 'docs' / name).read_text()
        packet[name] = content
        prompt += '\n# ' + name + '\n' + content
    return prompt + '\n# 질문 또는 승인 요청 원문\n' + Path(questions).read_text(), packet


def proxy_command(image, name, env_file, model, session_id):
    """No workspace, no volume, no tools: the container's own layer holds the fresh config dir and transcript."""
    cli = ['claude', '-p', '--verbose', '--output-format', 'stream-json', '--model', model, '--effort', 'high',
           '--session-id', session_id, '--tools', '']
    return ['docker', 'create', '--name', name, '--interactive', '--init', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=128', '--cpus=1', '--memory=1536m', '--workdir=/tmp',
            '--env-file', str(env_file), '--env', 'HOME=' + launch.HOME_DST,
            '--env', 'CLAUDE_CONFIG_DIR=' + launch.CONFIG_DST, image] + cli


def final_text(stdout):
    text = None
    for line in stdout.read_bytes().splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(event, dict) and event.get('type') == 'result':
            text = event.get('result')
    return text


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True, help='run the consultation belongs to (its cost is that condition\'s cost)')
    p.add_argument('--questions', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--model', required=True, help='user proxy model id (not fixed by the protocol yet)')
    p.add_argument('--env-file', type=Path, required=True, help='0600 file outside the repo with only CLAUDE_CODE_OAUTH_TOKEN=...')
    p.add_argument('--readiness', type=Path, default=ROOT / 'docs/results/readiness.json')
    p.add_argument('--timeout-seconds', type=int, default=1800)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    launch.valid_run(a.run_id)
    ready = json.loads(a.readiness.read_text())
    if ready.get('user_proxy_model') and ready['user_proxy_model'] != a.model:
        p.error('--model differs from readiness user_proxy_model')
    image = ready['image_digests']['runner']
    env_file = a.env_file.resolve()
    token = launch.read_token(env_file)
    prompt, packet = build_prompt(a.questions)
    session_id = str(uuid.uuid4())
    name = 'payment-claude-proxy-' + uuid.uuid4().hex
    command = proxy_command(image, name, env_file, a.model, session_id)
    invocation = {'create': command, 'start': ['docker', 'start', '--attach', '--interactive', name],
                  'run_id': a.run_id, 'role': 'user-proxy', 'model': a.model, 'session_id': session_id,
                  'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'env_file': str(env_file)}
    if a.dry_run:
        print(json.dumps(dict(invocation, prompt_bytes=len(prompt.encode())), indent=2, ensure_ascii=False))
        return 0
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / 'packet').mkdir()
    for file, content in packet.items():
        (out / 'packet' / file).write_text(content)
    (out / 'prompt.txt').write_text(prompt)
    (out / 'invocation.json').write_text(json.dumps(invocation, indent=2, ensure_ascii=False) + '\n')
    code, error = None, None
    try:
        created = subprocess.run(command, capture_output=True, check=False)
        if created.returncode:
            raise RuntimeError('docker create failed: ' + created.stderr.decode(errors='replace')[-500:])
        with (out / 'stdout.jsonl').open('xb') as stdout, (out / 'stderr.txt').open('xb') as stderr:
            try:
                code = subprocess.run(invocation['start'], input=prompt.encode(), stdout=stdout, stderr=stderr,
                                      check=False, timeout=a.timeout_seconds).returncode
            except subprocess.TimeoutExpired:
                subprocess.run(['docker', 'stop', '--time', '5', name], capture_output=True, check=False)
                code = 124
        copied = subprocess.run(['docker', 'cp', name + ':' + launch.CONFIG_DST + '/projects', str(out / 'projects')],
                                capture_output=True, check=False)
        events, ids, incomplete, stats = [], [], [{'reason': 'transcript copy failed (docker cp rc=%s)' % copied.returncode}], {}
        if copied.returncode == 0:
            events, ids, incomplete, stats = launch.build_events(out / 'projects', set(), a.run_id, 'proxy-' + session_id,
                                                                 0, role='user-proxy')
        (out / 'events.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events))
        stream = launch.collect(out / 'stdout.jsonl')
        response = final_text(out / 'stdout.jsonl')
        (out / 'response.txt').write_text(response or '')
        usage = {'run_id': a.run_id, 'role': 'user-proxy', 'requested_model': a.model, 'session_id': session_id,
                 'session_id_confirmed': ((stream['result'] or {}).get('session_id') == session_id),
                 'transcript_models': sorted({e['model'] for e in events}), 'request_ids': ids,
                 'usage': launch.sum_usage(events), 'incomplete': incomplete, 'transcript_stats': stats,
                 'stream_result': stream['result'], 'exit_code': code,
                 'usage_complete': (code == 0 and not incomplete and bool(events) and stats.get('unparsable_lines', 1) == 0
                                    and stats.get('assistant_without_requestId', 1) == 0)}
        (out / 'usage.json').write_text(json.dumps(usage, indent=2, ensure_ascii=False) + '\n')
    except (OSError, RuntimeError, ValueError) as exc:
        error = launch.mask_text(str(exc)[:1000], [token])
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)
        hits = launch.scrub(out, [token])
        if hits:
            (out / 'SECRET-LEAK.txt').write_text('credential found and redacted in: ' + ', '.join(hits) + '\nrotate the token now\n')
    if error or hits:
        print('user proxy failed: ' + (error or 'credential leak redacted; rotate the token'), file=sys.stderr)
        return 1
    print(out / 'response.txt')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
