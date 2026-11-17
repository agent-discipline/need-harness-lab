동봉된 하네스 core와 필요한 playbook의 auto 절차를 적용하세요. 이 환경에는 원본 Claude 훅이 그대로 설치되어 있습니다.
요구사항에서 아직 확정되지 않은 사항은 구현 전에 질문하세요. 대리 사용자가 동결 계약에 따라 답변합니다.
여섯 필수 칸의 requirement-spec.md를 작성하고 대리 사용자의 합의를 받은 후 구현하세요.
PM은 인터뷰·명세·분해·게이트·기록을 소유하고, 독립 컨텍스트 워커로 구현·blind 테스트·듀얼 리뷰를 수행하세요.
모델 규칙: PM(이 세션)은 Fable, 모든 워커(구현·테스트 설계·리뷰)는 Opus, effort는 모두 high입니다. 리뷰는 원본 playbook대로 Opus 워커와 codex를 병렬로 사용하세요.
게이트 상태 기록은 set-state.sh만 사용하고, 훅이 차단하면 우회하지 말고 절차를 따르세요.
spec/log 및 모든 워커 사용량·검증 증거를 포함해 완료하세요.
환경 사실: 이 컨테이너에서 codex는 PATH의 `codex`로 호출합니다(nvm 경로 없음). review.md의 `--sandbox read-only` 호출이 이 컨테이너에서 실패하면 `--dangerously-bypass-approvals-and-sandbox`를 대안으로 쓰고 `user override`로 기록하세요(실행 관리자 사전 승인). 원격 push·PR·이슈 생성은 금지 영역입니다.
