# 실험 결과

현재 30개 시행 모두 `NOT_STARTED`다. 이번 준비 세션의 파일 생성·빌드·fixture 검증은 실험 결과가 아니다.

- `schedule.json`: 사전 고정 실행 순서
- `run-manifest.example.json`: 실제 모델·캐시·격리·이미지 증거 입력 형식
- `../experiments/{1,2,3}/core{1..10}/run.json`: 시행별 현재 상태
- `../experiments/{1,2,3}/core{1..10}/transcript.md`: 모든 프롬프트와 결과 분석 누적 문서
- `../experiments/{1,2,3}/core{1..10}/events.jsonl`: 전문과 hash를 보존하는 원장

각 시행 종료 시 `summary.md`, `usage-summary.json` 및 검증 원문을 추가하고 해당 core 코드와 docs/experiments 기록을 함께 커밋한다. 최종 비교표는 완료 또는 중단한 시행 모두를 포함해야 한다. 원격 push는 초기 세팅이나 시행 마감에 포함하지 않는다.

준비 상태 검사:

```bash
python3 payment-settlement/codex/scripts/preflight.py payment-settlement/codex/docs/experiments/1/core1/run.json
```

현재는 exit2로 미확인 개시 조건을 나열하는 것이 정상이다. gate를 통과시키기 위해 미확인 값을 true로 채우지 않는다.
