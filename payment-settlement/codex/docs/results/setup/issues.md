# 준비 과정 문제 기록

## SETUP-001 — 컨테이너에서 Codex 인증 파일 접근 실패

- 상태: RESOLVED
- 발생 상황: root 사용자와 cap-drop ALL 조합에서 호스트 uid 1000의 mode 0600 인증 파일을 읽지 못해 최초 CLI 호출이 401로 실패했다.
- 원인: 컨테이너 사용자와 파일 소유자가 다르고 DAC 우회 capability가 제거되어 있었다.
- 해결 방법: runner를 호스트 파일 소유자와 같은 uid 1000의 node 사용자로 실행하고 인증 파일은 읽기 전용으로 유지했다.
- 검증: setup-cli-smoke2 최초 호출과 동일 thread resume 성공. 증거: codex/turn-1-raw.jsonl, codex/turn-2-raw.jsonl. 실패 원본: codex/.runs/setup-cli-smoke (저장소 제외).

## SETUP-002 — resume 사용량의 중복 합산 위험

- 상태: RESOLVED
- 발생 상황: CLI turn.completed의 사용량이 매 턴 증가하는 세션 누적값이었다.
- 원인: 누적값을 턴별 사용량으로 해석하면 앞선 턴의 사용량을 다시 합산한다.
- 해결 방법: 원본 누적값을 보존하고 직전 누적값과의 차이를 delta로 정규화했다.
- 검증: 두 번째 누적 input 26,213/output 17, 실제 총계 26,230. 단순 합산 39,323은 잘못된 값. 증거: codex/usage-normalized.jsonl, codex/summary.json 및 measurement/runner 테스트.

## SETUP-003 — 전체 수용 조건 판정기 미완성

- 상태: OPEN
- 발생 상황: 현재 k6 시작 스크립트는 C01~C20 전체가 아닌 일부만 검증한다.
- 원인: 전체 장애 주입·회계 대조·부하 시나리오 구현이 남아 있다.
- 해결 방법: 전체 판정기와 negative control을 완성한 뒤 본 실험을 시작한다.
- 검증: 미완료. 파일럿 일부 통과를 전체 통과로 간주하지 않는다.

## SETUP-004 — 준비 에이전트 사용량 제한으로 중단

- 상태: RESOLVED (작업 재개 확인)
- 발생 상황: 전체 판정기·Fable runner·하네스 adapter 준비 중 세 에이전트가 사용량 제한 오류로 중단됐다.
- 원인: 도구가 보고한 계정 사용량 한도 도달. 한도 내부 계산은 미확인.
- 해결 방법: 기존 파일과 기록을 보존하고 2026-09-24 사용자 재개 요청 후 같은 준비 작업을 재개했다.
- 검증: 세 작업 재개 메시지와 실제 Fable 리뷰 결과 수신. 준비 작업 자체의 완성 여부는 별도 판정한다.
