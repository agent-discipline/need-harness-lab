# 시나리오별 관측 메모 — claude-pilot-04, 판정 11회째(턴 0011 = 피드백 10 [C20] 2회째 뒤, 최종 전체 판정)

진행자의 관측. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 8회째와 같다(`README.md` → `../r08/README.md` → `../r03/README.md` → `../r01/README.md`, 음성 대조 21건 통과, 새 허용 규칙 없음).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 40줄, `source-head.txt`). 실행 2026-11-18T17:52:59Z~, **v1 + v2 + v3 전부**. 이 판정은 run의 8시간 상한(18:18:09Z)에 걸쳐 돌았다(판정은 유료 호출이 아님 — 메인 지시).

## v1 (`v1/acceptance-http.json` 요청 471건, `v1/acceptance-audit.json`) — `run_http.py` exit 2(FAIL 항목 없음), `cx-` 2개 실행 중

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01~C09 | PASS (HTTP+DB+BANK) | [0,391] | 10회째와 같음(C07: 409 50건이 무효과 FAILED 행 — 허용 규칙 2) |
| C10 | PASS | [391,392] | decline → 422, FAILED 행(홀드 해제), 은행 기록 없음 — 허용 규칙 1·2 |
| C11 | PASS | [392,411] | timeout-before 202(0.63초)·5xx 202 → 조회 반복 뒤 둘 다 SUCCEEDED |
| C12 | PASS | [411,413] | timeout-after 201 SUCCEEDED(0.64초), 은행 기록 1건 |
| C15 | PASS | [413,418] | 정산 201, 재전송 200 같은 settlementId |
| C16 | PASS | [418,469] | 동시 50 모두 201, paymentCount 합 1 |
| C18 | PASS | [469,471] | 빈 정산 201 0건·0원, 재전송 200 |

- 마지막 전수 대조: PASS, errors 0, 집계 9개 0. payments 115(SUCCEEDED 64 / FAILED 51), ledger 332, settlements 52, 은행 64. 최대 응답 1.04초.

## v2 (`v2/acceptance-faults.json`, `v2/acceptance-audit.json`) — `run_faults.py` exit 0, `cx-` 2개 실행 중

| ID | 판정기 status | 관측 |
|---|---|---|
| C13 | PASS | app SIGKILL(은행 기록 있음 + DB PENDING 확인 뒤). 처음 요청 연결 끊김. healthy까지 16.3초, 그 뒤 5.2초에 SUCCEEDED. 재전송 200, audit PASS |
| C14 | PASS | db SIGKILL(같은 경계 확인 뒤). **처음 요청의 응답이 201**(앞 회차들은 202 — DB가 돌아온 뒤 요청 스레드가 확정까지 마친 것으로 보임). healthy까지 5.7초, 그 뒤 5.1초에 SUCCEEDED 확인. 재전송 200, audit PASS |
| C17 | PASS | 경계 fixture 3건·늦은 추가 1건·늦은 성공(확정 날짜 2026-11-19 KST) 모두 단언 통과 |

- `final_audit` PASS: payments 7(모두 SUCCEEDED), ledger 45, settlements 6, 은행 7. 30초 수렴 항목은 5.1~5.2초.

## v3 — 부하·회복 (C20) (`v3/load/summary.json`, 단계별 `v3/load/<phase>/`)

새 volume. stack 18:00:06Z(그때 이미 `cx-` 0 — 대기 없음), 부하 18:00:06Z~18:32:46Z, `load_check.py` exit 1. 부하 중 30초 표본 66개 모두 `cx=0`, 종료 시 0개. 한도는 앞 회차와 같음.

`summary.json`: `c20_passed: false`, `phase_gates_passed: false`, `sustainable_rps: 25`.

| 단계 | 부하 | k6 exit | p95 / p99 (ms) | 동기 확정 응답 | 5xx·연결 실패 | dropped | PENDING 30초 내 0 | 단계 뒤 전수 대조 | 자원 | `passed` |
|---|---|---|---|---|---|---|---|---|---|---|
| warmup | 10RPS 30초 | 0 | 45.6 / 80.9 | 301/301 | 0 | 0 | 예 | PASS | 통과 | true |
| normal | 50RPS 5분 | 99 | 369.7 / 1046.3 | **14,947/15,001**(나머지 54) | 0 | 0 | 예 | PASS | 통과 | false |
| spike_recovery | 100RPS 30초 → 50RPS 60초 | 99 | 전체 **5991.0 / 6151.2** · spike 6066.7 / 6262.0 · recovery 3140.3 / 5710.7 | spike 2,073/2,445 · recovery 2,785/2,880 | 0 | **676** | 예 | PASS | 통과 | false |
| soak | 30RPS 15분 | 0 | 77.6 / 138.5 | 27,001/27,001 | 0 | 0 | 예 | PASS | 통과, RSS 증가 +0.43% | **true** |
| capacity25 | 25RPS 2분 | 0 | 52.1 / 90.9 | 3,001/3,001 | 0 | 0 | 예 | PASS | 통과 | true |
| capacity50 | 50RPS 2분 | 99 | 427.2 / 1183.9 | 5,947/6,001 | 0 | 0 | 예 | PASS | 통과 | false |
| capacity100 | 100RPS 2분 | 99 | 6209.8 / 6328.7 | 7,085/8,520 | 0 | 3,481 | 예(1,047 → 0) | PASS | 통과 | false |
| capacity200 | 200RPS 2분 | 99 | 6178.3 / 6299.0 | 7,231/8,598 | 0 | 15,402 | 예(907 → 0) | PASS | 통과 | false |

- 세 번의 v3 비교(9회째 → 10회째 → 11회째): normal의 동기 확정 아닌 응답 301 → 100 → 54(p95 493 → 467 → 370), spike_recovery p95 5919 → 3742 → **5991**·dropped 664 → 305 → **676**(11회째에 다시 나빠짐), soak 비동기 36 → 0 → 0. 이번에는 측정 도구의 `/proc` 경합이 없었다(자원 관측 실패 0건) — soak `passed: true`.
- 5xx·연결 실패는 세 번 모두 0. app→DB 연결 최대 6.
- 부하 종료 뒤: PENDING 0(18:32:46Z), 전수 대조 PASS — payments 70,222 전부 SUCCEEDED, ledger 351,110, 은행 70,222, 집계 9개 0. 앱 로그 `v3/app.log.gz` ` ERROR ` 0줄, ` WARN ` 0줄.

## 판정기 status 요약
PASS 19개(C01~C19), C20 `c20_passed: false`. 이 뒤에는 피드백을 보내지 않았다(8시간 상한 18:18:09Z 경과, 그리고 C20은 연속 3회째 미충족).

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
