import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';

export const options = {
  scenarios: {normal: {executor: 'constant-arrival-rate', rate: Number(__ENV.RATE || 50),
    timeUnit: '1s', duration: __ENV.DURATION || '5m', preAllocatedVUs: 50, maxVUs: 200}},
  thresholds: {checks: ['rate==1'], http_req_failed: ['rate<0.001'],
    http_req_duration: ['p(95)<500', 'p(99)<1500'], dropped_iterations: ['count==0']},
};
export default function () {
  const n = exec.scenario.iterationInTest;
  const payload = {userId: n % 1000000 + 1, agencyId: n % 1000 + 1, amount: 100, currency: 'KRW'};
  const response = http.post(`${__ENV.BASE_URL || 'http://localhost:18080'}/api/payments`, JSON.stringify(payload),
    {headers: {'Content-Type': 'application/json', 'X-Agency-Id': String(payload.agencyId),
      'Idempotency-Key': `${__ENV.RUN_ID || 'load'}-${n}`}});
  check(response, {'success committed': r => r.status === 201 && r.json('status') === 'SUCCEEDED'});
}
