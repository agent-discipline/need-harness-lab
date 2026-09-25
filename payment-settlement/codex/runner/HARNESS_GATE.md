# Arm 3 외부 승인과 사용량 수집

`launch.py`는 arm 3의 `/workspace`를 승인 전 Docker readonly bind mount로 연다. PM과 native worker 모두 같은 읽기 전용 파일시스템 제약을 받는다. 승인 파일은 workspace 밖의 run 디렉토리에 두며 컨테이너에는 마운트하지 않는다. goal은 사용하지 않는다.

## 대화 진행

1. 첫 turn에 인터뷰 질문만 출력하도록 요청한다. 질문/답변 전체를 해당 시행 transcript에 기록한다.
2. 외부 controller가 공통 answer-policy에 따라 답하고 동일 thread를 `--resume`한다.
3. PM은 명세를 파일에 쓰지 않고 응답에 JSON으로 출력한다. 필수 필드는 `goal`, `scope`, `constraints`, `acceptance`, `test_plan`, `risks`이다. 추가 필드는 허용한다.
4. controller는 원문 JSON을 run 외부 임시 파일에 저장하고 내용이 공통 계약과 일치하는지 검토한다. 부족하면 같은 readonly 세션에 수정 요청한다. 인터뷰 답변 원문도 별도 파일에 저장한다.
5. 검토 완료 후 다음 명령으로 승인한다. 이는 사용자가 위임한 controller의 승인이고 새 사용자 확인 요청을 뜻하지 않는다.

```sh
python3 payment-settlement/codex/runner/harness_gate.py approve \
  --run payment-settlement/codex/.runs/RUN_ID \
  --spec /tmp/RUN_ID-spec.json --answers /tmp/RUN_ID-answers.txt
```

6. 승인 JSON과 답변의 SHA256이 기록된다. 다음 turn부터 workspace가 writable이다. 승인된 명세 원문을 승인 프롬프트에 포함하고, PM이 workspace에 명세/로그를 작성한 뒤 하네스 구현, 독립 테스트 worker, 고위험 dual review 절차를 진행한다. 승인 이전 초안은 응답/원문 로그에 보존한다.

승인 뒤 명세 파일이 변조되면 hash 불일치로 다시 readonly가 된다. 승인 내용의 의미 검토는 controller 책임이며, 이 게이트는 명세의 품질을 자동 판정하지 않는다. 워커 완료/검토 증거 및 실험 공통 판정기는 별도로 충족해야 한다.

## 사용량

CLI `turn.completed`는 PM 누적 사용량이며 native worker 사용량을 포함하지 않는 것이 실제 smoke에서 확인되었다. 따라서 arm 3에 CLI 총계만 사용하면 안 된다.

```sh
python3 payment-settlement/codex/runner/harness_gate.py audit \
  --sessions payment-settlement/codex/.runs/RUN_ID/turns
```

각 turn의 전체 session snapshot을 함께 읽고 `token_usage_record.payload.response_id`로 중복을 제거한다. PM과 worker의 개별 response usage를 더하며 CLI/turn/thread 누적 총계를 다시 더하지 않는다. `per_thread`로 역할별 비용을 분리한다. resume마다 snapshot이 반복되므로 turn별 audit 총계를 합산하면 안 된다. 입력 token에는 cached input이 포함되고 reasoning output은 output의 부분집합이다.

모든 spawn 호출 수와 수집한 worker 경로 수를 대조한다. `all_spawned_workers_collected`, `all_threads_have_usage`, `all_threads_have_model_context`, `configured_models_verified`가 모두 참이어야 한다. 비정상 종료/실패한 spawn으로 수가 다르면 자동 통과시키지 않고 원문 로그를 분석한다. 동일 경로 재사용 등 일반화되지 않은 경우에도 보수적으로 판정을 멈춘다. 실제 backend 모델 식별은 관측되지 않아 `backend_model_verified=false`이며 client 설정 증거와 구분한다.

## 실제 smoke 결과 (본 실험 제외)

[검증 결과](../docs/results/setup/harness-gate/verification.json): 승인 전 쓰기 실패, 승인 후 marker 쓰기 성공. PM 및 worker 각각 Sol high 설정 확인. PM 53,993 + worker 12,881 = 총 66,874 tokens. cached input 50,560. 한 번의 worker spawn과 한 개 worker session 수집이 일치했다.

원시 session은 `.runs/setup-harness-gate/turns/0001/sessions`에 보존한다. 공개용 prompt/CLI events/검증 JSON은 위 결과 폴더에 보존하며 인증 파일은 복사하지 않는다.
