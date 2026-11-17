# 작업 규칙

> 상시 규칙은 이 파일과 core.md(v4) 둘뿐이다. **core.md 전문은 이 파일 끝의 import 로 매 세션 컨텍스트에 자동 인라인된다(상시 선독).** 다른 규칙 문서는 core §8 트리거에 해당할 때만 읽는다.

1. **실행물(코드·스크립트·설정·스키마·DB/데이터 + 실행 정책 파일)을 변경하는 작업(L1)은 core §1의 전수 인터뷰 → 요구사항 명세서(필수 6칸 — 빈 칸 금지) 사용자 합의 + 자율성(auto/lazy) 선택을 통과한 뒤 시작한다.** (유일 예외: 긴급 수정 — 새 작업 폴더에 log.md 를 Write 로 먼저 생성 + 긴급 확인 + 빚(DEBT).)
2. 대화·리서치·분석·설계와 그 기록(docs/**)은 L0 — 게이트 없이 자유 진행한다. 단 그 결론이 구현의 입력이 되는 순간 실코드로 재확인한다(core §2).
3. 검증 강도는 stakes에 비례한다(core §3~4). stakes가 낮아도 머지 전 최소 안전선(core §4)은 건너뛰지 않는다.
4. 산출물은 대상 프로젝트의 `docs/plans/YYYY-MM-DD/작업명/`에 저장한다 — `requirement-spec.md` + `log.md` 2파일(core §7).

## 경로 규칙
- 이 파일·core.md·`playbooks/`·`templates/`는 같은 디렉토리에 있다(소스 repo: claude-code-harness, 배포: hooks/deploy.sh). 실제 작업 대상은 Claude가 호출된 프로젝트 디렉토리다.

## 상시 주입 (core.md)
<!-- Claude Code 는 CLAUDE.md 의 상대경로 import 를 매 세션 인라인한다. 아래 한 줄이 core.md 전문을
     컨텍스트에 강제 주입한다 — 이 파일과 같은 디렉토리(~/.claude)의 core.md 로 해석된다. -->
@core.md
