# 시나리오별 관측 메모 — claude-pilot-04, 판정 10회째(턴 0010 = 피드백 9 [C20] 뒤, 전체 판정)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 8회째와 같다(`README.md`, 음성 대조 21건 통과, 새 허용 규칙 없음).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 40줄, `source-head.txt`). 실행 2026-11-18T16:17:32Z~, **v1 + v2 + v3 전부**(메인 지시).

## v1 (`v1/acceptance-http.json` 요청 470건, `v1/acceptance-audit.json`) — `run_http.py` exit 2(FAIL 항목 없음), `cx-` 2개 실행 중

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01~C09 | PASS (HTTP+DB+BANK) | [0,391] | 9회째와 같음(C07: 409 50건이 무효과 FAILED 행 — 허용 규칙 2) |
| C10 | PASS | [391,392] | decline → 422, FAILED 행(홀드 해제), 은행 기록 없음 — 허용 규칙 1·2 |
| C11 | PASS | [392,410] | timeout-before 202(**0.64초** — 은행 timeout이 600ms로 바뀜)·5xx 202 → 조회 반복 뒤 둘 다 SUCCEEDED |
| C12 | PASS | [410,412] | timeout-after 201 SUCCEEDED(0.64초), 은행 기록 1건 |
| C15 | PASS | [412,417] | 정산 201, 재전송 200 같은 settlementId |
| C16 | PASS | [417,468] | 동시 50 모두 201, paymentCount 합 1 |
| C18 | PASS | [468,470] | 빈 정산 201 0건·0원, 재전송 200 |

- 마지막 전수 대조: PASS, errors 0, 집계 9개 0. payments 115(SUCCEEDED 64 / FAILED 51), ledger 332, settlements 52, 은행 64. 최대 응답 1.04초. 앱 로그 44줄, ` ERROR ` 0줄.

## v2 (`v2/acceptance-faults.json`, `v2/acceptance-audit.json`) — `run_faults.py` exit 0, `cx-` 2개 실행 중

| ID | 판정기 status | 관측 |
|---|---|---|
| C13 | PASS | app SIGKILL(은행 기록 있음 + DB PENDING 확인 뒤). healthy까지 21.6초(기동 뒤 warm-up 동안 `/health` 503 포함), 그 뒤 4.4초에 SUCCEEDED. 재전송 200, audit PASS |
| C14 | PASS | db SIGKILL. 처음 요청 202. healthy까지 6.4초, 그 뒤 9.0초에 SUCCEEDED. 재전송 200, audit PASS |
| C17 | PASS | 경계 fixture 3건·늦은 추가 1건·늦은 성공(확정 날짜 2026-11-19 KST) 모두 단언 통과 |

- `final_audit` PASS: payments 7(모두 SUCCEEDED), ledger 45, settlements 6, 은행 7. 30초 수렴 항목은 4.4~9.0초.

## v3 — 부하·회복 (C20) (`v3/load/summary.json`, 단계별 `v3/load/<phase>/`)

새 volume. stack 16:24:29Z, `cx-` 0 대기 14분(2분 간격 7회), 부하 16:38:30Z~17:11:06Z, `load_check.py` exit 1. 부하 중 30초 표본 66개 모두 `cx=0`, 종료 시 0개. 한도는 9회째와 같음.

`summary.json`: `c20_passed: false`, `phase_gates_passed: false`, `sustainable_rps: 25`.

| 단계 | 부하 | k6 exit | p95 / p99 (ms) | 동기 확정 응답 | 5xx·연결 실패 | dropped | PENDING 30초 내 0 | 단계 뒤 전수 대조 | 자원 | `passed` |
|---|---|---|---|---|---|---|---|---|---|---|
| warmup | 10RPS 30초 | 0 | 27.9 / 30.4 | 300/300 | 0 | 0 | 예 | PASS | 통과 | true |
| normal | 50RPS 5분 | 99 | 467.2 / 1132.3 | **14,900/15,000**(나머지 100) | 0 | 0 | 예 | PASS | 통과 | false |
| spike_recovery | 100RPS 30초 → 50RPS 60초 | 99 | 전체 **3742.2 / 4139.1** · spike 4011.8 / 4170.7 · recovery 957.5 / 2935.4 | spike 2,522/2,750 · recovery 2,933/2,946 | 0 | **305** | 예 | PASS | 통과 | false |
| soak | 30RPS 15분 | **0** | 60.4 / 92.8 | 27,000/27,000 | 0 | 0 | 예 | PASS | **false(측정 도구 경합)**, RSS 증가 +0.32% | **false** |
| capacity25 | 25RPS 2분 | 0 | 49.9 / 74.5 | 3,000/3,000 | 0 | 0 | 예 | PASS | 통과 | true |
| capacity50 | 50RPS 2분 | 99 | 350.8 / 855.4 | 5,979/6,001 | 0 | 0 | 예 | PASS | 통과 | false |
| capacity100 | 100RPS 2분 | 99 | 6191.1 / 6311.3 | 8,014/9,141 | 0 | 2,859 | 예(744 → 0) | PASS | 통과 | false |
| capacity200 | 200RPS 2분 | 99 | 6331.6 / 6463.7 | 7,675/8,952 | 0 | 15,048 | 예(857 → 0) | PASS | 통과 | false |

- 9회째 대비: normal의 동기 확정 아닌 응답 301 → 100, spike p95 5919 → 3742·dropped 664 → 305, soak 36 → 0, capacity25 통과(`sustainable_rps` null → 25).
- **soak는 k6 임계를 모두 통과(exit 0)했는데 `passed: false`** — 원인은 `resources_passed: false` 하나이고, 그것은 표본 2개에서 관측 명령이 끝난 프로세스의 `/proc/<pid>/status`를 열지 못한 측정 도구 쪽 경합이다(OOM·재시작·연결 초과 없음, app→DB 연결 최대 4). **구현과 무관한 판정 도구 쪽 사유로 soak 단계가 미통과로 기록된 것** — 메인 판정 때 볼 것. normal·spike_recovery는 이와 무관하게 k6 exit 99이므로 `c20_passed: false`는 그 두 단계로도 성립한다.
- 부하 종료 뒤: PENDING 0(17:11:06Z), 전수 대조 PASS — payments 71,497 전부 SUCCEEDED, ledger 357,485, 은행 71,497, 집계 9개 0. 앱 로그 `v3/app.log.gz` ` ERROR ` 0줄, ` WARN ` 0줄.

## 판정기 status 요약
PASS 19개(C01~C19), C20 `c20_passed: false`.

## 가장 앞선 미충족 항목(피드백 대상)
C20(P1) — 연속 2회째. 보낸 원문 `feedback-010.txt`(17:12Z, 8시간 상한 18:18:09Z까지 약 66분 남음).

## C01~C20 원자료 위치(이 폴더 기준)

| ID | 판정기가 낸 status | 원자료 |
|---|---|---|
| C01~C12 | PASS (HTTP+DB+BANK) | `v1/acceptance-http.json` cases.<ID> |
| C13·C14 | PASS | `v2/acceptance-faults.json` cases.C13·C14 |
| C15·C16·C18 | PASS (HTTP+DB+BANK) | `v1/acceptance-http.json` cases.<ID> |
| C17 | PASS | `v2/acceptance-faults.json` cases.C17(+`late_success`) |
| C19 | 어댑터 audit PASS(세 stack 각각) | `v1/acceptance-audit.json`, `v2/acceptance-audit.json`, `v3/post-load-audit.json`, `v3/load/<phase>/audit-stdout.json` |
| C20 | `c20_passed: false` | `v3/load/summary.json`, `v3/load/<phase>/{result.json,k6-summary.json,stdout.txt,resources.jsonl,k6-inspect.json}`, `v3/load-check.stdout`, `v3/post-load-pending.txt`, `v3/host-samples.txt`, `v3/app.log.gz` |

공통: `adapter.py`·`run_http.py`·`run_faults.py`·`pending.sql`·`run-v1-v2.sh`·`run-v3.sh`, `adapter-unittest.txt`, `commands.log`, `source-head.txt`, `README.md`.
