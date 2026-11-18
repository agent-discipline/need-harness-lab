#!/usr/bin/env bash
# Judging round driver (conductor): v1 (HTTP + audit) then v2 (faults/clock), each on a fresh-volume Compose project.
# Usage: [RUN_V2=1] run-v1-v2.sh <round dir relative to repo root>   (run from the repo root)
set -u
J=$1; RUN=claude-pilot-04; P=payment-claude-pilot-04; C=payment-settlement/claude/compose.yaml; LOG=$J/commands.log
log() { echo "$(date -u +%FT%TZ) | $*" >> $LOG; }
cx() { docker ps --format '{{.Names}}' | grep -c '^cx-'; }
stack() { # n port-suffix
  local n=$1 s=$2 out=$J/v$1; mkdir -p $out
  log "v$n 시작 전 cx- $(cx)개, load average $(cut -d' ' -f1-3 /proc/loadavg). v$n up: ARM=.runs/$RUN CORE=workspace APP_PORT=380$s DB_PORT=390$s BANK_PORT=400$s docker compose -p $P-v$n -f $C up -d --build --wait db bank app"
  ( ARM=.runs/$RUN CORE=workspace APP_PORT=380$s DB_PORT=390$s BANK_PORT=400$s docker compose -p $P-v$n -f $C up -d --build --wait db bank app ) > $out/compose-up.log 2>&1
  local rc=$?; log "v$n up exit=$rc"; return $rc
}
finish() { # n
  local n=$1 out=$J/v$1
  docker logs $P-v$n-app-1 > $out/app.log 2>&1
  ( docker compose -p $P-v$n -f $C down -v ) > $out/compose-down.log 2>&1; log "v$n down exit=$?"
}
log "판정 4회째 시작(v1·v2). cx- $(cx)개"
if stack 1 11; then
  log "v1 fixture: users/agencies = $(docker exec $P-v1-db-1 psql -U lab -d payment -X -A -t -c 'select (select count(*) from users),(select count(*) from agencies)'), cx- $(cx)개"
  log "v1 http: python3 $J/run_http.py --base-url http://localhost:38011 --namespace p04r04v1 --project $P-v1 --output $J/v1/acceptance-http.json"
  python3 -B $J/run_http.py --base-url http://localhost:38011 --namespace p04r04v1 --project $P-v1 --output $J/v1/acceptance-http.json > $J/v1/run-http.stdout 2> $J/v1/run-http.stderr
  log "v1 http exit=$? (1 = FAIL인 항목 있음, 2 = FAIL 없음·전체 합격 주장 안 함)"
  python3 -B $J/adapter.py --project $P-v1 --output $J/v1/acceptance-audit.json > /dev/null 2> $J/v1/audit.stderr
  log "v1 audit(adapter.py → v1/acceptance-audit.json) exit=$?"
  docker exec $P-v1-db-1 psql -U lab -d payment -X -A -c "select status, failure_code, count(*) from payments group by 1,2 order by 1,2" > $J/v1/payments-by-status.txt 2>&1
fi
finish 1
if [ "${RUN_V2:-0}" = 1 ]; then
if stack 2 12; then
  log "v2 faults: python3 $J/run_faults.py --base-url http://localhost:38012 --namespace p04r04v2 --project $P-v2 --output $J/v2/acceptance-faults.json"
  python3 -B $J/run_faults.py --base-url http://localhost:38012 --namespace p04r04v2 --project $P-v2 --output $J/v2/acceptance-faults.json > $J/v2/run-faults.stdout 2> $J/v2/run-faults.stderr
  log "v2 faults exit=$?"
  python3 -B $J/adapter.py --project $P-v2 --output $J/v2/acceptance-audit.json > /dev/null 2> $J/v2/audit.stderr
  log "v2 audit(adapter.py → v2/acceptance-audit.json) exit=$?"
fi
finish 2
else log "v2 이번 회차 미실행(v1의 P0 HTTP 항목이 모두 통과한 뒤부터 — 메인 지시), 직전 결과 유지"; fi
log "v1·v2 끝. cx- $(cx)개"
echo ROUND_V1V2_DONE
