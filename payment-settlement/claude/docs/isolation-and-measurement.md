# Claude 판 격리·캐시·사용량 측정 (Codex 판과의 차이만)

공통 원칙은 [Codex 판 문서](../../codex/docs/isolation-and-measurement.md)를 따른다. 여기에는 Claude Code 실행에서 달라지는 점만 적는다.

## 격리 단위

- run마다 새 컨테이너(`payment-claude-runner:2.1.326`, USER node uid 1000), 새 workspace 복사본(`git init` + 초기 커밋 1개), 새 `CLAUDE_CONFIG_DIR` volume(`/home/node/.claude`), 새 session id(uuid4 고정). 3번은 두 번째 리뷰어 전용 volume(`/home/node/.fable-review`)을 추가한다. 추가 자격증명 마운트는 없다.
- `HOME`·`CLAUDE_CONFIG_DIR`은 이미지 ENV와 `docker create --env` 양쪽에 명시한다(훅 배선 `~/.claude/hooks/*`, transcript 경로가 암시값에 기대지 않게).
- 1·2번 volume은 비어 있다: 훅·CLAUDE.md·memory·플러그인 없음. 도구는 **allow-list**(`--tools Bash,Read,Edit,Write,NotebookEdit,Glob,Grep,ToolSearch,WebSearch,WebFetch`)로 제한해 에이전트·스킬·스케줄러·메시징·worktree 표면을 닫고, self-test가 init 이벤트의 tools가 이 집합의 부분집합임을 단정한다(웹 도구 `WebSearch`·`WebFetch`는 세 조건 모두 허용 — 2026-11-17 결정. self-test가 모든 arm의 init tools에 두 도구가 있는지 단정하고, 사용은 턴마다 `web_requests`·manifest `observed_tools`로 기록). 3번도 같은 목록 + `Task`(서브에이전트)로 제한하고, self-test가 3번 = 1·2번 목록 + 서브에이전트 표면임을 단정한다(2026-11-17 실측: 1번 10개, 3번 같은 10개 + `Task`). 1·2번 transcript에 sidechain 요청이 나타나면 `incomplete` 경보. `--bare`는 OAuth와 양립하지 않아 쓰지 않는다.
- 3번 volume에는 `harness-source`(v4.3, commit 7b0ef7b)의 배포 manifest(`src/core.md→core.md`·`HISTORY.md`·`hooks/`·`playbooks/`·`templates/`·`agents/`) + 글로벌 부트스트랩 `CLAUDE.md` + 실배포 hooks 섹션 추출 `settings.json`을 시딩한다. init과 매 턴 직전에 무과금 self-test가 훅 존재·`bash -n`·시딩 해시·추가 파일 부재·gate-guard 차단(exit 2)/docs 통과(exit 0)·`fable-review` PATH를 단정하고, 실패하면 호출하지 않는다.
- 호스트 HOME·다른 run·정답·평가 로그·Docker socket은 마운트하지 않는다. 네트워크는 파일 격리와 별개다(runbook 참조).

## 인증과 시크릿

- 구독 OAuth 장기 토큰만 `--env-file`(repo 밖, 0600, 단일 키)로 주입한다. `invocation.json`에는 파일 경로만 남는다. `docker inspect` 사본은 `Config.Env` 값을 마스킹해 저장한다.
- 매 턴 종료 시(`finally`) run 디렉터리 전체(workspace·transcript 사본 포함)를 토큰 값으로 스캔한다. 히트는 치환 후 `SECRET-LEAK.txt`를 남기고 턴을 실패로 끝낸다(디스크 블록 회수는 보장하지 않으므로 토큰 회전이 정답).
- config volume은 턴마다 파일 목록(내용 아님)을 기록하고 `.credentials.json` 존재 여부를 `credentials_file_in_volume`으로 남긴다. 실험 종료 시 volume을 폐기한다.

## 사용량 원천과 집계

- 원천은 두 가지를 모두 보존한다: ① `stream-json` stdout의 최종 `result`(subtype·session_id·`usage`·`modelUsage`·`total_cost_usd`·num_turns) ② `CLAUDE_CONFIG_DIR/projects/**/*.jsonl` transcript 사본(assistant 줄의 `requestId`·`message.model`·`message.usage`·`isSidechain`).
- 집계 단위는 **요청(requestId)**이다. 같은 requestId의 모든 사본을 모아 usage 동일성·모델 단일성을 검사하고, 어긋나면 `incomplete`에 사유를 남긴다. 어느 사본이든 sidechain이면 `worker`, 아니면 3번은 `pm`, 1·2번은 `implementer`다. 3번 두 번째 리뷰어의 transcript는 별도 config dir에서 오므로 전부 `reviewer`다. 턴 간 중복은 `request-ids.txt` append-only 원장으로 막는다.
- Anthropic usage의 `input_tokens`는 캐시 제외 값이므로 공유 adapter(`../../measurement/normalize.py --provider anthropic`)가 `cache_read`·`cache_creation`을 더해 전체 입력으로 바꾼다. adapter는 파일당 하나의 역할만 받으므로 collect가 `events.role-{pm,implementer,worker,reviewer}.jsonl`로 분리 출력하고 `launch.py normalize`가 역할별로 호출해 `usage-normalized.jsonl`·`usage-summary.json`을 만든다.
- `usage_complete`는 result 이벤트 존재·exit 0·transcript 복사 성공·이벤트 1건 이상·`incomplete` 없음·파싱 불가 줄 0·requestId 없는 assistant 줄 0·`result.usage`와 요청 합의 차이가 main 또는 전체 기준으로 0·(arm 3) 그 턴의 리뷰어 호출이 전부 종료되고 transcript를 남겼을 때만 true다. 스모크 4턴에서 delta 0이 실증되어 관측치였던 교차검증을 완전성 조건으로 올렸다(서브에이전트 포함 여부가 확정되기 전까지는 main/전체 중 하나가 0이면 통과).
- `usage_cross_check`는 `result.usage`와 main 역할 합·전체 합의 차이를 기록한다. `result.usage`가 sidechain을 포함하는지는 arm 3 첫 파일럿에서 확정한다.
- 3번의 두 번째 리뷰어(`fable-review`, `claude-fable-5-2`)는 호출마다 새 `claude -p` 세션이며 자체 `CLAUDE_CONFIG_DIR`(`/home/node/.fable-review`)를 쓴다. 래퍼가 CLI 시작 전에 호출과 고정 session id를 `reviewer-calls.log`에 남기고, 실행기가 매 턴 로그와 `projects/`를 `reviewer-calls.log`·`reviewer-projects/`로 복사해 요청 단위 `reviewer` 이벤트로 만든다. 이 사용량은 `usage_cross_check`의 main/전체 합에 섞지 않고 `reviewer_sum`으로 따로 둔다(PM의 `result.usage`에 포함되지 않으므로). 그 턴의 호출이 끝나지 않았거나 transcript가 없거나, 로그 없이 transcript만 있거나, 로그를 읽지 못하면 `reviewer_usage_pending=true`·`usage_complete=false`. 이미 집계한 호출은 `reviewer-calls.json`, 요청은 `request-ids.txt`로 재집계를 막는다.
- 구독이므로 `billed_cost=null`, `estimated_cost=total_cost_usd`(CLI 추정). 캐시는 observed-cache로 계측만 한다. 턴마다 `cost`에 고정 세션 추정치와 (3번) 리뷰어 호출별 추정치(`raw/<session>.json`의 `total_cost_usd`)를 남기고, 회수하지 못한 호출은 `cost_unobserved`·`cost_complete=false`로 남긴다(0으로 채우지 않는다). 재개한 세션의 `total_cost_usd`가 호출 단위인지 누적인지는 파일럿 전에 확인되지 않았다.
- **run 단일 장부**: `.runs/<run>/ledger.json`이 모든 역할(pm·implementer·worker·reviewer·user-proxy)의 요청 단위 사용량·총 토큰·비용을 한곳에 모은다. 대리 사용자 호출(`consult_contract.py`)은 `.runs/<run>/consults/NNNN/`에 남아 같은 장부로 합산된다. **run 상한(토큰 2,000만)은 이 장부의 전 역할 합으로 판정한다** — 리뷰어와 대리 사용자도 포함(runbook "상한").
- **웹 사용과 effort 관측**: 요청마다 transcript의 `tool_use` 블록(WebSearch·WebFetch, tool_use id로 중복 제거)과 assistant 줄의 `perTurnEffort`를 기록해 턴과 장부의 `observed_tools_and_effort`에 역할별로 둔다. 한계: ① 사용량이 미완전한 요청은 세지 못하므로 그때 `web_tool_uses_complete=false`, ② WebSearch 도구가 내부에서 따로 보내는 검색 요청의 토큰이 transcript에 남는지는 확인되지 않았다, ③ `perTurnEffort`가 없는 요청(특히 서브에이전트)의 effort는 `unobserved`로 남으며 이 경우 effort high 전파는 미검증이다.
