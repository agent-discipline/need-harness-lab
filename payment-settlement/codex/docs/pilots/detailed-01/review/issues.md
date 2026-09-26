# 독립 리뷰 준비 중 문제

## R01: 검증 스크립트/은행 계약 packet 누락

- 발생: attempt-01 Fable이 restart regression과 은행 계약을 확인할 수 없다고 보고했다.
- 원인: 익명화 allowlist가 src와 빌드파일만 포함하여 구현의 scripts/restart-recovery.sh와 공통 infra 계약을 누락했다.
- 해결: scripts 및 공통 bank-contract.md를 다음 실행 packet에 추가했다. 첫 리뷰 입력/원본 결과는 보존했다.
- 검증: runner allowlist와 복사 로직 확인. 기존 리뷰는 제한이 있는 제외 파일럿 자료이며 정식 완성도 합격으로 사용하지 않는다.
- 관련: attempt-01/prompt.txt, attempt-01/review.json, attempt-01/summary.json.

## R02: 별칭 모델 버전 가변성

- 발생: 요청 fable은 실제 claude-fable-5-1로 해석되었다.
- 원인: 별칭은 제공자에 의해 변경될 수 있다.
- 해결: 정식 runner 기본값 claude-fable-5-1 고정, fallback 미설정. 매 실행 actual 모델 보존.
- 검증: 실제 응답 message.model 및 result.modelUsage 일치.
