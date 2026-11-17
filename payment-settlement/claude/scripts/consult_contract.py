#!/usr/bin/env python3
"""Fresh isolated Claude user proxy: frozen contract + questions only, no tools, its own session and config.

The proxy model is the agreed launch.USER_PROXY_MODEL; readiness must carry it and --model must equal it.
Output goes to <runs>/<run-id>/consults/NNNN so the run ledger counts the proxy with every other role.
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


def consult_dir(runs, run_id):
    """Consultations live inside their run (consults/NNNN) so the run ledger counts them with every other role."""
    run = Path(runs).resolve() / launch.valid_run(run_id)
    manifest = json.loads((run / 'manifest.json').read_text())
    if manifest.get('arm') != 3:
        raise ValueError('the user proxy answers only the harness arm (arm 3)')
    folder = run / 'consults'
    number = len([p for p in folder.iterdir() if p.is_dir()]) + 1 if folder.is_dir() else 1
    return run, folder / ('%04d' % number)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True, help='arm 3 run the consultation belongs to (its cost is that condition\'s cost)')
    p.add_argument('--questions', type=Path, required=True)
    p.add_argument('--model', required=True, help='must equal the agreed and frozen user_proxy_model (%s)' % launch.USER_PROXY_MODEL)
    p.add_argument('--env-file', type=Path, required=True, help='0600 file outside the repo with only CLAUDE_CODE_OAUTH_TOKEN=...')
    p.add_argument('--readiness', type=Path, default=ROOT / 'docs/results/readiness.json')
    p.add_argument('--runs', type=Path, default=ROOT / '.runs')
    p.add_argument('--timeout-seconds', type=int, default=1800)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    ready = json.loads(a.readiness.read_text())
    if ready.get('user_proxy_model') != launch.USER_PROXY_MODEL:
        p.error('readiness does not carry the agreed user_proxy_model %s; freeze it before any proxy call' % launch.USER_PROXY_MODEL)
    if a.model != launch.USER_PROXY_MODEL:
        p.error('--model must be the agreed user proxy model %s' % launch.USER_PROXY_MODEL)
    try:
        run, out = consult_dir(a.runs, a.run_id)
        # the run's caps cover the proxy too (same gate as the runner's turns; a censored run gets no consultation)
        launch.limit_gate(run, next_is_feedback=False, write=not a.dry_run)
    except (OSError, ValueError, RuntimeError) as exc:
        p.error(str(exc))
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
        print(json.dumps(dict(invocation, prompt_bytes=len(prompt.encode()), output=str(out)), indent=2, ensure_ascii=False))
        return 0
    out.mkdir(parents=True, exist_ok=False)
    (out / 'packet').mkdir()
    for file, content in packet.items():
        (out / 'packet' / file).write_text(content)
    (out / 'prompt.txt').write_text(prompt)
    (out / 'invocation.json').write_text(json.dumps(invocation, indent=2, ensure_ascii=False) + '\n')
    code, error = None, None
    # written first so an interrupted call is visible to the run ledger as an incomplete (not a zero) consultation
    usage = {'run_id': a.run_id, 'role': 'user-proxy', 'requested_model': a.model, 'session_id': session_id,
             'usage': None, 'usage_unobserved': True, 'usage_complete': False, 'cost_usd': None}
    (out / 'usage.json').write_text(json.dumps(usage, indent=2, ensure_ascii=False) + '\n')
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
        for event in events:
            event['requested_model'] = a.model
        (out / 'events.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events))
        stream = launch.collect(out / 'stdout.jsonl')
        response = final_text(out / 'stdout.jsonl')
        (out / 'response.txt').write_text(response or '')
        complete = (code == 0 and not incomplete and bool(events) and stats.get('unparsable_lines', 1) == 0
                    and stats.get('assistant_without_requestId', 1) == 0)
        observed = copied.returncode == 0 and bool(events)
        usage.update({'session_id_confirmed': ((stream['result'] or {}).get('session_id') == session_id),
                      'transcript_models': sorted({e['model'] for e in events}), 'request_ids': ids,
                      # never a zero dictionary for usage nobody observed; partial sums are flagged as lower bounds
                      'usage': launch.sum_usage(events) if observed else None, 'usage_unobserved': not observed,
                      'usage_is_lower_bound': observed and not complete,
                      'incomplete': incomplete, 'transcript_stats': stats, 'stream_result': stream['result'],
                      'exit_code': code, 'usage_complete': complete,
                      'cost_usd': launch.number((stream['result'] or {}).get('total_cost_usd'))})
        (out / 'usage.json').write_text(json.dumps(usage, indent=2, ensure_ascii=False) + '\n')
    except (OSError, RuntimeError, ValueError) as exc:
        error = launch.mask_text(str(exc)[:1000], [token])
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)
        hits = launch.scrub(out, [token])
        if hits:
            (out / 'SECRET-LEAK.txt').write_text('credential found and redacted in: ' + ', '.join(hits) + '\nrotate the token now\n')
        launch.write_ledger(run)
        launch.record_overshoot(run)
    if error or hits:
        print('user proxy failed: ' + (error or 'credential leak redacted; rotate the token'), file=sys.stderr)
        return 1
    print(out / 'response.txt')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
