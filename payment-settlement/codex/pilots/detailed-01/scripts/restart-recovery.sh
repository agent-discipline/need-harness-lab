#!/usr/bin/env bash
set -euo pipefail

# Run against a disposable fixture volume after ./gradlew bootJar.
port=18080
app_pid=''
cleanup() {
  if [[ -n "$app_pid" ]]; then kill "$app_pid" 2>/dev/null || true; wait "$app_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
start_app() {
  SERVER_PORT="$port" "${JAVA_HOME:-/opt/java/openjdk}/bin/java" -jar build/libs/app.jar > /tmp/payment-restart-regression.log 2>&1 &
  app_pid=$!
  for ((n=0;n<80;n++)); do
    if curl -fsS "http://localhost:$port/health" >/dev/null 2>&1; then return; fi
    sleep 0.25
  done
  cat /tmp/payment-restart-regression.log
  exit 1
}

key="restart-$(python3 -c 'import uuid; print(uuid.uuid4())')"
user_id="$(python3 -c 'import random; print(random.randrange(100000,800000))')"
start_app
response="$(curl -fsS -X POST "http://localhost:$port/api/payments" \
  -H 'Content-Type: application/json' -H 'X-Agency-Id: 998' \
  -H "Idempotency-Key: $key" -H 'X-Bank-Fault: timeout-after' \
  -d "{\"userId\":$user_id,\"agencyId\":998,\"amount\":100,\"currency\":\"KRW\"}")"
payment_id="$(python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="PENDING",d; print(d["paymentId"])' <<< "$response")"
kill -9 "$app_pid"
wait "$app_pid" 2>/dev/null || true
app_pid=''
start_app
for ((n=0;n<120;n++)); do
  response="$(curl -fsS "http://localhost:$port/api/payments/$payment_id" -H 'X-Agency-Id: 998')"
  if python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["status"]=="SUCCEEDED" and d["bankTransferId"] else 1)' <<< "$response"; then
    echo "Recovered payment $payment_id after restart"
    exit 0
  fi
  sleep 0.25
done
echo "Payment $payment_id did not recover within 30 seconds" >&2
exit 1
