# 요구사항 명세서 — Claude 전용 본 실험

> 작성일: 2026-11-17 · 작업 폴더: `docs/plans/2026-11-17/claude-only-run/`
> 대상 프로젝트: `need-harness-lab` (로컬 `/home/jun/project/agent-discipline/need-harness-lab`, branch `setup/payment-experiment`, HEAD `94cbc30`, 원격 `agent-discipline/need-harness-lab`)

---

## 0. 요구사항 원문 (인터뷰 기록)

- 원문(2026-11-17):
  - "니드 하네스랩에 대해서, 이건 해당 실험을 별도 에이전트 Opus에게 확장시켜서 이 작업과 동시에 해당 실험도 조직에 올리고 실험을 서브에이전트에게 진행시키자."
  - "비용은 우리가 다른 실험에서 사용한 것처럼 그대로 사용해서 진행해도 될 것 같아."
  - "그 부분은 내가 처리할 거고, 다른 프로젝트들도 그렇게 했었잖아." (메인 해석: 비용 처리는 사용자 소관)
  - "코덱스는 그냥 사용하지 말자. 그냥 클로드로 진행하는 방식으로 가자."
  - "실제 하네스가 필요한지에 대한 저 실험"
  - "우선 이걸 하기 앞서서 하네스 실험부터 진행하는 게 맞지 않아? 해당 실험하고 위 작업을 하거나 위 실험을 진행하면서 해당 내용을 다듬는 게 맞을 것 같아"
- Q/A(2026-11-17):
  - 공개 → "커밋된 이력 그대로 공개" (완료)
  - Opus의 역할 → "실험 진행자를 Opus로"
  - 하네스 조건의 리뷰 → "두 번째 리뷰어를 Claude 모델로 대체" → 모델은 "Fable 5.2"
  - 모델 버전 → "현재 최신 버전" (구현자·워커 Opus 5.5 `claude-opus-5-5`, 하네스 조건 PM Fable 5.2 `claude-fable-5-2`)
  - 하네스 버전 → "현재 v4.3"
  - 웹 도구 → "세 조건 모두 허용하고 기록"
  - 보고 단위 → "파일럿과 1사이클 뒤 승인" (준비 완료·파일럿 완료·1사이클 완료 시점에 보고와 승인, 이후 2~10사이클은 연속 진행하며 사이클마다 요약, 이상 시 중단 보고)
- 메인이 제시했고 이의가 없었던 기본값(2026-11-17):
  - 상한 = run당 2,000만 토큰 · 8시간 · 피드백 30회.
  - 판정 어댑터 = 실험 진행자가 시행마다 작성, 비용은 "평가 비용"으로 분리 기록.
  - 기존 Codex 산출물 = 손대지 않고 보존. 미커밋 1사이클 결과물은 로컬에만 둔다.
  - 부하 측정 = 이 머신에서 조건 순서대로 하나씩. run 종료마다 컨테이너 볼륨 정리(디스크 여유 31GB).
  - 준비 단계 코드 리뷰 = 하네스 규칙대로 Opus 워커 ∥ codex. "Codex 미사용"은 실험 대상에 대한 결정.
- 조사로 확인한 사실(Opus 조사 워커 보고 + 메인 교차 확인: `claude/README.md`·`claude/docs/runbook.md`·`docs/experiment-plan.md`·`2026-09-23/claude-arm-runner/requirement-spec.md` 앞 60줄):
  - Claude 쪽 30 run 전부 NOT_STARTED. `payment-settlement/claude/` 전체와 `docs/`가 아직 git에 커밋되지 않았다(untracked).
  - Claude 쪽은 protocol v1(토큰 상한 200만, 외부 리뷰어 없음). 없는 스크립트: `start_cycle`·`freeze_readiness`·`consult_contract`·`capture_turn`·`load_check`.
  - 실행기 이미지는 Claude Code 2.1.280 고정. 호스트 CLI는 2.1.326.
  - 하네스 사본 `harness-source`는 commit `1754f16`(2026-09). 현재 하네스는 `/home/jun/project/agent-discipline/harness`(v4.3).
  - `codex/scripts/prepare.py:36`과 `SOURCE.json`이 하네스 옛 경로(`/home/jun/project/claude-code-harness`)를 적고 있다.
  - 판정기 어댑터(`evaluation/pilot_adapter.py`·`pilot_faults.py`)는 파일럿 구현 전용이다.
  - Claude 쪽 인증 토큰은 재발급이 필요하다(`claude/docs/auth-token.md`).

---

## 1. 목표·대상 (필수)

`payment-settlement/claude/`의 세 조건(1 minimal · 2 detailed · 3 harness-auto) × 10회 = 30회를 Claude만으로 끝까지 실행해, 조건별 완료까지의 토큰·추정 비용·완료율·품질을 결과 표로 낸다. 실험 진행자(실행 관리·판정·피드백 전달·기록)는 Opus 에이전트가 맡고, 메인 세션이 그 에이전트를 브리핑하고 결과를 회수한다.

- A. 준비 — Claude 쪽을 본 실험이 가능한 상태로 보강하고 새 프로토콜 버전(`payment-settlement-v3-claude`)으로 동결.
- B. 파일럿 1회(결과 제외).
- C. 본 실험 10사이클.
- D. 결과 정리.

## 2. 경계·불변식 (필수)

- 세 조건의 구현 모델(Opus 5.5, effort high)·도구(웹 도구 포함)·자원 한도·상한은 같다. 차이는 입력 상세도와 절차뿐이다. 하네스 조건만 PM(Fable 5.2)·서브에이전트·하네스 훅을 갖는다.
- 하네스 조건의 리뷰는 Opus 워커와 Fable 5.2 두 리뷰어다(codex 자리 대체). 이 대체는 실제 하네스와 다른 점으로 결과에 명시한다.
- run마다 새 컨테이너·새 세션·새 DB. 다른 run의 코드·실패 이력이 넘어가지 않는다.
- 실험 진행자는 구현에 개입하지 않는다 — 판정 결과와 `prompts/feedback.md`가 정한 형식의 피드백만 전달한다. 진행자·판정 어댑터·대리 사용자의 토큰은 역할별로 분리 기록한다.
- 상한: run당 input+output 2,000만 토큰 · 8시간 · 피드백 30회. 먼저 닿으면 `CENSORED_*`로 끝내고 보존한다. 실패 run을 결과에서 지우지 않는다.
- 판정 기준과 요구사항은 동결 후 바꾸지 않는다. 바꾸면 프로토콜 버전을 올리고 전 조건 재실행이다.
- 사용량 집계는 요청 단위이고 부모·서브에이전트 중복 합산이 없다. 관측하지 못한 사용량은 0으로 채우지 않고 `usage_complete=false`로 남긴다.
- 자격증명 값은 저장소·로그·기록 어디에도 남기지 않는다.

## 3. 기준소스 (필수)

- 계약·판정: `payment-settlement/docs/{requirements,acceptance,answer-policy,resources}.md`, `evaluation/`, `infra/` (현재 내용 그대로).
- 절차: `payment-settlement/claude/docs/runbook.md`, `payment-settlement/docs/experiment-plan.md`(v2 변경 절 포함), `payment-settlement/codex/scripts/*`(Claude 쪽에 없는 스크립트의 참조 구현).
- 하네스: `/home/jun/project/agent-discipline/harness`의 현재 main(v4.3) — 사본으로 가져올 때 commit SHA를 기록한다.

## 4. 금지영역 (필수)

- `payment-settlement/codex/**` 수정·삭제·커밋(미커밋 1사이클 결과물 포함). 참조와 복사 원본으로만 쓴다.
- `payment-settlement/docs/acceptance.md`·`requirements.md`·`answer-policy.md`의 내용 변경.
- 루트 공유 모듈(`evaluation/`·`infra/`·`measurement/`·`recording/`)의 기존 동작 변경. 추가가 필요하면 멈추고 보고한다.
- 사용자 계정 자격증명 파일 수정, 저장소 안 토큰 기록.
- push는 메인이 사용자 확인 후에만. 실험 진행자는 로컬 커밋까지만 한다.
- 파일럿과 본 실험의 유료 실행을 승인 시점(§0 보고 단위) 전에 시작하지 않는다.

## 5. 검증 방법 (필수)

- 준비: 실행기 단위 테스트(`python3 -m unittest discover -s payment-settlement/claude/runner`) 통과, `verify_setup.py` 통과, `preflight.py` 전 항목 true(가짜 true 금지), 동결 해시와 baseline 커밋 기록. 변경 diff는 듀얼 리뷰.
- 인증 스모크: 재발급 토큰으로 1턴, 실제 모델 ID가 Opus 5.5·Fable 5.2로 기록되는지 확인.
- 파일럿: 1회 완주. 하네스 조건에서 서브에이전트 사용량이 부모와 중복 없이 분리되는지, 두 번째 리뷰어(Fable) 사용량이 관측되는지 확인.
- 본 실험: run마다 `result.json`의 `status`·`usage_complete`, 판정기 C01~C20 결과, 부하 SLO 측정값. 사이클마다 3건 종료·문서화·커밋을 확인한 뒤 다음 사이클.
- 결과: 30행 결과 표가 run 기록과 1:1로 대조된다(누락·중복 0).

## 6. stakes (필수)

- 판정: 높음 — 근거: run당 상한 2,000만 토큰·8시간 × 30회로 비용과 시간이 크고 되돌릴 수 없으며, 실행기·집계 코드의 오류는 30회 전체의 측정을 무효로 만든다. 준비 단계는 듀얼 리뷰 루프와 테스트 설계 분리 대상이다.

---

## 7. 자율성 (모드 — stakes와 별개 축)

- [x] auto (보고·승인 지점은 §0 보고 단위)
- [ ] lazy

## 8. load-bearing 가정 (1~2개)

- 재발급한 구독 토큰으로 컨테이너 안 `claude -p`가 `claude-opus-5-5`·`claude-fable-5-2`를 실행하고 요청 단위 사용량을 돌려준다 → 토큰 재발급 직후 스모크 1턴.
- 하네스 v4.3 훅이 실행기 컨테이너에서 9월 스냅샷과 같은 방식으로 설치·강제된다(훅 self-test 통과) → 준비 단계에서 무과금 self-test로 실증.

## 9. task 분해

| task | 목표 | 의존 | acceptance |
|------|------|------|-----------|
| 01 | 현 상태 보존 커밋 — untracked인 `payment-settlement/claude/`와 `docs/`를 setup 커밋으로 기록(작업 브랜치) | — | codex 경로·미커밋 결과물 미포함 확인 |
| 02 | 실행기 보강 — 상한 2,000만, 모델 ID, 웹 도구 세 조건 동일, 두 번째 리뷰어 Fable, CLI 이미지 갱신, 빠진 스크립트 5종 | 01 | §5 준비 항목(테스트·verify) 통과 |
| 03 | 하네스 사본을 v4.3으로 갱신 + 하네스 조건 프롬프트 갱신 + 훅 self-test | 01 | 가정 2 실증 |
| 04 | 듀얼 리뷰(02·03 diff) → 수정 | 02, 03 | 리뷰 ledger 종결 |
| 05 | 토큰 재발급(사용자) → 인증 스모크 | 02 | 가정 1 실증 |
| 06 | 프로토콜 v3 동결 → **보고·승인 ①** | 04, 05 | preflight 전 항목 true, 동결 기록 |
| 07 | 파일럿 1회 → **보고·승인 ②** | 06 | §5 파일럿 통과 |
| 08 | 1사이클 → **보고·승인 ③** | 07 | 3건 종료·커밋 |
| 09 | 2~10사이클(연속, 사이클마다 요약) | 08 | 사이클마다 3건 종료·커밋 |
| 10 | 결과 정리 | 09 | 결과 표 30행 대조 |

---

## 승인 상태

- [x] 필수 6칸 전부 기입 (빈 칸 없음)
- [ ] 사용자 합의 → SPEC=1
- [ ] 자율성 선택 → MODE 기록
