import http from 'k6/http';
import { check, sleep } from 'k6';

// External correctness starter. Full C01..C20 gating is documented separately.
export const options = { vus: 1, iterations: 1, batch: 100, batchPerHost: 100, thresholds: { checks: ['rate==1'] } };
const base = __ENV.BASE_URL || 'http://localhost:18080';
const ns = __ENV.RUN_ID || 'pilot';
function send(key, amount = 100, extra = {}) {
  return http.post(`${base}/api/payments`, JSON.stringify({userId: 1, agencyId: 1, amount, currency: 'KRW'}),
    { headers: {'Content-Type': 'application/json', 'X-Agency-Id': '1', 'Idempotency-Key': key, ...extra} });
}
function body(r) { try { return r.json(); } catch (_) { return {}; } }
export default function () {
  const first = send(`${ns}-single`);
  const id = body(first).paymentId;
  check(first, {'C01 success': r => r.status === 201 && body(r).status === 'SUCCEEDED' && !!id});
  for (let i = 0; i < 20; i++) {
    const replay = send(`${ns}-single`);
    check(replay, {'C04 replay same ID': r => r.status === 200 && !!id && body(r).paymentId === id});
  }
  check(send(`${ns}-single`, 101), {'C05 payload conflict': r => r.status === 409});
  check(send(`${ns}-bad`, 0), {'C02 zero invalid': r => r.status === 400});
  check(send(`${ns}-decline`, 100, {'X-Bank-Fault': 'decline'}), {'C10 decline': r => r.status === 422});
  const requests = Array.from({length: 100}, () => ({method: 'POST', url: `${base}/api/payments`,
    body: JSON.stringify({userId: 2, agencyId: 1, amount: 100, currency: 'KRW'}),
    params: {headers: {'Content-Type': 'application/json', 'X-Agency-Id': '1', 'Idempotency-Key': `${ns}-race`}}}));
  const responses = http.batch(requests);
  const ids = responses.map(r => body(r).paymentId);
  check(responses, {
    'C06 all accepted': rs => rs.every(r => [200, 201, 202].includes(r.status)),
    'C06 same nonempty ID': () => ids.every(id => !!id) && new Set(ids).size === 1,
  });
  const uncertain = send(`${ns}-uncertain`, 100, {'X-Bank-Fault': 'timeout-after'});
  const uncertainId = body(uncertain).paymentId;
  check(uncertain, {'C12 accepted': r => [201, 202].includes(r.status) && !!uncertainId});
  if (uncertainId) {
    let status;
    for (let i = 0; i < 30; i++) {
      const result = http.get(`${base}/api/payments/${uncertainId}`, {headers: {'X-Agency-Id': '1'}});
      status = body(result).status;
      if (status === 'SUCCEEDED') break;
      sleep(1);
    }
    check(status, {'C12 converged': s => s === 'SUCCEEDED'});
  }
}
