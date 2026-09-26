# 독립 판정기

구현자가 수정하지 않는 공통 판정 코드다. 구현의 자체 테스트 보고를 합격 증거로 사용하지 않는다.

## 실행

```sh
python3 payment-settlement/evaluation/acceptance.py --base-url http://localhost:25080 --namespace unique-run --pilot-audit --output results/http.json
python3 payment-settlement/evaluation/pilot_faults.py --namespace unique-fault-run --output results/faults.json
python3 payment-settlement/evaluation/pilot_adapter.py --output results/audit.json
python3 -m unittest discover -s payment-settlement/evaluation -p 'test_*.py' -v
```

`pilot_adapter.py`와 `pilot_faults.py`는 제외 파일럿의 스키마/컨테이너에만 적용한다. 다른 구현은 평가자가 동일한 의미의 `snapshot`, `verify_requests`, `audit`, SQL/clock/fault adapter를 작성하고 검토해야 한다. 업무 스키마를 구현자에게 강제하지 않는다. 외부 장애 runner는 `payment-codex-pilot-verify` 이외 컨테이너를 조작하지 않는다.

HTTP runner는 C01~C12, C15, C16, C18을 실행한다. `--pilot-audit`를 지정하면 매 시나리오 전후 영속 snapshot, 요청과 영속 payload/key 대조, reject의 신규 금융 효과, 기존 금융 필드 불변성, 은행 journal/원장/잔액/수수료/정산 전수 대조를 함께 검증한다. HTTP만 실행하면 PARTIAL이며 전체 합격을 주장하지 않는다. C19는 독립 adapter의 전수검증이다.

장애 runner는 은행 journal에 송금이 생겼고 DB가 PENDING임을 직접 확인한 다음 app 또는 DB에 SIGKILL을 보낸다(C13/C14). 이미 SUCCEEDED라면 장애 경계를 놓친 실패로 기록한다. 재시작 health 이후 30초 이내 수렴, 동일 ID replay, 전수 대조가 필요하다. C17은 KST 자정 전후 succeeded_at fixture, immutable snapshot, 실제 은행 중단 후 늦은 성공의 확정 날짜를 검증한다. 시각 fixture와 정산 결과는 증거로 남으므로 **검증 후 DB를 다른 시행에 재사용하지 않는다.**

각 독립 시행은 fresh volume에서 시작한다. 한 correctness 실행 내 시나리오는 고유 키와 정산 대행사 범위를 사용하고 전후 snapshot으로 효과를 대조한다. 동일 runner를 같은 DB에 반복하면 정산/잔액 fixture가 오염된다. 새 namespace만으로 잔액 초기화가 되지는 않는다.

## 결과 해석

- PASS: 명시된 시나리오에서 실제 검증한 계약 충족.
- FAIL: 관측 위반 또는 판정 실행 오류. 오류 전문과 요청 증거를 보존한다.
- PARTIAL: HTTP만 확인했거나 필요한 증거가 부족함.
- NOT_RUN: 이 runner 범위 밖. 성공으로 집계하지 않는다.

HTTP 결과의 `all_acceptance_passed`는 항상 false다. C13/C14/C17/C19/C20 및 외부 품질 리뷰 결과를 모으는 상위 controller만 전체 합격 여부를 판단한다. C20은 `k6/workload.js`와 별도 자원 관측 결과를 함께 사용한다. Fable 리뷰는 실행 판정과 별도 증거다.

## 판정기 검증

`test_reconcile.py`는 정상 레코드가 통과하고 중복 차감, 원장 누락, 은행 payload 변경, 잘못된 정산 합계, orphan 은행 레코드, 이중 정산 item이 실패하는지 검증한다. 실제 excluded pilot에서 KST 정산 오류를 발견했고 app/DB crash recovery는 전수대조까지 통과했다. 시나리오별 원문 증거는 Codex pilot 문서에 저장한다.
