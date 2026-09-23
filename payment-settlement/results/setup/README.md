# 초기 세팅 검증 결과

2026-09-23 준비 검증이다. **본 실험30회는 아직 NOT_STARTED**이며 아래 결과를 결제 구현 성능으로 해석하지 않는다.

| 검증 | 결과 |
|---|---|
| Kotlin/Spring `test bootJar` | 빌드 성공. 업무 테스트는 아직 없어 `test NO-SOURCE` |
| Docker app/DB/fake-bank | 모두 healthy |
| 사용자 seed | 정확히1,000,000명, 잔액합1,000,000,000,000 |
| 대행사 seed | 정확히1,000개, 수수료100bps, 초기 정산잔액0 |
| 30개 서버 동일성 | 공통 원본과18개 서버 파일 SHA256 동일 |
| k6 health/DB smoke | checks100%, exit0 |
| 업무 API 미구현 negative control | checks 실패, exit99 — 정상적으로 불합격 판정 |
| 계측/기록/fake-bank/runner 단위 검증 | 각각7/6/3/5개 통과 |
| 별도 Docker Codex 최초 호출 | `RUNNER_OK` 응답 |
| 같은 session ID로 resume | `RUNNER_OK_RESUMED` 응답 |
| 설정 | Codex0.155.1, gpt-6-sol/high, goals/memories 비활성 |

실제 응답·사용량은 [codex/summary.json](codex/summary.json), 인프라 수치는 [verification.json](verification.json)을 따른다. 성공한 두 CLI 호출의 전체 input+output은 **26,230 tokens**, 캐시 읽기는24,064 input tokens다. 이는 실험 토큰이 아닌 setup이며 이 문서/fixture 제작이나 실패한 첫 인증 시도의 미보고 사용량은 이 숫자에 포함하지 않는다. 실제 청구금액과 backend 모델 snapshot은 제공되지 않아 null이다.

첫 호출 시도는 UID1000·0600 인증 파일을 cap-drop=ALL 컨테이너 root가 읽지 못해401로 끝났다. 인증을 노출하거나 권한을 완화하지 않고 컨테이너 프로세스를 소유자와 같은 node(uid1000)로 변경한 뒤 성공했다. 실패 원문은 git 제외 `.runs/setup-cli-smoke`에 보존한다. 준비 서브에이전트 한 개는 사용량 제한을 보고했지만, 수정된 별도 CLI 호출은 성공했다.

누적 사용량 검증: 첫 응답 input13086/output7, 재개 응답 input26213/output17. session token_count의 마지막 요청값 input13127/output10과 차분이 일치했다. collector는 원본을 유지하고 차분 이벤트만 normalizer에 보낸다. 모든 raw counter를 합산하면 중복집계된다.

남은 본 실험 개시 작업은 C01..C20 전체 판정기/DB 대조 adapter/장애주입·성능 파일럿, 3번 하네스 게이트·독립 워커 계측 검증이다. 현재 스크립트는 해당 검증을 완료했다고 표시하지 않으며, 원본 Claude 훅을 Codex 훅으로 자동 설치하지 않는다.
