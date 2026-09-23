# 실행별 프롬프트·분석 전문 기록

각 arm의 core1..core10은 서로 다른 run-dir를 사용한다. 해당 core 내부 부모 및 worker 기록은 같은 run-dir에 role/session-id를 구분하여 시간순으로 넣는다. 이 도구는 모델 호출을 수행하거나 프롬프트, 분석, 성공 여부를 생성하지 않는다. 실제 입력 파일의 내용을 그대로 보존한다.

`payment-settlement`에서 실행:

```sh
python3 recording/record.py --run-dir runs/arm1/core1/recording --init
python3 recording/record.py --run-dir runs/arm1/core1/recording --prompt-file /tmp/prompt.txt --analysis-file /tmp/analysis.txt --response-file /tmp/response.txt --role implementer --session-id arm1-core1-main --status NEEDS_FIX --evidence-files /tmp/k6-result.json
python3 -m unittest discover -s recording -v
```

초기화만 하면 NOT_STARTED 상태와 실제 기록 없음 문구를 생성한다. 반복 초기화는 기존 내역을 검증하고 보존한다. 기록은 prompt-file/analysis-file/role/session-id/status가 필수다. response-file은 선택이며 전달받으면 전문을 보존한다. 실제 에이전트 응답이 있는 경우 반드시 전달하여 구현 답변을 빠뜨리지 않는다. 입력과 분석, 응답은 UTF-8이며 증거 파일은 이진 파일도 base64로 보존한다. 예시 status 값은 NEEDS_FIX, PASS, FAILED, BLOCKED이며 도구는 이를 판정하지 않는다.

Python API는 `init_run(path)`와 `record_turn(run_dir, prompt_file, analysis_file, role, session_id, status, response_file=None, evidence_files=None)`다. CLI와 API 모두 같은 잠금과 검증을 사용한다.

## 저장 및 무결성

`events.jsonl`은 순번, UTC 시각, 파일 경로·전문·인코딩·바이트 수·SHA-256, 역할과 세션, 이전 이벤트 해시를 포함하는 추가 전용 원장이다. 원본 파일이 나중에 바뀌어도 당시 전문은 유지된다. `transcript.md`는 매번 원장에서 생성되며 `1. 프롬프트 전문`, `1. 결과물 분석`, `2. 프롬프트 전문` 순서로 표시한다. 응답 및 증거 전문도 함께 표시한다. 원본 내 백틱보다 긴 Markdown fence를 사용하여 긴 코드 블록도 안전하게 표시한다.

Linux fcntl 잠금으로 같은 run-dir 동시 기록을 직렬화한다. 기존 원장 해시 체인과 transcript 일치를 먼저 검사하며 변조 시 덮어쓰지 않고 실패한다. 같은 역할·세션에서 동일한 prompt/analysis/response 내용의 재제출은 중복으로 거부한다. 의도적으로 같은 요청을 재시도한 경우 분석 파일에 실제 재시도 내역을 기록하여 구분한다. 새 session-id를 임의 생성하여 중복 방지를 우회하지 않는다.

원장 추가 후 fsync하고 transcript를 원자적으로 교체한다. 프로세스가 두 작업 사이에 중단되면 원장에는 새 이벤트가 있고 transcript는 오래된 상태일 수 있다. 이때 다음 기록은 실패하며 파일을 보존한 상태에서 확인해야 한다. 복구는 원장을 `read_events`로 검증한 후 `render(events)` 결과로 transcript를 재생성한다. 일반 기록 명령은 수동 변경을 자동 복구하거나 덮어쓰지 않는다.

해시 체인은 우발적 수정과 일부 변조를 탐지하지만 외부 서명된 저장소가 아니다. 원장 전체와 transcript를 함께 다시 작성하는 공격, 마지막 이벤트 삭제 후 관련 파일 재생성은 별도 신뢰 앵커 없이는 탐지할 수 없다. 실험 종료 시 기록 전체를 Git 또는 별도 보존소에 저장하여 비교 기준을 남긴다. 프롬프트·응답·증거에 비밀값이 있으면 그대로 기록되므로 실험용 자격 증명만 사용한다.
