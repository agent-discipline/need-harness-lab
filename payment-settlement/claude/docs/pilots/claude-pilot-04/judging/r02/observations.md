# 시나리오별 관측 메모 — claude-pilot-04, 판정 2회째(턴 0002 = 피드백 1 [C01] 뒤)

진행자가 판정기 산출물에서 옮겨 적은 관측이다. 통과 여부는 적지 않는다. 어댑터·허용 규칙은 1회째와 같다(`../r01/README.md` — 스키마가 `ledger` 뷰 추가 말고는 같아 `adapter.py`를 그대로 복사, 음성 대조 14건 통과 `adapter-unittest.txt`).

판정 대상: 작업 트리(HEAD `ec7b322 starter` + 미커밋 33줄, 내용 해시 `36e92fb8…18fd` — `source-head.txt`). 실행 2026-11-18T12:19:22Z~12:23:14Z, **v1만**(메인 지시: v2는 v1의 P0 HTTP 항목이 모두 통과한 뒤부터, v3는 C01~C19 통과 뒤부터 — **C13·C14·C17·C20은 이번 회차 미실행, 직전 결과 유지**). `cx-` 2개 실행 중인 채 실행(v1은 기다리지 않음).

## v1 (`v1/acceptance-http.json` 요청 400건, `v1/acceptance-audit.json`) — `run_http.py` exit 1

| ID | 판정기 status | 요청 색인 | 관측 |
|---|---|---|---|
| C01 | FAIL | [0,1] | **HTTP 단언은 통과**(201, SUCCEEDED). 시나리오 뒤 어댑터 audit가 FAIL: `agency_balance_vs_settlements` 1건 — 정산 요청이 없었는데 대행사 정산 잔액이 결제 성공 시점에 증가(구현의 실시간 반영 모델, `info.agency_balance_vs_succeeded_net_mismatches` 0 = 잔액이 성공 결제 net 합과 같음). 그 밖의 집계 8개는 0. **어댑터에 허용 규칙을 넣으면 PASS가 될 항목 — 넣지 않음(메인 규칙 다)** |
| C02 | FAIL | [1,4] | amount −1·0은 400. amount 1000001 → **422** FAILED `INSUFFICIENT_BALANCE`(기대 400), FAILED 행 저장. 이후 입력은 보내지 않음 |
| C03 | FAIL | [4,6] | 2건 모두 404(HTTP 단언 통과). audit가 위와 같은 `agency_balance_vs_settlements`로 FAIL |
| C04 | FAIL | [6,27] | HTTP 단언 통과(201 뒤 200×20, 같은 paymentId). audit 같은 사유 |
| C05 | FAIL | [27,30] | HTTP 단언 통과(409×2). audit 같은 사유 |
| C06 | FAIL | [30,230] | HTTP 단언 통과(동일 키 동시 100, paymentId 1개, 조회 100회). audit 같은 사유 |
| C07 | FAIL | [230,330] | 허용 밖 상태 코드: 잔액 부족이 **422**(기대 409) — 201과 422가 섞임 |
| C08 | FAIL | [330,332] | 대행사 902는 201, 같은 키의 대행사 903은 **409**(기대 201 — 키 범위가 대행사별이 아님) |
| C09 | FAIL | [332,335] | 헤더≠body 대행사는 403. 다른 대행사(902)의 조회가 **200**(기대 404) |
| C10 | FAIL | [335,336] | `X-Bank-Fault: decline` → 422가 아님("decline must return 422") |
| C11 | FAIL | [336,340] | HTTP 단언 통과. audit 같은 사유(`agency_balance_vs_settlements` 2) |
| C12 | FAIL | [340,342] | HTTP 단언 통과. audit 같은 사유 |
| C15 | FAIL | [342,347] | 결제 fixture 3건 성공, `POST /api/settlements` 2건 404 "no such endpoint" |
| C16 | FAIL | [347,398] | `POST /api/settlements` 50건 404 |
| C18 | FAIL | [398,400] | `POST /api/settlements` 2건 404 |

- 마지막 전수 대조(`v1/acceptance-audit.json`): FAIL, errors `agency_balance_vs_settlements`(4개 대행사). counts: payments 115(SUCCEEDED 64 / FAILED 51), ledger 320, 은행 journal 64. 나머지 집계 0, 행 단위 대조(reconcile) 오류 0.
- C11·C12는 `X-Bank-Fault`가 전달되지 않아 장애 없이 201이 된 것일 수 있다(구현은 `X-Fault`만 읽음) — C10의 실패와 같은 원인으로 보이며 원자료는 requests[335:342].

## 가장 앞선 미충족 항목(피드백 대상)
C01 — 판정기 status FAIL(HTTP 단언 통과, 전수 대조에서 대행사 정산 잔액 불일치). 보낸 원문 `feedback-002.txt`.

## C01~C20 원자료 위치(이 폴더 기준)
C01~C12·C15·C16·C18: `v1/acceptance-http.json` cases.<ID>와 위 표의 요청 색인. C19: `v1/acceptance-audit.json`(FAIL). C13·C14·C17: 이번 회차 미실행 — `../r01/v2/acceptance-faults.json`. C20: 이번 회차 미실행 — `../r01/v3/load/summary.json`. 명령 `commands.log`.
