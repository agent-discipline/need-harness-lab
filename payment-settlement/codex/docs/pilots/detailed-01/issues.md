# 파일럿 문제 기록

## PILOT-001 — 제안된 토큰 상한 초과

- 상태: OPEN
- 발생 상황: 제외된 파일럿 첫 구현 턴에서 input 2,172,364 + output 30,590 = 2,202,954 tokens를 사용했다. cached input 2,110,848은 input의 부분집합이다.
- 원인: 상세 원인 미확인. 단일 구현 턴의 측정값이 계획의 잠정 200만 상한을 넘었다.
- 해결 방법: 본 시행 전 상한과 중도 중단 방식을 동결해야 한다. 파일럿은 30회 통계에서 제외한다.
- 검증 증거: codex/.runs/pilot-detailed-01/turns/0001/events.jsonl 및 result.json (저장소 제외).

## 첫 외부 검증 관측

새 Docker DB/bank에서 k6 payments.js 실행 exit 0, checks 28/28 통과, 129 requests, p95 318.07ms. 이는 기능 시나리오 결과이며 부하 SLO나 C01~C20 전체 통과를 의미하지 않는다. HTTP 실패 비율 3/129는 계약상 오류 응답과 함께 해석해야 하며 서비스 장애율로 그대로 사용하지 않는다. 이 단계에서 해결이 필요한 기능 실패는 관측되지 않았다.

## PILOT-002 — PostgreSQL KST 정산 경계 오류

- 상태: RESOLVED
- 발생: 성공확정 2026-09-23T23:44:13Z 결제를 9월24일 정산에서 누락했다. 외부 C15/C16/C17 실패.
- 원인: date AT TIME ZONE의 타입 변환으로 의도한 timestamptz 경계가 아닌 timestamp가 생성됐다.
- 해결: application에서 Asia/Seoul 날짜의 양쪽 자정을 Instant로 계산하고 저장소에 명시적 timestamptz 경계로 전달했다.
- 검증: fresh verify2에서 C15/C16/C17 PASS, HTTP/DB/은행 전수 대조. 증거 verification-02/http.json, faults.json, audit.json.
- 연결: 2번 프롬프트/분석, REVIEW_FIXES.md.

## PILOT-003 — 테스트 matcher NPE

- 상태: RESOLVED (구현자 실행 보고)
- 발생/원인: Kotlin Mockito matcher의 null과 non-null 타입 조합으로 회귀 테스트에서 NPE.
- 해결/검증: matcher 수정 후 모델이 단위/통합11개와 재시작 script 통과 보고. 원문 response-002.txt 및 REVIEW_FIXES.md. 이 개별 NPE를 외부에서 별도 재현한 것은 아니다.

## PILOT-004 — 정상 부하의 동기 성공 기준 미충족

- 상태: OPEN (제외 파일럿을 추가 튜닝하지 않고 관측값 보존)
- 발생: 50RPS/5분, iterations15,001, dropped0, 동기 성공 기준14,841/15,001로160건 미충족. k6 exit99.
- 관측: p95 153.98ms, p99 1008.16ms, 예상 밖5xx0. 최종 PENDING0 및 DB/은행 전수 대조·자원검증 PASS.
- 원인: 미확인. 개별 실패 응답은 workload가 보관하지 않아 상태코드를 추정으로 확정하지 않는다.
- 해결: 미해결. 부하 발생기 준비 성공과 후보 합격 실패를 분리한다. 본 시행 SLO는 그대로 유지한다.
- 증거: load-preparation/results/normal/k6-summary.json 및 result.json.
