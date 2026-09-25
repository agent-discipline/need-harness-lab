# 구동과 시행 절차

## 초기 환경 검증

명령은 저장소 루트 기준이다. `python3 payment-settlement/codex/scripts/prepare.py`는 30개 복제본과 빈 시행 기록을 준비하고, 기존 실험 코드는 덮어쓰지 않는다.

```bash
python3 payment-settlement/codex/scripts/lab.py verify --core 1
docker compose -p payment-lab-smoke -f payment-settlement/codex/compose.yaml config --quiet
docker compose -p payment-lab-smoke -f payment-settlement/codex/compose.yaml up --build -d --wait
curl --fail http://localhost:18080/health
docker compose -p payment-lab-smoke -f payment-settlement/codex/compose.yaml exec -T db psql -U lab -d payment -c 'SELECT count(*), sum(balance_minor) FROM users;'
docker compose -p payment-lab-smoke -f payment-settlement/codex/compose.yaml --profile test run --rm k6 run /tests/smoke.js
```

독립 시행은 `ARM=2 CORE=core4 ... -p payment-a2-c4`처럼 core와 project를 함께 지정한다. 환경변수만 변경하고 기존 Compose project를 재사용하면 DB/journal이 공유되므로 독립 시행이 아니다. 같은 포트를 사용하므로 이전 시행을 `stop`한 뒤 다음 시행을 시작한다. volume 삭제 대신 새로운 project명을 사용하며 원본 증거는 보존한다.

## 시행별 문서화

각 `codex/docs/experiments/1/core1` 같은 경로에 run manifest, events.jsonl, transcript.md를 둔다. events는 원본 전문과 SHA256을 보존하고 transcript는 읽기용이다. transcript를 직접 편집하지 않는다. agent 생성 분석은 관측 로그를 인용하고 실제 미실행 결과를 추정하지 않는다.

```bash
python3 payment-settlement/codex/scripts/lab.py prompt --arm 1 > /tmp/payment-prompt.txt
# 이 파일을 해당 격리 에이전트에 실제 전달한 뒤 응답/검증과 분석을 저장한다.
python3 payment-settlement/recording/record.py \
  --run-dir payment-settlement/codex/docs/experiments/1/core1 \
  --prompt-file /tmp/payment-prompt.txt --response-file /tmp/payment-response.txt \
  --analysis-file /tmp/payment-analysis.md --role implementer \
  --session-id ACTUAL_SESSION_ID --status ITERATING \
  --evidence-files /tmp/payment-test.txt
```

숫자는 자동 누적된다. `1. 프롬프트 전문 / 1. 결과물 분석 / 2. 프롬프트 전문 …` 형식이다. 분석에는 요청 충족 여부, 실제 변경 파일/commit diff, 검증 명령·exit code, 통과/실패, 이번/누적 토큰, 다음 프롬프트 근거를 적는다. tool 이벤트/시스템 프롬프트/워커 brief도 원본 별도 파일로 보존한다. 부모와 자식의 입력을 상위 요약으로 대체하지 않는다.

## 종료와 커밋

schedule.json의 cycle 하나씩 진행한다. 한 cycle의 세 구현을 병렬 실행하고 최종 부하 측정과 개별 커밋은1→2→3 순서로 수행한다. 세 시행의 마감/커밋 전에는 다음 cycle을 시작하지 않는다. 완성/실패/상한 도달 판정 뒤 해당 run의 `codex/docs/experiments/<arm>/core<N>/summary.md`와 `run.json`을 마감한다. summary에는 종료 사유·계약별 판정·품질·사용량·캐시/격리 상태·증거 경로를 포함한다. usage 원본에서 누락이 있는 run은 비교 가능한 성공으로 인정하지 않는다.

`scripts/commit_run.py --arm N --core M`은 문서/usage 존재와 상태, 이전 시행 커밋 여부를 확인한 뒤 해당 core만 커밋한다. 실제 실행 증거 없는 NOT_STARTED는 커밋 완료 대상이 아니다. 부모 저장소에서 에이전트가 직접 git 명령으로 다음 시행에 영향을 주지 않게 평가자만 커밋한다. 원격 push는 수행하지 않는다. 종료된 시행의 HEAD는 다음 index 갱신에서 연결하며 커밋 자신의 SHA를 자기 파일에 넣으려 하지 않는다.

## 실험 개시 체크리스트

- [ ] `gpt-sol-high`의 실제 model ID와 모든 역할의 high 설정을 실 API/CLI metadata로 확인
- [ ] 합의한 observed-cache 방식에서 캐시 read 사용량 및 write 미관측 여부를 명시. strict-zero-cache 후속 실험은 별도 사전 검증 필요
- [ ] 실제 에이전트 런타임의 독립 컨테이너/VM·읽기 범위·세션·인증·memory 격리 확인
- [ ] Codex CLI0.155.1 원본 usage 이벤트와 자식/compaction 포함 범위 수집 검증
- [ ] Codex용 auto 게이트/worker lifecycle 이식 및 부정 테스트(명세 승인 전 코드쓰기 차단). 원본 Claude 훅 복사만으로 완료 아님
- [ ] C01..C20 전체 외부 판정·DB adapter·장애 주입·negative control 완성, 파일럿 실행. 현재 k6 시작 스크립트는 전체 판정기가 아님
- [ ] 이미지 digest·모델·계약·평가기·부하·seed 동결 및 baseline commit/tag 기록
- [ ] 30개 시행의 schedule, 문서/커밋 처리 검증

준비 상태에서 이 항목을 실행한 것으로 표시하지 않는다. 현재 설치된 내장 서브에이전트는 공유 파일시스템을 사용하고 캐시 제어 인터페이스가 없으므로 그대로 30회 호출한 결과는 사용자가 요구한 엄격한 격리 비교가 되지 않는다.

## 별도 Codex를 대화형처럼 제어

[runner 도구](../runner/README.md)는 run마다 별도 컨테이너·작업 복사본·Codex 상태 volume을 사용한다. 최초 exec의 thread ID를 저장하고 후속 입력은 그 ID로 resume한다. 3번은 PM의 질문 응답을 실행 관리자가 answer-policy에 따라 전달한다. goal은 사용하지 않는다. 실제 호출 원문·stderr·usage를 호스트에서 보존하므로 구현자가 평가 로그를 읽거나 고칠 수 없다. 원본 하네스의 Claude 훅 강제와 Codex 절차 이식은 구별한다.

## 구현 중 문제와 해결 기록

각 시행의 `docs/experiments/<arm>/core<N>/issues.md`에 발생 상황, 증거, 원인, 해결 방법, 재검증 결과를 기록한다. [공통 양식](issue-template.md)을 사용한다. 프롬프트 결과를 분석할 때 함께 갱신하고 해당 실험 커밋에 포함한다. 실제 시행 후 문제가 관측되지 않았으면 검증 범위와 함께 “관측된 문제 없음”을 명시한다. 미실행과 문제 없음은 구분한다.

초기 환경·계측 문제는 `docs/results/setup/issues.md`, 제외된 파일럿 문제는 `docs/pilots/<pilot>/issues.md`에 분리한다. 원인을 확인하지 못했다면 미확인으로 남기며 해결 후 검증 증거가 없으면 RESOLVED로 표시하지 않는다. 기록은 평가자 소유이며 다른 시행 에이전트에게 공개하지 않는다.

최종 완성도는 세 방법 모두 Claude Fable의 동일 기준 독립 검증을 한 차례 받는다. 방법 번호를 가린 코드와 공통 요구사항·실제 검증 증거를 제공하고, 요청 모델/응답 모델·원문·판정·사용량을 보존한다. Fable 검증 사용량은 Sol 구현 사용량과 구분하고 전체 비용에도 포함한다. Fable 판정과 실행 테스트 판정을 함께 보고하며 미해결 지적을 통과로 처리하지 않는다. 3번의 하네스 내부 리뷰와는 별도이다.

## 모델 관측 범위

Codex CLI 응답은 실제 backend 모델 ID를 제공하지 않아 `actual_model=null`로 보존한다. 실행 전에는 세션 turn_context의 `configured_model=gpt-6-sol` 및 `high`를 검증하고, backend 식별자 미관측을 명시한다. 이를 backend 신원 검증 완료로 표현하지 않는다. 하위 작업자의 설정·사용량은 별도 session audit으로 확인한다.

실제 턴 완료 뒤 `scripts/capture_turn.py --run-id <id> --turn <번호> --analysis <분석파일>`로 프롬프트·전체 최종 메시지·분석·사용량 delta를 시행별 docs에 보존한다. 분석 파일은 평가자가 관측 증거에 근거해 작성한다. 원본 tool/session 로그는 `.runs`에 유지한다.
