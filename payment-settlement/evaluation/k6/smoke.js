import http from 'k6/http';
import { check } from 'k6';

export const options = {
  vus: 1, iterations: 1,
  thresholds: { checks: ['rate==1'], http_req_failed: ['rate==0'] },
};
export default function () {
  const response = http.get(`${__ENV.BASE_URL || 'http://localhost:18080'}/health`);
  check(response, {
    'HTTP 200': r => r.status === 200,
    'DB SELECT succeeds': r => r.status === 200 && r.json('database') === true,
  });
}
