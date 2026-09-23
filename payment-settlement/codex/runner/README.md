# 별도 Codex CLI 격리 실행기

이 도구는 실험 준비용 실행 경로다. `init`은 초기 서버를 복사하고, **`turn`은 실제 유료 모델 호출을 시작한다.** 이미지 빌드와 실제 Codex2턴 호출·같은 세션 resume를 검증했다. 이 호출은 setup이며 30회 본 실험에는 포함하지 않는다. 전체 30회 자동 오케스트레이터나 완성된 판정기라는 의미는 아니다.

## 준비

```bash
docker build -t payment-codex-runner:0.155.1 payment-settlement/runner
python3 payment-settlement/codex/runner/launch.py init \
  --run-id sol-arm1-core01 --arm 1 --core 1 --model ACTUAL_MODEL_ID
# 3번에만 공개 하네스 snapshot 마운트 가능
python3 payment-settlement/codex/runner/launch.py init \
  --run-id sol-arm3-core01 --arm 3 --core 1 --model ACTUAL_MODEL_ID \
  --harness payment-settlement/codex/harness-source
```

각 조건 1..3, core 1..10의 30개 run을 개별 생성한다. `ACTUAL_MODEL_ID`는 실행 전 확인한 값으로 바꾼다. 모델 이름을 추측해 기본값으로 넣지 않는다. 기존 run과 턴은 덮어쓰지 않는다. 원본 `arm/core`는 수정하지 않고 `.runs/<run>/workspace`로 export한다. `.git`, build, Gradle 캐시, 에이전트 상태·로그는 제외하며 starter symlink는 거부한다.

이미지는 Node22·Temurin21과 정확한 npm 버전 `@openai/codex@0.155.1`을 사용한다. base tag는 mutable하므로 본 실행 전 image digest를 고정하여 `init --image`에 지정한다.

## 최초 요청과 같은 세션 재개

```bash
# 실제 호출 없이 명령 구성 확인. 인증 내용은 읽거나 출력하지 않는다.
python3 payment-settlement/codex/runner/launch.py turn \
  --run-id sol-arm1-core01 --prompt payment-settlement/codex/prompts/1-minimal.md \
  --auth /ABSOLUTE/PATH/auth.json --dry-run

# 명시적 실제 호출: 먼저 계약·평가기·모델·이미지·캐시 정책 동결 필요
python3 payment-settlement/codex/runner/launch.py turn \
  --run-id sol-arm1-core01 --prompt /PATH/first-prompt.txt \
  --auth /ABSOLUTE/PATH/auth.json --cache-policy observed-cache

# 호스트 관리자가 관측 결과/질문 답변을 작성한 뒤 같은 thread ID로 재개
python3 payment-settlement/codex/runner/launch.py turn \
  --run-id sol-arm1-core01 --resume --prompt /PATH/next-prompt.txt \
  --auth /ABSOLUTE/PATH/auth.json --cache-policy observed-cache
```

prompt 파일은 stdin으로 전달한다. 최초는 `codex exec -`, 후속은 원시 이벤트에서 회수한 ID를 이용한 `codex exec resume ID -`다. `--last`와 `--ephemeral`은 사용하지 않는다. 모든 조건에서 goals와 memories는 비활성화한다. 3번만 native multi_agent를 활성화하며 자식 모델과 effort는 별도 세션 metadata로 확인해야 한다. 같은 세션을 유지하며 호스트 관리자가 매 단계 응답을 읽고 다음 입력을 주는 방식이다. 자동 goal 반복은 사용하지 않는다.

## 격리와 로그

- 각 run의 workspace와 무작위 이름의 독립 `/home/node/.codex` Docker volume만 쓰기 가능하다. 인증은 지정한 `auth.json` 파일 하나만 읽기 전용 bind한다. 하네스는 3번에서 이 프로젝트의 `harness-source`만 `/harness` 읽기 전용으로 마운트할 수 있다.
- 호스트 HOME, 다른 run, 프로젝트 전체, Docker socket, 평가기/정답, 호출 로그를 마운트하지 않는다. Docker create/start를 사용하고 종료 후 해당 호출 컨테이너만 제거한다. run 상태 volume과 workspace는 보존한다.
- 외부 컨테이너 격리를 전제로 CLI의 `--dangerously-bypass-approvals-and-sandbox`를 사용한다. CLI 내부 샌드박스 대신 Docker 경계로 호스트 파일 쓰기를 제한한다. `--ignore-user-config --ignore-rules`, 명시 모델, high effort를 매 호출 지정한다. 인터넷 egress는 모델 API/의존성 설치 때문에 열려 있으며 네트워크 화이트리스트나 악성 코드 보안 인증까지 제공하지 않는다.
- `.runs/<run>/turns/NNNN/`에 prompt 원문, invocation, 수정하지 않은 stdout JSONL/stderr, container inspect, 이벤트 가공본과 result를 호스트에서 보존한다. 가공본 `turn.completed`마다 run/turn/line 기반 event_id를 추가하고 세션 누적 counter를 차분으로 변환한다. reported_cumulative_usage를 함께 보존한다. prompt 해시는 원문과 함께 저장한다. 인증 파일 내용은 수집하지 않는다.
- 외부 최종 출력의 stdout/stderr 원문만으로 내부 프롬프트·시스템 프롬프트·모든 자식 호출까지 관측했다고 주장할 수 없다. native 워커/compaction/retry 포함 여부와 usage 중복 여부는 세션별 원자료 추출·측정 adapter 검증이 필요하다. 미확인 상태는 `usage_complete=false`로 처리한다.
- `manifest.requested_model`과 실제 공급 모델은 구분한다. `actual_model`은 기본 null이며 backend 응답에 모델 ID가 없는 경우 null을 유지한다. 복사한 세션의 turn_context에서 configured_model과 effort를 관측해 별도로 기록한다.
- 서버 캐시를 끄는 기능은 제공하지 않는다. **observed-cache만 지원**하며 실제 호출에는 해당 플래그를 요구한다. strict-zero-cache 결과로 보고할 수 없다.
- 하네스 파일 마운트는 정책 열람 지원이다. Claude hooks가 Codex에서 강제 실행된다는 뜻은 아니다. PM 인터뷰/승인/리뷰 절차와 원본 차이는 별도 관리자가 확인한다.

## 검증

```bash
python3 -m unittest discover -s payment-settlement/runner -v
```

테스트는 서로 다른 run 상태 분리, 덮어쓰기 거부, 정확한 resume, auth 읽기 전용 마운트, Docker socket 부재, dry-run 무실행, 원시 JSONL 보존과 event_id를 확인한다. 실제 Docker 이미지 빌드·인증·모델 availability·session resume end-to-end는 별도 실행 증거가 필요하다.

실제 검증: `results/setup/codex/summary.json` 및 원본 응답 참조. 인증 file UID1000/0600과 Docker cap-drop=ALL 조합에서 root 프로세스는 읽지 못했으므로 이미지 USER를 node(uid1000)로 맞췄다. 호스트 auth 소유자가 다른 UID이면 이미지 사용자 매핑을 맞춘 후 시작해야 한다. 인증 파일을 world-readable로 바꾸지 않는다.

통합 테스트를 수행하는 run은 init에 `--network <해당 Compose project>_default`를 준다. DB_URL/BANK_BASE_URL이 컨테이너 서비스 주소로 제공된다. 다른 시행 네트워크를 연결하지 않는다. turn의 `--timeout-seconds` 기본1800초는 단일 호출 상한이며 run전체8시간/2백만토큰 상한은 외부 관리자가 별도 집계한다. timeout은 해당 호출 컨테이너만 중지하고 원문/상태를 보존한다.
