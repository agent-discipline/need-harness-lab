#!/usr/bin/env bash
# Judging round driver (conductor): v3 load (C20). Stack first, then wait until no cx- container runs, then load_check.py.
# Usage: run-v3.sh <round dir relative to repo root>   (run from the repo root)
set -u
J=$1; RUN=claude-pilot-04; P=payment-claude-pilot-04; C=payment-settlement/claude/compose.yaml; LOG=$J/commands.log; out=$J/v3
ROOT=$(pwd); mkdir -p $out
log() { echo "$(date -u +%FT%TZ) | $*" >> $LOG; }
cx() { docker ps --format '{{.Names}}' | grep -c '^cx-'; }
log "v3 up(부하 전 stack 먼저 — 메인 지시): cx- $(cx)개. ARM=.runs/$RUN CORE=workspace APP_PORT=38013 DB_PORT=39013 BANK_PORT=40013 docker compose -p $P-v3 -f $C up -d --build --wait db bank app"
( ARM=.runs/$RUN CORE=workspace APP_PORT=38013 DB_PORT=39013 BANK_PORT=40013 docker compose -p $P-v3 -f $C up -d --build --wait db bank app ) > $out/compose-up.log 2>&1
rc=$?; log "v3 up exit=$rc"; [ $rc -eq 0 ] || { echo V3_UP_FAILED; exit 4; }
n=0
until [ "$(cx)" = "0" ]; do
  n=$((n+1)); [ $n -gt 30 ] && { log "v3 대기 60분 초과 — cx- $(cx)개. stack은 띄운 채 둠"; echo V3_WAIT_TIMEOUT; exit 3; }
  sleep 120
done
log "cx- 0개 확인(2분 간격 확인 $n회 뒤), load average $(cut -d' ' -f1-3 /proc/loadavg)"
( while true; do echo "$(date -u +%FT%TZ) cx=$(cx) load=$(cut -d' ' -f1-3 /proc/loadavg)"; sleep 30; done ) > $out/host-samples.txt &
SAMPLER=$!
AUDIT="[\"python3\", \"-B\", \"$ROOT/$J/adapter.py\", \"--project\", \"$P-v3\"]"
log "v3 load: python3 payment-settlement/claude/scripts/load_check.py --project $P-v3 --output $out/load --pending-sql-file $J/pending.sql --audit-command-json '$AUDIT'"
python3 payment-settlement/claude/scripts/load_check.py --project $P-v3 --output $out/load --pending-sql-file $J/pending.sql --audit-command-json "$AUDIT" > $out/load-check.stdout 2> $out/load-check.stderr
rc=$?; echo $rc > $out/load-check.exit
kill $SAMPLER 2>/dev/null
log "v3 load exit=$rc. host-samples.txt 표본 $(wc -l < $out/host-samples.txt)개 중 cx≠0 $(grep -vc 'cx=0 ' $out/host-samples.txt)개"
log "v3 부하 종료 뒤 PENDING 소진 관측(15초 간격, 0이 되거나 10분) → v3/post-load-pending.txt"
for i in $(seq 1 40); do
  c=$(docker exec $P-v3-db-1 psql -U lab -d payment -X -A -t -c "$(cat $J/pending.sql)" 2>&1); echo "$(date -u +%FT%TZ) pending=$c" >> $out/post-load-pending.txt
  [ "$c" = "0" ] && break; sleep 15
done
python3 -B $J/adapter.py --project $P-v3 --output $out/post-load-audit.json > /dev/null 2> $out/post-load-audit.stderr
log "v3 post-load audit exit=$?"
docker logs $P-v3-app-1 2>&1 | gzip > $out/app.log.gz
K=$(docker ps -a --format '{{.Names}}' | grep "^$P-v3-load-")
[ -n "$K" ] && docker rm $K > /dev/null && log "v3 k6 컨테이너(종료 상태) $(echo "$K" | wc -l)개 제거"
( docker compose -p $P-v3 -f $C down -v ) > $out/compose-down.log 2>&1; log "v3 down exit=$?"
log "판정 7회째 끝. cx- $(cx)개"
echo ROUND_V3_DONE
