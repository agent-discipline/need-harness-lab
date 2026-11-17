#!/usr/bin/env python3
"""Sequential k6 contract phases against an existing, explicitly owned stack."""
import argparse
import datetime as dt
import json
import re
import subprocess
import time
from pathlib import Path

PHASES = ['warmup', 'normal', 'spike_recovery', 'soak', 'capacity25', 'capacity50', 'capacity100', 'capacity200']
ROOT = Path(__file__).resolve().parents[2]

def run(cmd, check=True):
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(f'{cmd[:4]}: {p.stderr}')
    return p

def inspect(cid):
    return json.loads(run(['docker','inspect',cid]).stdout)[0]

def sql(db, query):
    return run(['docker','exec',db,'psql','-U','lab','-d','payment','-At','-v','ON_ERROR_STOP=1','-c',query]).stdout.strip()

def sample(containers, app_ip):
    result = {'time':dt.datetime.now(dt.timezone.utc).isoformat(), 'monotonic':time.monotonic(), 'services':{}}
    for service, cid in containers.items():
        state = inspect(cid)
        data = run(['docker','exec',cid,'sh','-c',
            'cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.events /sys/fs/cgroup/pids.current; '
            'for p in /proc/[0-9]*/status; do awk \'/^(Name|Pid|VmRSS|Threads):/ {print}\' "$p"; done'], check=False)
        result['services'][service] = {'state':state['State'], 'restart_count':state['RestartCount'],
            'cgroup_proc_raw':data.stdout, 'observation_error':data.stderr, 'observation_exit':data.returncode}
    result['app_db_connections'] = json.loads(sql(containers['db'],
        "SELECT json_build_object('total',count(*),'active',count(*) FILTER (WHERE state='active')) FROM pg_stat_activity WHERE client_addr='"+app_ip+"'::inet"))
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--phases', nargs='+', choices=PHASES, default=PHASES)
    p.add_argument('--pending-sql-file', type=Path, required=True, help='Evaluator-reviewed read-only count query')
    p.add_argument('--audit-command-json', required=True, help='JSON argv for C19 hook: exit0 and stdout JSON {passed:true}')
    p.add_argument('--sample-seconds', type=float, default=5)
    a=p.parse_args()
    # Claude arm: the run's own Compose project created by start_cycle.py (payment-claude-cNN-aN)
    if not re.fullmatch(r'payment-claude-[a-z0-9-]+',a.project): p.error('Explicit payment-claude-* owned project required')
    if a.sample_seconds < 1: p.error('sample interval must be >=1s')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'summary.json').exists(): p.error('Refusing evidence overwrite')
    query=a.pending_sql_file.read_text().strip()
    if not query.lower().startswith('select ') or ';' in query.rstrip(';'): p.error('Single SELECT pending query required')
    audit=json.loads(a.audit_command_json)
    if not isinstance(audit,list) or not audit or not all(isinstance(v,str) for v in audit):p.error('Audit must be argv array')
    containers={}
    for service in ('app','db','bank'):
        ids=run(['docker','ps','-q','--filter',f'label=com.docker.compose.project={a.project}','--filter',f'label=com.docker.compose.service={service}']).stdout.split()
        if len(ids)!=1:raise RuntimeError(f'Expected exactly one running {service}')
        containers[service]=ids[0]
    ai=inspect(containers['app']); networks=ai['NetworkSettings']['Networks']
    network=next((n for n in networks if n.startswith(a.project+'_')),None)
    if not network:raise RuntimeError('No owned project network')
    app_ip=networks[network]['IPAddress'];baseline=sample(containers,app_ip)
    (out/'baseline.json').write_text(json.dumps(baseline,indent=2))
    summary={'project':a.project,'phases':[],'all_acceptance_passed':False,'c20_passed':False}
    for index,name in enumerate(a.phases):
        phase='capacity' if name.startswith('capacity') else name
        folder=out/name;folder.mkdir()
        kname=f'{a.project}-load-{index}-{int(time.time())}'
        command=['docker','run','--name',kname,'--network',network,'--cpus=2','--memory=1024m',
            '--mount',f'type=bind,src={ROOT / "evaluation/k6"},dst=/tests,readonly',
            '-e','BASE_URL=http://app:8080','-e',f'PHASE={phase}','-e',f'RUN_ID={kname}']
        if phase=='capacity':command+=['-e',f'RATE={name[8:]}']
        command+=['grafana/k6:1.2.3','run','--summary-export=/tmp/summary.json','/tests/workload.js']
        samples=[]
        with (folder/'stdout.txt').open('w') as stdout,(folder/'stderr.txt').open('w') as stderr,(folder/'resources.jsonl').open('w') as log:
            process=subprocess.Popen(command,stdout=stdout,stderr=stderr)
            while True:
                try:observation=sample(containers,app_ip)
                except Exception as exc:observation={'error':str(exc),'monotonic':time.monotonic()}
                samples.append(observation);log.write(json.dumps(observation)+'\n');log.flush()
                if process.poll() is not None:break
                time.sleep(a.sample_seconds)
        copied=run(['docker','cp',f'{kname}:/tmp/summary.json',str(folder/'k6-summary.json')],check=False)
        # Retain exited k6 container for evidence; never remove arbitrary containers or volumes.
        (folder/'k6-inspect.json').write_text(json.dumps(inspect(kname),indent=2))
        pending=[];deadline=time.monotonic()+30
        while True:
            try:count=int(sql(containers['db'],query))
            except Exception as exc:count=None;pending.append({'error':str(exc)})
            pending.append({'elapsed':30-max(0,deadline-time.monotonic()),'count':count})
            if count==0 or time.monotonic()>=deadline:break
            time.sleep(1)
        hook=run(audit,check=False);(folder/'audit-stdout.json').write_text(hook.stdout);(folder/'audit-stderr.txt').write_text(hook.stderr)
        try:report=json.loads(hook.stdout);audit_ok=hook.returncode==0 and (report.get('passed') is True or report.get('status') == 'PASS')
        except (ValueError,AttributeError):audit_ok=False
        resource_ok=all('error' not in s and s['app_db_connections']['total']<=10 and all(
            v['observation_exit']==0 and not v['state']['OOMKilled'] and v['state']['Running'] and
            v['restart_count']==baseline['services'][service]['restart_count'] and
            v['state']['StartedAt']==baseline['services'][service]['state']['StartedAt']
            for service,v in s['services'].items()) for s in samples)
        # /proc VmRSS includes all app processes; compare medians of early/late steady soak windows.
        rss=[]
        for s in samples:
            raw=s.get('services',{}).get('app',{}).get('cgroup_proc_raw','')
            values=re.findall(r'^VmRSS:\s+(\d+)\s+kB',raw,re.M)
            if values:rss.append(sum(map(int,values)))
        import statistics
        growth=None
        if phase=='soak' and len(rss)>=12:
            width=max(3,len(rss)//5);early=statistics.median(rss[width:2*width]);late=statistics.median(rss[-width:]);growth=late/early-1 if early else None
            resource_ok=resource_ok and growth is not None and growth<=0.20
        record={'phase':name,'k6_exit_code':process.returncode,'summary_saved':copied.returncode==0,
            'pending_samples':pending,'pending_converged':count==0,'audit_passed':audit_ok,
            'resources_passed':resource_ok,'soak_rss_growth':growth,
            'passed':process.returncode==0 and copied.returncode==0 and count==0 and audit_ok and resource_ok}
        (folder/'result.json').write_text(json.dumps(record,indent=2));summary['phases'].append(record)
        (out/'summary.json').write_text(json.dumps(summary,indent=2))
    required=set(PHASES[:4]);summary['phase_gates_passed']=required.issubset(a.phases) and all(r['passed'] for r in summary['phases'] if not r['phase'].startswith('capacity'))
    summary['c20_passed']=summary['phase_gates_passed']
    summary['sustainable_rps']=max([int(r['phase'][8:]) for r in summary['phases'] if r['phase'].startswith('capacity') and r['passed']],default=None)
    (out/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
    return 0 if all(r['passed'] for r in summary['phases'] if not r['phase'].startswith('capacity')) else 1
if __name__=='__main__':raise SystemExit(main())
