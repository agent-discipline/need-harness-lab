# 하네스 효과 비교 실험실

첫 실험은 [결제·정산 API](payment-settlement/README.md)다. 짧은 요청의 점진 구현, 상세 프롬프트, 하네스 auto 절차의 **완료까지 전체 토큰·비용·완성도**를 비교한다.

현재 단계는 실험 설계와 공통 초기 환경 준비다. 결제 기능 구현과 모델 비교 실행은 아직 시작하지 않는다. 준비 과정에서 사용한 에이전트·토큰은 본 실험에서 제외하고 setup으로 분리한다.

최종 지시는 **Kotlin + Spring 레이어드 구조**로 반영했다. 단일 갓 클래스 방식은 적용하지 않는다.

## Codex 작업 영역

```text
payment-settlement/codex/
├── 1/core1 ... core10/
├── 2/core1 ... core10/
├── 3/core1 ... core10/
├── docs/experiments/   # 조건·시행별 프롬프트 전문과 결과 분석
├── docs/results/       # 토큰·검증·비교 결과
├── runner/
└── scripts/
```

[Codex 상세 구조와 실행 안내](payment-settlement/codex/README.md)를 따른다.
