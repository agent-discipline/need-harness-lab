#!/usr/bin/env python3
"""Fresh isolated Sol/high user proxy with frozen contract and questions only."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runner'))
from launch import codex_command
from harness_gate import audit_sessions


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--questions', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    packet = out / 'packet'
    packet.mkdir()
    prompt = '당신은 구현자와 분리된 대리 사용자입니다. 제공한 계약만 근거로 질문에 답하세요. 구현 코드나 다른 실험을 보지 마세요. 구현 방법은 구현자의 판단에 맡기세요. 명세 승인 요청이면 여섯 필드와 계약 일치 여부를 검토하고 APPROVED 또는 NEEDS_REVISION을 명시하세요. 변경해야 할 실제 모순만 지적하세요. 도구/서브에이전트는 필요하지 않습니다.\n\n'
    for name in ('requirements.md', 'resources.md', 'acceptance.md', 'answer-policy.md'):
        content = (ROOT.parent / 'docs' / name).read_text()
        (packet / name).write_text(content)
        prompt += '\n# ' + name + '\n' + content
    prompt += '\n# 질문 또는 승인 요청 원문\n' + a.questions.read_text()
    (out / 'prompt.txt').write_text(prompt)
    name = 'payment-proxy-' + uuid.uuid4().hex
    image = json.loads((ROOT / 'docs/results/readiness.json').read_text())['image_digests']['runner']
    cli = codex_command('gpt-6-sol')
    cli[-1:-1] = ['--disable', 'multi_agent']
    command = ['docker', 'create', '--name', name, '-i', '--init', '--cap-drop=ALL',
        '--security-opt=no-new-privileges', '--cpus=1', '--memory=1536m', '--workdir=/packet',
        '--mount', f'type=bind,src={packet},dst=/packet,readonly',
        '--mount', f'type=bind,src={Path.home() / ".codex/auth.json"},dst=/home/node/.codex/auth.json,readonly',
        image] + cli
    (out / 'invocation.json').write_text(json.dumps(command, indent=2))
    subprocess.run(command, check=True, capture_output=True)
    with (out / 'stdout.jsonl').open('w') as stdout, (out / 'stderr.txt').open('w') as stderr:
        process = subprocess.run(['docker', 'start', '-ai', name], input=prompt, text=True,
                                 stdout=stdout, stderr=stderr)
    subprocess.run(['docker', 'cp', name + ':/home/node/.codex/sessions', str(out / 'sessions')], check=True)
    result = audit_sessions(out / 'sessions')
    result['exit_code'] = process.returncode
    (out / 'usage-audit.json').write_text(json.dumps(result, indent=2))
    messages = []
    for line in (out / 'stdout.jsonl').read_text().splitlines():
        e = json.loads(line)
        if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'agent_message':
            messages.append(e['item']['text'])
    (out / 'response.txt').write_text('\n\n'.join(messages))
    subprocess.run(['docker', 'rm', name], check=True, capture_output=True)
    print(out / 'response.txt')
    return process.returncode


if __name__ == '__main__':
    raise SystemExit(main())
