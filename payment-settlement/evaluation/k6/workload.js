import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';

// Run phases separately to preserve phase-level thresholds and evidence.
const phase = __ENV.PHASE || 'normal';
const profiles = {
  warmup: [10, '30s'], normal: [50, '5m'], spike: [100, '30s'],
  recovery: [50, '60s'], spike_recovery: [100, '90s'], soak: [30, '15m'], capacity: [Number(__ENV.RATE || 25), '2m'],
};
if (!profiles[phase]) throw new Error(`Unknown phase ${phase}`);
if (!__ENV.RUN_ID) throw new Error('A unique RUN_ID per phase is required');
const [rate, duration] = profiles[phase];
const latency = new Trend('payment_latency', true);
const failures = new Rate('unexpected_failure');
const completed = new Rate('committed_response');
const replay = new Rate('replay_fraction');
export const options = {
  scenarios: { traffic: { executor: 'constant-arrival-rate', rate, timeUnit: '1s',
    duration, preAllocatedVUs: Math.max(50, rate), maxVUs: 400, gracefulStop: '30s' } },
  thresholds: {
    payment_latency: ['p(95)<500', 'p(99)<1500'],
    unexpected_failure: ['rate<0.001'], committed_response: ['rate==1'],
    dropped_iterations: ['count==0'], checks: ['rate==1'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
};
if (phase === 'spike_recovery') {
  options.scenarios = {
    spike: { executor: 'constant-arrival-rate', rate: 100, timeUnit: '1s', duration: '30s',
      preAllocatedVUs: 100, maxVUs: 400, gracefulStop: '30s' },
    recovery: { executor: 'constant-arrival-rate', rate: 50, timeUnit: '1s', duration: '60s',
      startTime: '30s', preAllocatedVUs: 50, maxVUs: 400, gracefulStop: '30s' },
  };
  for (const segment of ['spike', 'recovery']) {
    options.thresholds[`payment_latency{segment:${segment}}`] = ['p(95)<500', 'p(99)<1500'];
    options.thresholds[`unexpected_failure{segment:${segment}}`] = ['rate<0.001'];
    options.thresholds[`committed_response{segment:${segment}}`] = ['rate==1'];
  }
}
const base = __ENV.BASE_URL || 'http://localhost:18080';
function send(body, key) {
  return http.post(`${base}/api/payments`, JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json', 'X-Agency-Id': String(body.agencyId),
      'Idempotency-Key': key }, timeout: '10s', tags: { phase },
  });
}
function succeeded(response) {
  try { return response.json('status') === 'SUCCEEDED'; } catch (_) { return false; }
}
export function setup() {
  // Fixture calls have no custom metrics and are excluded from measured latency/error rate.
  const seeds = [];
  for (let n = 0; n < 20; n++) {
    const body = { userId: 800001 + n, agencyId: 801 + n, amount: 100, currency: 'KRW' };
    const key = `${__ENV.RUN_ID}-seed-${n}`;
    const response = send(body, key);
    if (response.status !== 201 || !succeeded(response)) throw new Error(`Replay fixture failed: ${response.status}`);
    seeds.push({ body, key, id: response.json('paymentId') });
  }
  return seeds;
}
export default function (seeds) {
  const n = exec.scenario.iterationInTest;
  const isReplay = n % 20 === 19;
  const seed = seeds[Math.floor(n / 20) % seeds.length];
  const body = isReplay ? seed.body : {
    userId: n % 700000 + 1, agencyId: n % 700 + 1, amount: 100, currency: 'KRW',
  };
  const response = send(body, isReplay ? seed.key : `${__ENV.RUN_ID}-${exec.scenario.name}-new-${n}`);
  const ok = response.status === (isReplay ? 200 : 201) && succeeded(response);
  const tags = { segment: exec.scenario.name };
  latency.add(response.timings.duration, tags);
  failures.add(response.status === 0 || response.status >= 500, tags);
  completed.add(ok, tags);
  replay.add(isReplay);
  check(response, {
    'committed synchronous response': () => ok,
    'replay identity preserved': r => !isReplay || (ok && r.json('paymentId') === seed.id),
  });
}
