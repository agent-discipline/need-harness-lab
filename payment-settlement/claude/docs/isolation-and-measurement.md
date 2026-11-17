# Claude 판 격리·캐시·사용량 측정 (Codex 판과의 차이만)

공통 원칙은 [Codex 판 문서](../../codex/docs/isolation-and-measurement.md)를 따른다. 여기에는 Claude Code 실행에서 달라지는 점만 적는다.

## 격리 단위

- run마다 새 컨테이너(`payment-claude-runner:2.1.280`, USER node uid 1000), 새 workspace 복사본(`git init` + 초기 커밋 1개), 새 `CLAUDE_CONFIG_DIR` volume(`/home/node/.claude`), 새 session id(uuid4 고정). 3번은 codex 상태 volume과 읽기 전용 `auth.json`을 추가한다.
- `HOME`·`CLAUDE_CONFIG_DIR`은 이미지 ENV와 `docker create --env` 양쪽에 명시한다(훅 배선 `~/.claude/hooks/*`, transcript 경로가 암시값에 기대지 않게).
- 1·2번 volume은 비어 있다: 훅·CLAUDE.md·memory·플러그인 없음. 도구는 **allow-list**(`--tools Bash,Read,Edit,Write,NotebookEdit,Glob,Grep,ToolSearch,WebSearch,WebFetch`)로 제한해 에이전트·스킬·스케줄러·메시징·worktree 표면을 닫고, self-test가 init 이벤트의 tools가 이 집합의 부분집합임을 단정한다(웹 도구는 D5 확정 전까지 포함, 사용은 턴마다 `web_requests`로 기록). 1·2번 transcript에 sidechain 요청이 나타나면 `incomplete` 경보. `--bare`는 OAuth와 양립하지 않아 쓰지 않는다.
- 3번 volume에는 `harness-source`(commit 1754f16)의 `src/core.md→core.md`·`hooks/`·`playbooks/`·`templates/` + 글로벌 부트스트랩 `CLAUDE.md` + 실배포 hooks 섹션 추출 `settings.json`을 시딩한다. init과 매 턴 직전에 무과금 self-test가 훅 존재·`bash -n`·gate-guard 차단(exit 2)/docs 통과(exit 0)·`codex` PATH를 단정하고, 실패하면 호출하지 않는다.
- 호스트 HOME·다른 run·정답·평가 로그·Docker socket은 마운트하지 않는다. 네트워크는 파일 격리와 별개다(runbook 참조).

## 인증과 시크릿

- 구독 OAuth 장기 토큰만 `--env-file`(repo 밖, 0600, 단일 키)로 주입한다. `invocation.json`에는 파일 경로만 남는다. `docker inspect` 사본은 `Config.Env` 값을 마스킹해 저장한다.
- 매 턴 종료 시(`finally`) run 디렉터리 전체(workspace·transcript 사본 포함)를 토큰 값으로 스캔한다. 히트는 치환 후 `SECRET-LEAK.txt`를 남기고 턴을 실패로 끝낸다(디스크 블록 회수는 보장하지 않으므로 토큰 회전이 정답).
- config volume은 턴마다 파일 목록(내용 아님)을 기록하고 `.credentials.json` 존재 여부를 `credentials_file_in_volume`으로 남긴다. 실험 종료 시 volume을 폐기한다.

## 사용량 원천과 집계

- 원천은 두 가지를 모두 보존한다: ① `stream-json` stdout의 최종 `result`(subtype·session_id·`usage`·`modelUsage`·`total_cost_usd`·num_turns) ② `CLAUDE_CONFIG_DIR/projects/**/*.jsonl` transcript 사본(assistant 줄의 `requestId`·`message.model`·`message.usage`·`isSidechain`).
- 집계 단위는 **요청(requestId)**이다. 같은 requestId의 모든 사본을 모아 usage 동일성·모델 단일성을 검사하고, 어긋나면 `incomplete`에 사유를 남긴다. 어느 사본이든 sidechain이면 `worker`, 아니면 3번은 `pm`, 1·2번은 `implementer`다. 턴 간 중복은 `request-ids.txt` append-only 원장으로 막는다.
- Anthropic usage의 `input_tokens`는 캐시 제외 값이므로 공유 adapter(`../../measurement/normalize.py --provider anthropic`)가 `cache_read`·`cache_creation`을 더해 전체 입력으로 바꾼다. adapter는 파일당 하나의 역할만 받으므로 collect가 `events.role-{pm,implementer,worker}.jsonl`로 분리 출력하고 `launch.py normalize`가 역할별로 호출해 `usage-normalized.jsonl`·`usage-summary.json`을 만든다.
- `usage_complete`는 result 이벤트 존재·exit 0·transcript 복사 성공·이벤트 1건 이상·`incomplete` 없음·파싱 불가 줄 0·requestId 없는 assistant 줄 0·`result.usage`와 요청 합의 차이가 main 또는 전체 기준으로 0·(arm 3) codex 리뷰 세션이 없거나 정규화 완료일 때만 true다. 스모크 4턴에서 delta 0이 실증되어 관측치였던 교차검증을 완전성 조건으로 올렸다(서브에이전트 포함 여부가 확정되기 전까지는 main/전체 중 하나가 0이면 통과).
- `usage_cross_check`는 `result.usage`와 main 역할 합·전체 합의 차이를 기록한다. `result.usage`가 sidechain을 포함하는지는 arm 3 첫 파일럿에서 확정한다.
- 3번의 codex 리뷰 호출은 래퍼가 `--ephemeral`을 제거해 `~/.codex/sessions`에 남으며 매 턴 `codex-sessions/`로 복사한다. 이 사용량은 codex adapter로 별도 정규화한다(분석 단계). 래퍼가 모든 호출을 `wrapper-calls.log`에 남기므로, 그 턴에 codex 호출 시도가 있었거나 세션 파일이 있으면 `reviewer_usage_pending=true`·`usage_complete=false`(정규화 전). 호출 시도도 세션도 없는 턴은 리뷰어 관련 조건을 통과한다.
- 구독이므로 `billed_cost=null`, `estimated_cost=total_cost_usd`(CLI 추정). 캐시는 observed-cache로 계측만 한다.
