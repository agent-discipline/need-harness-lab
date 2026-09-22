# 실험 01 — 결제·정산 API의 프롬프트와 하네스 효과

동일한 Kotlin/Spring 서버와 판정 기준에서 **완료까지 필요한 총 토큰**과 **정확성·설계 품질·처리량**을 비교한다. 현재 저장소는 준비 단계이며 실험 결과를 포함하지 않는다.

| 조건 | 최초 입력 | 이후 진행 |
|---|---|---|
| 1 — minimal | 결제·정산 API를 구현해줘 | 외부 검증에서 가장 앞선 미충족 요구사항 하나씩 전달 |
| 2 — detailed | 동결된 상세 요구사항 | 검증 결과를 구조화한 수정 프롬프트로 전달 |
| 3 — harness-auto | 2와 **동일한** 상세 요구사항 + 하네스 절차 | 사전 인터뷰 → 대리 사용자 응답 → 명세 합의 → 구현·독립 검증·기록 |

**각 조건10회, 총30회**를 위해 `1/core1`~`1/core10`, `2/core1`~`2/core10`, `3/core1`~`3/core10`을 준비한다. 업무 서버 파일은 바이트가 동일하고 `experiment/`의 시행 식별자와 기록만 다르다. `coreN`은 CPU 수가 아니라 **독립 반복 실험 번호**다. 공통 원본은 `template/`이며 실험 구현은 복제본에서만 한다.

## 문서

- [실험 계획](docs/experiment-plan.md): 비교 조건, 실행 순서, 반복, 종료와 실패 기준
- [결제 계약](docs/requirements.md): 판정용 정답 계약 및 2·3번 상세 입력
- [검증 시나리오](docs/acceptance.md): k6·DB·장애 복구·설계 품질 기준
- [격리와 측정](docs/isolation-and-measurement.md): 캐시 제약 및 증거 보존
- [자원·비용](docs/resources.md): 컨테이너 한도, DB 풀, AWS 가정
- [하네스 분석](docs/harness-analysis.md): 원본 분석과 이식 차이
- [구동 절차](docs/runbook.md): 준비 환경 검증과 실험 개시 조건
- [사용량 어댑터](measurement/README.md): Codex / OpenAI / Anthropic 정규화

## 빠른 시작

저장소 루트에서 실행한다. 실제 결제 비즈니스 코드는 비어 있으며 `/health`만 구현되어 있다.

```bash
python3 payment-settlement/scripts/lab.py verify
docker compose -p payment-lab-smoke -f payment-settlement/compose.yaml up --build -d --wait
curl --fail http://localhost:18080/health
docker compose -p payment-lab-smoke -f payment-settlement/compose.yaml --profile test run --rm k6 run /tests/smoke.js
docker compose -p payment-lab-smoke -f payment-settlement/compose.yaml stop
```

추가 반복이 필요하면 동일 복제본을 만든다(10회 본 실험 밖의 예시이며 기존 폴더는 덮어쓰지 않는다).

```bash
python3 payment-settlement/scripts/lab.py clone --core 11
```

Docker 전체 환경은 공통 fixture이다. 구현 에이전트에는 해당 core 폴더만 별도 컨테이너/VM에 마운트한다. **형제 폴더가 보이는 이 저장소에서 바로 실행한 서브에이전트는 엄격한 격리 실험으로 인정하지 않는다.** 준비를 도운 서브에이전트도 실험 세션으로 재사용하지 않는다.

실제 모델 실행은 자동 시작하지 않는다. 모델 ID, 사용량 수집, 하네스 런타임 이식, 비공개 판정기와 장애 주입의 파일럿 검증 후 `ready`로 전환한다. 미완료 준비 항목은 [실험 개시 체크리스트](docs/runbook.md#실험-개시-체크리스트)에 명시한다.
