# 실험01 — 결제·정산 API의 프롬프트와 하네스 효과

동일한 Kotlin/Spring 업무 계약과 외부 판정 기준으로 짧은 요청, 상세 프롬프트, 하네스 auto의 완료까지 토큰·정확성·설계 품질을 비교한다.

공통 문서와 판정 자원은 이 폴더에서 관리하고, 실행 환경의 코드·대화·결과는 플랫폼별 영역에서 관리한다. 현재 이 README의 구조 설명은 **Codex 영역만** 다룬다.

- [실험 계획](docs/experiment-plan.md): 조건별10회, 총30회 독립 시행과 종료·커밋 절차
- [업무 계약](docs/requirements.md), [외부 판정](docs/acceptance.md), [자원 예산](docs/resources.md)
- [하네스 분석](docs/harness-analysis.md), [대리 사용자 답변 정책](docs/answer-policy.md)
- 공통 평가 코드는 `evaluation/`, 동일 DB·은행 fixture는 `infra/`, 계측/기록 유틸리티는 `measurement/`와 `recording/`
- [Codex 폴더 구조·실행 안내](codex/README.md), [실행 기록](codex/docs/experiments/), [초기 검증](codex/docs/results/setup/README.md)

```text
codex/
├── 1/core1 ... core10/       # 짧은 요청 방식의 독립 Kotlin 서버
├── 2/core1 ... core10/       # 상세 프롬프트 방식의 독립 Kotlin 서버
├── 3/core1 ... core10/       # 하네스 auto 방식의 독립 Kotlin 서버
├── docs/
│   ├── experiments/         # 1~3/core1~10의 전체 프롬프트·결과 분석
│   ├── results/             # 실행 순서·토큰 집계·검증 결과
│   └── runbook.md
├── template/
├── prompts/
├── runner/
├── scripts/
├── harness-source/
└── compose.yaml
```

세 방식 모두 별도 컨테이너의 Codex를 사용한다. 3번도 goal 없이 외부 관리자가 같은 세션의 exec/resume로 인터뷰·명세 합의·구현·검증을 제어한다. 서버 측 캐시는 관측하여 별도 집계하며 완전 비활성화를 주장하지 않는다. 최종 지시대로 레이어드 Kotlin 구조를 적용한다.
