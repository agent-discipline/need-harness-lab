# 토큰 측정 어댑터

Python 표준 라이브러리만 사용하며 모델 호출 없이 저장된 사용량 이벤트를 정규화한다. 원본 로그는 보존하고 이 디렉터리의 예제는 합성 데이터로만 사용한다.

```sh
python3 measurement/normalize.py --provider codex --run-id pilot-01 --session-id arm1-main --role implementer --model gpt-sol-high --input measurement/fixtures/codex.jsonl --output /tmp/normalized.jsonl
python3 measurement/summarize.py /tmp/normalized.jsonl --output /tmp/summary.json
python3 -m unittest discover -s measurement -v
```

위 명령은 `payment-settlement`에서 실행한다. provider는 `codex`, `openai`, `anthropic`을 지원한다. 구현자/오케스트레이터/작업자/검토자는 각각 implementer/orchestrator/worker/reviewer로 기록한다. `--model`은 실험자가 붙인 라벨이며 실제 모델 사용을 증명하지 않는다. `actual_model`은 원본 응답에 존재할 때만 보존한다. 실행 설정 및 서버 반환 모델 확인은 별도 사전 점검이다.

## 원본 계약과 중복 방지

CLI0.155.1 실측에서 `codex exec --json`의 `turn.completed.usage`는 **같은 세션 누적값**이었다. `runner/launch.py`는 원본 누적값을 보존하고 직전 snapshot을 빼서 `usage_semantics=delta` 이벤트를 만든다. normalize에는 이 차분 이벤트만 전달한다. 세션이 바뀌면 counter baseline도 새로 시작하고 감소하면 오류로 처리한다. 기본 이벤트에 고유 ID가 없을 수 있으므로 수집 시 세션 내 영속적인 `event_id`(예: turn-0001)를 한 번 부여하고 이후 재처리에서도 유지한다. 파일을 재수집할 때마다 ID를 바꾸면 안 된다. 원본 바이트 로그를 별도로 보존한다. 이벤트 내용 해시를 ID로 쓰면 사용량이 같은 다른 요청이 사라지므로 그렇게 하지 않는다.

OpenAI는 최종 Responses 응답 또는 `response.completed` 이벤트, Anthropic은 스트림을 조립한 최종 message를 받는다. Anthropic message_start/message_delta 조각은 거부한다. 누적 token_count 스냅샷과 cumulative/unknown 사용량은 거부하며 요청별 증분을 추출하는 별도 어댑터 없이는 합산하지 않는다. Codex 이벤트는 delta 표시가 없으면 거부한다. OpenAI/Anthropic의 지원 최종 이벤트는 해당 provider의 요청별 사용량 계약을 따른다. 임의 누적 수치를 최종 응답으로 위장해 입력하지 않는다.

중복 키는 `(provider, run_id, session_id, event_id)`이며 같은 키의 다른 내용은 오류다. 여러 파일에 걸친 중복도 집계에서 제거한다. 세션 ID는 실제 호출 소유 세션을 유지하고 로그 복사마다 새 ID를 붙이지 않는다. 하나의 요청을 Codex와 OpenAI 양쪽에서 동시에 집계하지 않는다. 각 실행은 하나의 수집 경로를 선택한다.

부모와 자식의 **자기 요청 사용량만** `usage_scope=own_request`로 기록한다. 자식 토큰을 포함한 부모 요약은 입력하지 않는다. 자식 raw 이벤트와 부모 집계 보고서를 함께 더하면 안 된다. 자식 실행은 서로 다른 session_id로 원본 로그를 보존한다. 도구가 자식 포함 여부를 명시하지 않으면 집계하지 말고 측정 불가로 기록한다. 모든 실험군의 오케스트레이터 후속 프롬프트 작성, 하네스 질의 응답, 작업자 및 검토자 비용을 같은 경계로 수집한다.

## 숫자 해석

- Codex와 OpenAI의 input_tokens에는 캐시 입력이 이미 들어 있으므로 다시 더하지 않는다.
- Anthropic의 input_tokens는 캐시 제외 값이다. cache_read_input_tokens와 cache_creation_input_tokens를 더해 전체 입력으로 바꾼다. 두 항목이 누락되면 정확한 전체 입력을 구할 수 없으므로 거부한다.
- cached_input_tokens가 누락되면 null이다. 알려지지 않은 캐시 사용량을 0으로 추정하지 않는다. 어느 한 이벤트라도 결측이면 집계 캐시 값 역시 null이다.
- reasoning_tokens는 출력에 포함된 부분집합이다. 총합은 **input_tokens + output_tokens**이며 reasoning을 다시 더하지 않는다.
- `actual_cost_usd`는 모든 이벤트에 provider_reported_cost_usd가 있을 때만 합산한다. 단가 파일 어댑터는 아직 없으며 보고 비용이 없으면 null이다. 토큰 수를 임의 단가로 실제 비용이라고 표시하지 않는다.
- 캐시 읽기/쓰기 숫자는 계측값이다. 이 도구는 서버 캐시를 비활성화하거나 세션 격리를 보증하지 않는다. cached=0 실험과 cached>0 실험, cached=null 실험은 구분해야 한다.

집계기는 실행 하나만 받는다. 실험 간 비교는 각 실행의 summary를 이용한다. 성공 여부, 소요시간, 재시도, 품질 점수, 테스트 종료시각은 실험 실행 기록에서 따로 연결한다. 최초 요구사항부터 최종 통과까지 실패한 호출과 수정을 포함하여 로그를 빠짐없이 수집한다. usage가 반환되지 않은 실패 호출은 토큰 0으로 처리하지 말고 별도 누락 기록을 남겨 해당 실행의 측정 완전성을 표시한다.

추가 계측 역할: 프롬프트 작성자는 `prompt-author`, PM은 `pm`, 사용자 대리 응답자는 `user-proxy`, 테스트 설계자는 `test-designer`로 기록한다.

CLI0.155.1의 `cache_write_input_tokens`, `reasoning_output_tokens`도 보존한다. 실제2턴 검증은 `results/setup/codex/` 참조: input13086→26213, output7→17로 누적되며 올바른 총합은26230이다. 재개값을 그대로 더한39323은 잘못된 중복 집계다.
