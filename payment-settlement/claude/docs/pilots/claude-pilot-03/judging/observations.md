# 시나리오별 관측 메모 — claude-pilot-03

진행자가 판정기 산출물에서 옮겨 적은 **관측**이다. 통과 여부는 적지 않는다. 표의 "판정기 status"는 판정기·어댑터가 기계적으로 낸 칸의 값 그대로다(`acceptance.py`의 `check`: 콜백 단언 + 어댑터 audit PASS + verify_requests 오류 0이면 `PASS`·scope `HTTP+DB+BANK`).

판정 대상: 작업 폴더 HEAD `e227e10`(브랜치 `feat/payment-settlement-v1`, 미커밋 0). 판정 실행 2026-11-18T07:12Z~ (시작·각 stack 시작 전 `cx-` 컨테이너 0개 확인 — `commands.log`, v3 실행 중 표본은 `v3/host-samples.txt`).

## v1 — HTTP 시나리오 + 전수 대조 (`v1/acceptance-http.json`, 요청 517건 / `v1/acceptance-audit.json`)

새 volume(users 1,000,000 / agencies 1,000 / payments 0). `run_http.py` exit 2(= FAIL 항목 없음, 판정기는 전체 합격을 주장하지 않음).

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01 | PASS (HTTP+DB+BANK) | [0,1] | POST 201, body status SUCCEEDED. 첫 요청 0.98초 |
| C02 | PASS | [1,10] | 금액 −1·0·1000001·1.5, 통화 USD, 필드 4종 누락 = 9건 모두 400 `INVALID_REQUEST`. 전후 snapshot 행 수 변화 없음 |
| C03 | PASS | [10,12] | 없는 사용자 404 `USER_NOT_FOUND`, 없는 대행사 404 `AGENCY_NOT_FOUND` |
| C04 | PASS | [12,33] | 최초 201, 같은 키 20회 모두 200·같은 paymentId |
| C05 | PASS | [33,36] | 같은 키 다른 금액·다른 사용자 2건 모두 409 `IDEMPOTENCY_CONFLICT` |
| C06 | PASS | [36,236] | 동일 키 동시 100: 201 1건 / 202 16건 / 200 83건, paymentId 1개. 전후 payments 3→4, ledger 9→12(1건분) |
| C07 | PASS | [236,386] | 한 사용자 다른 키 동시 100 × 20,000원: 201 50건 / 409 50건(성공 총액 1,000,000). 409 50건은 FAILED `INSUFFICIENT_BALANCE` 행으로 저장됨(원장·은행 기록 없음 — 어댑터 README 1번). 최대 응답 1.60초 |
| C08 | PASS | [386,388] | 대행사 902·903 같은 키 각각 201, paymentId 다름 |
| C09 | PASS | [388,391] | 헤더≠body 대행사 403 `AGENCY_MISMATCH`, 다른 대행사의 조회 404 `PAYMENT_NOT_FOUND` |
| C10 | PASS | [391,392] | `X-Bank-Fault: decline` → 422 `{code: BANK_DECLINED, …}`. DB에 FAILED `BANK_DECLINED` 1행, 은행 journal 없음 |
| C11 | PASS | [392,437] | timeout-before: 202 PENDING(1.03초) / 5xx: 202 PENDING(0.02초). 조회 43회, 두 건 합쳐 약 11초 뒤 둘 다 SUCCEEDED |
| C12 | PASS | [437,459] | timeout-after: 202 PENDING(1.02초), 조회 21회 약 5.4초 뒤 SUCCEEDED. 은행 기록 1건 |
| C15 | PASS | [459,464] | 결제 101·199·10001 → 정산 201 gross 10301 / fee 102 / net 10199 / count 3, 재전송 200 같은 settlementId·같은 값 |
| C16 | PASS | [464,515] | 같은 날짜 정산 동시 50(서로 다른 키): 50건 모두 201, paymentCount 합 1(1건이 1, 49건이 0). settlement_items 3→4 |
| C18 | PASS | [515,517] | 2000-01-01 정산 201 0건·0원, 재전송 200 같은 settlementId |
| C13·C14·C17·C19·C20 | NOT_RUN | — | 이 runner 범위 밖(아래 v2·v3, 전수 대조) |

- 시나리오마다 어댑터 audit와 verify_requests가 함께 돌았다(각 case의 `audit`·`snapshots` 칸).
- **C19(전수 대조, `v1/acceptance-audit.json`)**: 어댑터 status PASS, errors 0. counts: payments 115(SUCCEEDED 64 / FAILED 51 / PENDING 0), ledger 296, settlements 52, items 4, 은행 journal 64. 집계 11개 모두 0(사용자 1,000,000·대행사 1,000, 음수 잔액, 잔액 보존 2종, 보류 초과, 대행사 잔액, 원장 차·대 합, orphan 3종). `v1/payments-by-status.txt`.
- 앱 로그 `v1/app.log` 41줄, `ERROR` 0건.

## v2 — 장애·시각 시나리오 (`v2/acceptance-faults.json`, `v2/acceptance-audit.json`)

새 volume. `run_faults.py` exit 0.

| ID | 판정기 status | 관측 |
|---|---|---|
| C13 | PASS | `timeout-after` 결제에서 은행 journal에 기록이 생긴 것과 DB status `PENDING`을 확인한 뒤 app 컨테이너 SIGKILL. 처음 요청은 연결 끊김(`Remote end closed connection without response`). 재기동 healthy까지 21.8초, healthy 뒤 6.9초에 SUCCEEDED. 같은 키 재전송 200·같은 paymentId. 직후 audit PASS(payments 1 / ledger 3 / bank 1) |
| C14 | PASS | 같은 경계(은행 기록 있음 + DB `PENDING`)에서 db 컨테이너 SIGKILL. 처음 요청 응답 202 PENDING. db healthy까지 5.8초, 그 뒤 5.9초에 SUCCEEDED. 재전송 200. audit PASS(payments 2 / ledger 6 / bank 2) |
| C17 | PASS | 성공 결제 3건의 확정 시각을 2020-01-01T14:59:59Z·15:00:00Z·15:00:01Z로 주입(평가자 fixture, `business_date` 함께). 2020-01-01 정산 = 1건 101원, 2020-01-02 정산 = 2건 205원. 뒤늦게 15:00:02Z 1건 추가 → 같은 키 재전송은 처음 snapshot 그대로, 새 키 정산 = 1건 104원. 늦은 성공: 은행 SIGKILL 중 결제 202 → 오늘 정산 0건 → 은행 재기동 뒤 SUCCEEDED, 확정 날짜 = 복구한 날(2026-11-18) → 같은 키 재전송 0건 그대로, 새 키 정산 1건 107원, 2020-01-01 정산 0건 |

- 마지막 전수 대조(`final_audit`, `v2/acceptance-audit.json`): PASS, payments 7(모두 SUCCEEDED), ledger 33, settlements 6, items 5, bank 7.
- **검증 뒤 이 DB는 재사용하지 않았다**(시각 fixture 주입 — `evaluation/README.md`). `down -v`로 제거.

## v3 — 부하·회복 (C20) (`v3/load/summary.json`, 단계별 `v3/load/<phase>/`)

새 volume. `load_check.py`(공유 `k6/workload.js`, k6 컨테이너 2 CPU·1024MB, app 1.5 CPU·1536MB, db 1.5 CPU·2048MB, bank 0.5 CPU·256MB — `compose.yaml` 한도) exit 1. 07:21:13Z~07:54:27Z. 실행 중 30초 간격 표본 67개 모두 `cx-` 컨테이너 0개, 호스트 load average 3.4~5.3(`v3/host-samples.txt` — 같은 머신에 `alert-system-*` 컨테이너 11개가 떠 있었다).

`summary.json`: `c20_passed: false`, `phase_gates_passed: false`, `sustainable_rps: 25`.

| 단계 | 부하 | k6 exit | p95 / p99 (ms) | 동기 확정 응답(201·200 + SUCCEEDED) | 5xx·연결 실패 | dropped | PENDING 30초 내 0 | 단계 뒤 전수 대조 | 자원 | `passed` |
|---|---|---|---|---|---|---|---|---|---|---|
| warmup | 10RPS 30초 | 0 | 82.8 / 93.9 | 301/301 | 0 | 0 | 예 | PASS | 통과 | true |
| normal | 50RPS 5분 | 99 | **1165.0 / 1886.7** | 14,755/14,991 (98.4%, 나머지 236) | 0 | **9** | 예 | PASS(payments 14,568 전부 SUCCEEDED) | **false**(아래) | false |
| spike_recovery | 100RPS 30초 → 50RPS 60초 | 99 | 전체 1183.0 / 1556.6 · spike 1361.7 / 1600.0 · recovery 971.3 / 1450.1 | spike 2,395/3,001(79.8%) · recovery 2,976/2,995(99.4%) | 0 | **5** | 예 | PASS | 통과 | false |
| soak | 30RPS 15분 | 0 | 97.0 / 182.6 | 27,001/27,001 | 0 | 0 | 예 | PASS(45,956 전부 SUCCEEDED) | 통과, RSS 증가 +0.15% | true |
| capacity25 | 25RPS 2분 | 0 | 77.1 / 86.8 | 3,000/3,000 | 0 | 0 | 예 | PASS | 통과 | true |
| capacity50 | 50RPS 2분 | 99 | **563.5** / 710.6 | 6,000/6,000 | 0 | 0 | 예 | PASS | 통과 | false |
| capacity100 | 100RPS 2분 | 99 | 1567.4 / 1923.0 | 7,417/12,000(61.8%) | 0 | 0 | **아니오**(4,251 → 30초 뒤 2,823) | FAIL(PENDING 2,809) | 통과 | false |
| capacity200 | 200RPS 2분 | 99 | 1233.5 / 1698.7 | 7,904/24,001(32.9%) | 0 | 0 | **아니오**(18,083 → 30초 뒤 16,933) | FAIL(PENDING 16,922) | 통과 | false |

- SLO 문언(`acceptance.md` 부하 SLO 2): p95 < 500ms, p99 < 1500ms, 예상치 못한 5xx < 0.1%, dropped_iterations = 0. normal(필수 단계)에서 p95 1165ms·p99 1887ms·dropped 9, spike_recovery(필수)에서 p95·p99 초과·dropped 5.
- "동기 확정 응답"이 아닌 것은 5xx가 아니다(`unexpected_failure` 0건, `http_req_failed` 0건) — k6가 201/200 + SUCCEEDED가 아닌 응답으로 센 것으로, 구현 설정상 은행 호출 대기 줄을 넘긴 결제를 202 PENDING으로 돌려주는 경로에 해당한다(구현의 `bank.max-concurrent-calls: 1`·`max-queued-calls: 56`·`max-queue-wait-ms: 2000`, 앱 로그에 `bank … failed: not sent` WARN 25,451줄). 응답 본문을 k6가 남기지 않으므로 202라는 것은 구현 설정과 DB(PENDING → 뒤에 SUCCEEDED)로부터의 추정이다.
- normal 단계의 `resources_passed: false` 원인: 표본 1개에서 **은행 컨테이너** 관측 명령이 `awk: cannot open "/proc/955/status" (No such file or directory)`로 exit≠0(표본을 뜨는 사이 프로세스가 끝난 측정 도구 쪽 경합). OOM·재시작·app DB 연결 10 초과는 없었다(app→DB 연결 최대 4). 이 단계는 그와 무관하게 k6 exit 99다.
- app→DB 연결 최대: normal 4, spike 5, soak 5, capacity100·200에서 10(풀 상한). app RSS 304~357MB, OOM·재시작 0.
- **부하 종료 뒤 수렴**(`v3/post-load-pending.txt`, `v3/post-load-audit.json`): capacity200 종료(07:53:34Z) 뒤 PENDING 14,536(07:55:12Z) → 0(08:01:02Z), 약 7분 30초. 그 뒤 전수 대조 PASS: payments 88,787 전부 SUCCEEDED, ledger 266,361, 은행 journal 88,787, 집계 11개 모두 0. 거래 유실·이중 처리는 관측되지 않았다.
- capacity100·200 직후 audit의 `errors`는 어댑터가 앞 200개만 남겼다(`error_count` 3,254·17,408 중 표시분은 모두 `pending:`). PENDING 수(2,809·16,922)보다 오류 수가 많은 차이는 종류를 확인하지 못했다(은행에 기록됐으나 DB가 아직 PENDING인 건의 `bank_presence`·`ledger_count`일 가능성 — 수렴 뒤 대조에서는 0).
- 앱 로그 `v3/app.log.gz`(메인이 압축, 원본 10MB): ` ERROR ` 0줄, ` WARN ` 25,451줄.
- 구현 PM이 턴 0004 최종 응답에서 스스로 적은 것과 같은 방향의 관측: "50RPS 고정 부하는 이 환경에서 은행 처리량(초당 47~73건)의 경계", "100RPS spike에서 쌓인 PENDING은 여유분만큼만 해소".

## 실행하지 않은 것
- 수용 기준 문서의 **독립 블라인드 설계 품질 리뷰**(Claude Fable, 12점 만점)는 C01~C20 밖의 별도 완료 조건이고 이 판정 절차에 포함하지 않았다(런북에 절차 없음 — 메인 결정 사항).
- 부하 단계의 hot-key·hot-account 별도 보고(SLO 절 마지막 문단)는 `workload.js`에 없어 하지 않았다.

## C01~C20 원자료 위치

| ID | 판정기가 낸 status | 원자료(이 폴더 기준) |
|---|---|---|
| C01 | PASS (HTTP+DB+BANK) | `v1/acceptance-http.json` cases.C01, requests[0:1] |
| C02 | PASS | `v1/acceptance-http.json` cases.C02, requests[1:10] |
| C03 | PASS | `v1/acceptance-http.json` cases.C03, requests[10:12] |
| C04 | PASS | `v1/acceptance-http.json` cases.C04, requests[12:33] |
| C05 | PASS | `v1/acceptance-http.json` cases.C05, requests[33:36] |
| C06 | PASS | `v1/acceptance-http.json` cases.C06, requests[36:236] |
| C07 | PASS | `v1/acceptance-http.json` cases.C07, requests[236:386]; `v1/payments-by-status.txt` |
| C08 | PASS | `v1/acceptance-http.json` cases.C08, requests[386:388] |
| C09 | PASS | `v1/acceptance-http.json` cases.C09, requests[388:391] |
| C10 | PASS | `v1/acceptance-http.json` cases.C10, requests[391:392] |
| C11 | PASS | `v1/acceptance-http.json` cases.C11, requests[392:437] |
| C12 | PASS | `v1/acceptance-http.json` cases.C12, requests[437:459] |
| C13 | PASS | `v2/acceptance-faults.json` cases.C13 |
| C14 | PASS | `v2/acceptance-faults.json` cases.C14 |
| C15 | PASS | `v1/acceptance-http.json` cases.C15, requests[459:464] |
| C16 | PASS | `v1/acceptance-http.json` cases.C16, requests[464:515] |
| C17 | PASS | `v2/acceptance-faults.json` cases.C17(+`late_success`) |
| C18 | PASS | `v1/acceptance-http.json` cases.C18, requests[515:517] |
| C19 | 어댑터 audit PASS(세 stack 각각) | `v1/acceptance-audit.json`, `v2/acceptance-audit.json`, `v3/post-load-audit.json`, 단계별 `v3/load/<phase>/audit-stdout.json` |
| C20 | `c20_passed: false` | `v3/load/summary.json`, `v3/load/<phase>/{result.json,k6-summary.json,stdout.txt,resources.jsonl,k6-inspect.json}`, `v3/post-load-pending.txt`, `v3/host-samples.txt`, `v3/app.log.gz` |

공통: 어댑터·구동 스크립트 `adapter.py`·`run_http.py`·`run_faults.py`·`pending.sql`, 어댑터 음성 대조 `adapter-unittest.txt`, 명령·종료 코드 `commands.log`, 판정 대상 커밋 `source-head.txt`, 어댑터 설명 `README.md`.
