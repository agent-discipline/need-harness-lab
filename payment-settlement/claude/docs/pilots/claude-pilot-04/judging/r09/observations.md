# 시나리오별 관측 메모 — claude-pilot-04, 판정 9회째(턴 0009 = 피드백 8 [C16] 뒤)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 8회째와 같다(`../r08/README.md` → `../r03/README.md`, 음성 대조 21건 통과).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 38줄, `source-head.txt`). 실행 2026-11-18T14:21Z~, **v1 + v2 + v3 전부**(v1·v2에서 C01~C19가 모두 판정기 status PASS → v3 실행).

## v1 (`v1/acceptance-http.json` 요청 470건, `v1/acceptance-audit.json`) — `run_http.py` exit 2(FAIL 항목 없음), `cx-` 2개 실행 중

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01~C09 | PASS (HTTP+DB+BANK) | [0,391] | 앞 회차와 같음. C07: 201 50 / 409 50, 409는 FAILED `INSUFFICIENT_BALANCE` 행(무효과) — 허용 규칙 2 적용 |
| C10 | PASS | [391,392] | decline → 422, FAILED 행(홀드 후 해제), 은행 기록 없음 — 허용 규칙 1·2 적용 |
| C11 | PASS | [392,410] | timeout-before 202(1.05초)·5xx 202 → 조회 반복 뒤 둘 다 SUCCEEDED(30초 내) |
| C12 | PASS | [410,412] | timeout-after 201 SUCCEEDED(1.03초, 은행 조회로 동기 확정), 은행 기록 1건 |
| C15 | PASS | [412,417] | 정산 201(10301/102/10199/3), 재전송 200 같은 settlementId |
| C16 | PASS | [417,468] | 같은 날짜 정산 동시 50: 50건 모두 201, paymentCount 1이 1건·0이 49건(0건·0원 정산 49행 저장) |
| C18 | PASS | [468,470] | 2000-01-01 정산 201 0건·0원, 재전송 200 같은 settlementId |

- 마지막 전수 대조(`v1/acceptance-audit.json`): PASS, errors 0, 집계 9개 0. payments 115(SUCCEEDED 64 / FAILED 51 / PENDING 0), ledger 332, settlements 52, 정산된 결제 4, 은행 journal 64. 최대 응답 시간 1.32초.

## v2 (`v2/acceptance-faults.json`, `v2/acceptance-audit.json`) — `run_faults.py` exit 0, `cx-` 2개 실행 중

| ID | 판정기 status | 관측 |
|---|---|---|
| C13 | PASS | 은행 기록 있음 + DB `PENDING` 확인 뒤 app SIGKILL. 처음 요청 연결 끊김. healthy까지 16.2초, 그 뒤 4.8초에 SUCCEEDED. 재전송 200·같은 paymentId. audit PASS |
| C14 | PASS | 같은 경계에서 db SIGKILL. 처음 요청 202. healthy까지 6.8초, 그 뒤 7.4초에 SUCCEEDED. 재전송 200. audit PASS |
| C17 | PASS | 2020-01-01 정산 1건 101원 / 2020-01-02 정산 2건 205원 / 늦게 추가된 1건: 같은 키 재전송은 그대로, 새 키 정산 1건 104원. 늦은 성공: 은행 중단 중 202 → 오늘 정산 0건 → 은행 재기동 뒤 SUCCEEDED(확정 날짜 2026-11-18) → 재전송 0건 그대로, 새 키 정산 1건 107원, 2020-01-01 정산 0건. 시각 fixture는 `completed_at`·`business_date`를 함께 주입(README) |

- `final_audit` PASS: payments 7(모두 SUCCEEDED), ledger 45, settlements 6, 은행 7. 시간 기준 항목(30초 수렴)은 4.8~7.4초로 여유가 있어 `cx-` 없는 재실행은 하지 않음.

## v3 — 부하·회복 (C20) (`v3/load/summary.json`, 단계별 `v3/load/<phase>/`)

새 volume. stack을 14:28:11Z에 띄워 두고 `cx-`가 0이 된 14:36:11Z에 부하 시작(2분 간격 확인 4회), `load_check.py` exit 1, 14:36:11Z~15:08:29Z. 부하 중 30초 표본 65개 모두 `cx=0`(`v3/host-samples.txt`), 종료 시 0개. 한도: k6 2 CPU·1024MB, app 1.5 CPU·1536MB, db 1.5 CPU·2048MB, bank 0.5 CPU·256MB(`compose.yaml`).

`summary.json`: `c20_passed: false`, `phase_gates_passed: false`, `sustainable_rps: null`.

| 단계 | 부하 | k6 exit | p95 / p99 (ms) | 동기 확정 응답(201·200 + SUCCEEDED) | 5xx·연결 실패 | dropped | PENDING 30초 내 0 | 단계 뒤 전수 대조 | 자원 | `passed` |
|---|---|---|---|---|---|---|---|---|---|---|
| warmup | 10RPS 30초 | 0 | 39.3 / 43.3 | 301/301 | 0 | 0 | 예 | PASS | 통과 | true |
| normal | 50RPS 5분 | 99 | 493.0 / 1234.6 | **14,700/15,001**(98.0%, 나머지 301) | 0 | 0 | 예 | PASS | false(아래) | false |
| spike_recovery | 100RPS 30초 → 50RPS 60초 | 99 | 전체 **5919.0 / 6368.7** · spike 6015.5 / 6667.0 · recovery 3696.9 / 5768.5 | spike 2,313/2,458 · recovery 2,770/2,879 | 0 | **664** | 예 | PASS | 통과 | false |
| soak | 30RPS 15분 | 99 | 81.4 / 145.1 | **26,965/27,001**(나머지 36) | 0 | 0 | 예 | PASS | false(아래), RSS 증가 +0.10% | false |
| capacity25 | 25RPS 2분 | 99 | 58.7 / 79.3 | 2,999/3,000 | 0 | 0 | 예 | PASS | 통과 | false |
| capacity50 | 50RPS 2분 | 99 | 305.7 / 1097.0 | 5,897/6,001 | 0 | 0 | 예 | PASS | 통과 | false |
| capacity100 | 100RPS 2분 | 99 | 6118.4 / 7480.8 | 8,762/9,246 | 0 | 2,755 | 예(217 → 0) | PASS | 통과 | false |
| capacity200 | 200RPS 2분 | 99 | 6160.6 / 7432.3 | 8,821/9,330 | 0 | 14,671 | 예(296 → 0) | PASS | 통과 | false |

- SLO 문언(`acceptance.md` 부하 SLO 2): p95 < 500ms, p99 < 1500ms, 예상 못 한 5xx < 0.1%, dropped_iterations = 0. `workload.js`는 여기에 "모든 응답이 동기 확정(신규 201 / 재전송 200 + SUCCEEDED)"(`committed_response rate==1`)을 임계로 둔다.
- normal: 지연·dropped·5xx는 임계 안, **동기 확정이 아닌 응답 301건**으로 k6 exit 99. k6는 응답 본문을 남기지 않아 그 301건의 상태 코드는 원자료에 없다(5xx·연결 실패는 0건이므로 2xx~4xx — 구현의 202 PENDING 경로로 추정). soak도 같은 양상(36건).
- spike_recovery: 지연 임계 초과(p95 5.9초), dropped 664. 5xx 0.
- normal·soak의 `resources_passed: false` 원인은 **측정 도구 쪽 경합**: 표본을 뜨는 사이 끝난 프로세스의 `/proc/<pid>/status`를 awk가 열지 못해 관측 명령 exit≠0(normal 55개 중 1개 — app, soak 162개 중 2개 — app·db). OOM·재시작·app→DB 연결 10 초과는 없음(연결 최대 7). 파일럿 03에서도 같은 일이 있었다. 두 단계는 이와 무관하게 k6 exit 99다.
- 부하 종료 뒤: PENDING 0(15:08:29Z, `v3/post-load-pending.txt`), 전수 대조 PASS — payments 71,619 전부 SUCCEEDED, ledger 358,095, 은행 journal 71,619, 집계 9개 0(`v3/post-load-audit.json`). 거래 유실·이중 처리는 관측되지 않음.
- 앱 로그 `v3/app.log.gz` 3,462줄, ` ERROR ` 0줄, ` WARN ` 0줄.

## 판정기 status 요약(이번 회차는 v1·v2·v3를 모두 돌림)
PASS 19개(C01~C19), C20 `c20_passed: false`.

## 가장 앞선 미충족 항목(피드백 대상)
C20(P1). 보낸 원문 `feedback-009.txt`.

## C01~C20 원자료 위치(이 폴더 기준)

| ID | 판정기가 낸 status | 원자료 |
|---|---|---|
| C01~C12 | PASS (HTTP+DB+BANK) | `v1/acceptance-http.json` cases.<ID>, 위 표의 요청 색인 |
| C13·C14 | PASS | `v2/acceptance-faults.json` cases.C13·C14 |
| C15·C16·C18 | PASS (HTTP+DB+BANK) | `v1/acceptance-http.json` cases.<ID> |
| C17 | PASS | `v2/acceptance-faults.json` cases.C17(+`late_success`) |
| C19 | 어댑터 audit PASS(세 stack 각각) | `v1/acceptance-audit.json`, `v2/acceptance-audit.json`, `v3/post-load-audit.json`, 단계별 `v3/load/<phase>/audit-stdout.json` |
| C20 | `c20_passed: false` | `v3/load/summary.json`, `v3/load/<phase>/{result.json,k6-summary.json,stdout.txt,resources.jsonl,k6-inspect.json}`, `v3/load-check.stdout`, `v3/post-load-pending.txt`, `v3/host-samples.txt`, `v3/app.log.gz` |

공통: `adapter.py`·`run_http.py`·`run_faults.py`·`pending.sql`·`run-v1-v2.sh`·`run-v3.sh`, `adapter-unittest.txt`, `commands.log`, `source-head.txt`, `README.md`.
