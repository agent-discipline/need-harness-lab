# 격리·캐시·사용량 측정

## 격리 단위

각 `arm/core/run_id`마다 새 컨테이너 또는 VM, 새 에이전트 상태 디렉터리, 새 세션ID, 새 작업 복사본, 별도 DB volume·은행 journal·Compose project를 사용한다. 구현 컨테이너에는 해당 core와 공통 공개 fixture 계약만 마운트한다. 호스트 전체 프로젝트, Docker socket, 다른 arm, 평가자 정답, 기존 대화·memory·브라우저 profile을 마운트하지 않는다. 외부 판정은 평가자에서 실행한다.

각 공급자의 인증은 별도 주입한다. 인증 비밀을 코드/로그/이미지에 복사하지 않는다. 브라우저를 쓰지 않는 CLI 실험에서는 쿠키가 해당 없음이다. 브라우저가 필요하면 profile을 run별로 새로 만들고 세션 스토리지도 격리한다. 같은 계정 사용과 같은 대화 사용은 다른 개념이다.

내장 서브에이전트의 fresh context만으로 파일 접근 격리는 되지 않는다. 이 워크스페이스의 형제 디렉터리를 볼 수 있으면 strict isolation 판정 실패다. 본 준비에 참여한 에이전트는 본 실험에 재사용하지 않는다. **run 사이에는 초기화하고, 같은 run의 수정 턴 사이에는 대화·코드를 유지한다.** 부모 PM과 자식 워커도 필요한 brief만 받고 다른 실험 대화는 상속하지 않는다.

Gradle 의존성·Docker base image의 공통 캐시는 결제 솔루션 캐시와 구별한다. 모든 arm을 동일하게 사전 다운로드해 네트워크 시간 변동을 줄일 수 있다. 작업 build/output 및 에이전트 기록은 복제하지 않는다. 공통 코드 원본은 SHA256으로 비교한다.

## 서버 측 토큰 캐시

[OpenAI 공식 prompt caching 문서](https://developers.openai.com/api/docs/guides/prompt-caching)는 모델별 자동/명시적 캐시와 usage의 cached/write token 필드를 설명한다. 클라이언트의 세션 삭제·쿠키 초기화·`--ephemeral`은 공급자 내부 캐시 삭제를 뜻하지 않는다. 이번 설치 CLI0.155.1의 `exec --help`에는 ephemeral/ignore-user-config가 있지만 캐시 비활성화를 보장하는 옵션을 확인하지 못했다.

후속 사용자 합의로 이번 본 비교는 **observed-cache**를 사용한다. 독립 컨테이너와 exec/resume 세션 제어로 코드/대화 오염을 막고 공급자 캐시 할인은 별도 기록한다. 정책 구분은 다음과 같다.

- `strict-zero-cache`: 실제 선택 모델/전송 경로에서 cache-read/write 제어 가능 여부를 검증하고, 모든 호출의 read/write 사용량이 관측되어0인 실행만 인정. 관측 불가(null)는0으로 간주하지 않는다. 지원되는 API의 explicit-only/no-breakpoint 방식도 **해당 모델과 런타임으로 검증한 후** 사용한다.
- `observed-cache`: 기본 현실 비교. fresh session으로 솔루션/기억 전파를 차단하고 cached read/write를 측정한다. raw input+output, cache 비율, 실제 비용을 함께 공개. strict 결과와 혼합하지 않는다.
- 임의 nonce·cache key 변경·대기만으로 캐시가 완전히 지워졌다고 주장하지 않는다. 캐시 미관측 CLI에서는 strict 실험 개시 불가로 표기한다.

토큰 캐시는 모델이 이전 구현 내용을 새 대화에서 기억한다는 것과 동일하지 않다. 이 실험에서는 대화/파일 오염과 캐시 할인 영향을 각각 통제·기록한다.

## 집계 경계

시작은 조건별 최초 요구사항 생성/전달 유료 호출, 종료는 모든 필수 통과·리뷰·완료 문서까지 마친 마지막 응답이다. 구현자만의 값과 전체 비용을 함께 공개한다. 역할별 `prompt-author / pm / user-proxy / implementer / test-designer / reviewer` 합을 보존한다. 질문 답변을 부모가 대신 생성하면 부모의 그 호출도 해당 run 비용이다.

원본JSONL → provider adapter → 정규화 이벤트 → 합계의 구조다. 원본은 수정하지 않고 hash·CLI버전·시각·session/parent ID를 보존한다. 이벤트 delta만 합산하고 누적 snapshot은 마지막 값 또는 차분으로 처리해야 한다. 부모 usage가 자식 포함값인지 확인하지 못하면 aggregate와 child를 동시에 더하지 않는다. reasoning은 output의 부분집합이면 재가산하지 않는다. Anthropic의 cache read/write 별도 input 계산은 adapter에서 처리한다.

SDK/API bill과 구독 UI percent는 서로 대체할 수 없다. 구독형 Codex에서 요청별 실제 청구금액이 없으면 `billed_cost=null`, 별도 단가표 추정은 `estimated_cost`로 표시한다. 워커/compaction/retry 누락이 있으면 `usage_complete=false`이며 정량 비교에서 제외한다. 공급자가 보고하지 않는 필드는 null로 유지한다. [측정 모듈](../../measurement/README.md)을 따른다.
