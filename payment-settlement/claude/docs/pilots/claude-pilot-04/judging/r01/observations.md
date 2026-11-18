# 시나리오별 관측 메모 — claude-pilot-04, 판정 1회째

진행자가 판정기 산출물에서 옮겨 적은 **관측**이다. 통과 여부는 적지 않는다. "판정기 status"는 판정기·어댑터가 기계적으로 낸 칸의 값 그대로다.

판정 대상: 작업 폴더의 작업 트리(HEAD `ec7b322 starter` + 미커밋 변경 30줄, 내용 해시 `aec216d0…751b` — `source-head.txt`). 판정 실행 2026-11-18T11:27:55Z~12:08:24Z(`commands.log`).

`cx-` 컨테이너: v1·v2(11:27:55Z~11:33:42Z)는 메인 지시로 기다리지 않고 실행 — 그동안 `cx-` 2개 실행 중, load average 8.4~11.1. v3는 stack을 11:34:39Z에 띄워 두고 `cx-`가 0이 된 12:06:40Z에 부하를 시작(2분 간격 확인 16회), 부하 중 표본 3개 모두 `cx=0`(`v3/host-samples.txt`), 종료 시 0개.

## 공통 관측 — 계약 경로에 응답하는 endpoint가 없다

- 판정기가 보낸 **모든** 업무 요청이 404 `{"code":"NOT_FOUND","message":"no such endpoint"}`였다: v1 217건(`/api/payments` 215, `/api/settlements` 2), v2 4건, v3 단계마다 1건.
- 구현이 연 경로는 `POST /payments`·`GET /payments/{id}`·`GET /agencies/{id}/settlement`(구현 문서 `docs/payment-settlement.md`, 작업 폴더). `/api` 접두가 없고 `POST /api/settlements`에 해당하는 정산 생성 API가 없다.
- 그래서 DB·은행에 생긴 행이 하나도 없다(세 stack 모두 payments 0 / ledger 0 / 은행 journal 0). 어댑터 audit는 빈 상태에서 PASS(집계 9개 모두 0, `absent_tables: [settlements, settlement_items]`).
- 앱은 세 stack 모두 정상 기동(healthy). `docker build` 안의 `gradlew clean test bootJar`는 성공했고 DB 통합 테스트 24건은 SKIPPED(구현자가 예고한 대로 — `v1/compose-up.log`), 31건 PASSED. 앱 로그 35줄, ` ERROR ` 0줄.

## v1 — HTTP 시나리오 + 전수 대조 (`v1/acceptance-http.json`, 요청 217건 / `v1/acceptance-audit.json`)

새 volume(users 1,000,000 / agencies 1,000). `run_http.py` exit 1(= FAIL 항목 있음).

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01 | FAIL | [0,1] | POST /api/payments → 404 `NOT_FOUND` "no such endpoint"(기대 201 SUCCEEDED) |
| C02 | FAIL | [1,2] | 첫 잘못된 입력(amount −1) → 404(기대 400). 첫 단언에서 멈춰 나머지 8종은 보내지 않음 |
| C03 | **PASS (HTTP+DB+BANK)** | [2,4] | 없는 사용자·없는 대행사 2건 모두 404 — **단 본문은 `NOT_FOUND`/"no such endpoint"**(경로 자체가 없어서 난 404). 판정기는 상태 코드 404·audit PASS·변경 0만 보므로 기계적으로 PASS가 됐다. `acceptance.md`: "skeleton404가 검증에서 통과하면 평가기 결함" |
| C04 | FAIL | [4,5] | 최초 요청 404(기대 201) |
| C05 | FAIL | [5,7] | 같은 키 다른 금액 → 404(기대 409) |
| C06 | FAIL | [7,107] | 동일 키 동시 100건 모두 404 |
| C07 | FAIL | [107,207] | 한 사용자 다른 키 동시 100건 모두 404 |
| C08 | FAIL | [207,209] | 두 대행사 모두 404(기대 201) |
| C09 | FAIL | [209,210] | 헤더≠body 대행사 → 404(기대 403) |
| C10 | FAIL | [210,211] | decline → 404(기대 422) |
| C11 | FAIL | [211,212] | timeout-before → 404(기대 201/202) |
| C12 | FAIL | [212,213] | timeout-after → 404 |
| C15 | FAIL | [213,214] | 정산용 결제 fixture가 404 |
| C16 | FAIL | [214,215] | 정산용 결제 fixture가 404 |
| C18 | FAIL | [215,217] | POST /api/settlements 2건 모두 404(기대 200/201) |
| C13·C14·C17·C19·C20 | NOT_RUN | — | 이 runner 범위 밖(아래) |

- FAIL 항목은 콜백 단언에서 멈췄으므로 scope가 `HTTP_ONLY`이고 그 시나리오의 audit·verify_requests는 돌지 않았다.
- **C19(전수 대조, `v1/acceptance-audit.json`)**: 어댑터 status PASS, errors 0 — 대조할 금융 행이 0건(payments 0, ledger 0, 은행 journal 0)인 상태의 값이다. `v1/payments-by-status.txt` 0행.

## v2 — 장애·시각 시나리오 (`v2/acceptance-faults.json`)

새 volume. `run_faults.py` exit 1.

| ID | 판정기 status | 관측 |
|---|---|---|
| C13 | FAIL | `bank commit boundary not observed` — timeout-after 결제(404)가 은행 기록을 만들지 않아 10초 안에 경계를 관측하지 못함. app SIGKILL은 하지 않음 |
| C14 | FAIL | 같은 사유. db SIGKILL은 하지 않음 |
| C17 | FAIL | fixture 결제가 404. `late_success`도 FAIL(은행 SIGKILL 뒤 결제 404, 기대 202) — 은행 컨테이너는 다시 기동함 |

- `final_audit`: PASS(행 0건). 요청 4건 모두 404. 시간 기준(30초 수렴)까지 간 항목이 없어 `cx-` 부하와 무관한 실패다 — 다시 돌리지 않았다.

## v3 — 부하·회복 (C20) (`v3/load/summary.json`)

새 volume. `load_check.py` exit 1, 12:06:40Z~12:08:08Z(약 90초). `summary.json`: `c20_passed: false`, `phase_gates_passed: false`, `sustainable_rps: null`.

- 8개 단계 모두 k6 exit 107: `workload.js`의 setup이 `Replay fixture failed: 404`로 중단(`v3/load/<phase>/stderr.txt`). 단계마다 요청 1건(setup의 재전송 fixture 결제)만 나갔고 부하 구간은 시작되지 않았다 — p95·p99·dropped 등 부하 수치는 **측정되지 않았다**(k6 요약의 0·"✓"는 표본 0건에서 나온 값).
- 단계별 `passed: false`(k6 exit≠0). PENDING 0(행 없음), 단계 뒤 audit PASS(행 0건), 자원 관측 통과.
- 부하 뒤 수렴: PENDING 0(`v3/post-load-pending.txt`), `v3/post-load-audit.json` PASS(행 0건). 앱 로그 `v3/app.log.gz`.

## 실행하지 않은 것
- 수용 기준 문서의 독립 블라인드 설계 품질 리뷰(런북에 절차 없음).
- 시간 기준 항목의 `cx-` 없는 재실행(해당 없음 — 시간 기준까지 간 항목이 없다).

## C01~C20 원자료 위치

| ID | 판정기가 낸 status | 원자료(이 폴더 `judging/r01/` 기준) |
|---|---|---|
| C01 | FAIL (404, 기대 201) | `v1/acceptance-http.json` cases.C01, requests[0:1] |
| C02 | FAIL (404, 기대 400) | `v1/acceptance-http.json` cases.C02, requests[1:2] |
| C03 | PASS (HTTP+DB+BANK) — 본문은 `NOT_FOUND`/"no such endpoint" | `v1/acceptance-http.json` cases.C03, requests[2:4] |
| C04 | FAIL (404) | `v1/acceptance-http.json` cases.C04, requests[4:5] |
| C05 | FAIL (404, 기대 409) | `v1/acceptance-http.json` cases.C05, requests[5:7] |
| C06 | FAIL (100건 404) | `v1/acceptance-http.json` cases.C06, requests[7:107] |
| C07 | FAIL (100건 404) | `v1/acceptance-http.json` cases.C07, requests[107:207] |
| C08 | FAIL (404) | `v1/acceptance-http.json` cases.C08, requests[207:209] |
| C09 | FAIL (404, 기대 403) | `v1/acceptance-http.json` cases.C09, requests[209:210] |
| C10 | FAIL (404, 기대 422) | `v1/acceptance-http.json` cases.C10, requests[210:211] |
| C11 | FAIL (404) | `v1/acceptance-http.json` cases.C11, requests[211:212] |
| C12 | FAIL (404) | `v1/acceptance-http.json` cases.C12, requests[212:213] |
| C13 | FAIL (`bank commit boundary not observed`) | `v2/acceptance-faults.json` cases.C13 |
| C14 | FAIL (같은 사유) | `v2/acceptance-faults.json` cases.C14 |
| C15 | FAIL (fixture 결제 404) | `v1/acceptance-http.json` cases.C15, requests[213:214] |
| C16 | FAIL (fixture 결제 404) | `v1/acceptance-http.json` cases.C16, requests[214:215] |
| C17 | FAIL (fixture 결제 404, late_success FAIL) | `v2/acceptance-faults.json` cases.C17 |
| C18 | FAIL (정산 요청 404) | `v1/acceptance-http.json` cases.C18, requests[215:217] |
| C19 | 어댑터 audit PASS(세 stack 각각, **금융 행 0건**) | `v1/acceptance-audit.json`, `v2/acceptance-audit.json`, `v3/post-load-audit.json` |
| C20 | `c20_passed: false`(8단계 모두 k6 setup 중단, 부하 미측정) | `v3/load/summary.json`, `v3/load/<phase>/{result.json,k6-summary.json,stdout.txt,stderr.txt,resources.jsonl,k6-inspect.json}`, `v3/load-check.stdout`, `v3/host-samples.txt`, `v3/post-load-pending.txt` |

공통: 어댑터·구동 스크립트 `adapter.py`·`run_http.py`·`run_faults.py`·`pending.sql`·`run-v1-v2.sh`·`run-v3.sh`, 어댑터 음성 대조 `adapter-unittest.txt`, 명령·종료 코드 `commands.log`, 판정 대상 상태 `source-head.txt`, 어댑터 설명·허용 규칙 `README.md`.
