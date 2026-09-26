# Fable 독립 완성도 리뷰

각 최종 후보에 동일한 정적 리뷰를 1회 수행한다. 공통 실행 판정기를 대체하지 않는다. 하네스 내부 리뷰와 별도이며 외부 평가 토큰으로 집계한다.

```sh
python3 payment-settlement/codex/review/run.py \
  --source <implementation-directory> --output <new-review-directory>
```

- 모델: `claude-fable-5-1`, high. 첫 제외 파일럿에서 `fable` 별칭이 해당 실제 모델로 해석됨을 확인하여 고정했다. 자동 fallback은 없다.
- 매번 새 Docker 컨테이너, fresh filesystem, 기존 세션/메모리/설정 미마운트. 사용자 OAuth 인증 파일만 readonly 정상 사용한다. 호스트 설정은 변경하지 않는다.
- 익명 `/packet/source`에 코드·테스트·실행 스크립트, 공통 요구사항·판정 계약·은행 계약만 전달한다. 방법 번호/프롬프트 이력/하네스 생성 문서를 전달하지 않는다.
- 코드 readonly, Read/Glob/Grep만 허용, safe-mode, 사용자 설정 및 MCP 차단. 리뷰어는 코드를 수정하거나 테스트를 실행하지 않는다.
- `stdout.jsonl` 원본, `stderr.txt`, `prompt.txt`, `invocation.json`, 입력 packet 및 SHA256, `summary.json`, 추출 가능한 경우 `review.json` 보관. CLI 완료와 품질 합격은 서로 다르다.
- 전체 입력은 `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`; 출력과 합쳐 총 토큰을 산출한다. thinking은 출력의 부분집합으로 중복 합산하지 않는다. `modelUsage`는 교차검증용이며 result usage와 다시 합산하지 않는다. list cost는 구독 실제 청구액이 아니다.
- 서버 캐시는 강제로 끌 수 있다고 주장하지 않는다. 캐시 read/write를 관측하고 별도로 기록한다.

## 최초 파일럿

`../docs/pilots/detailed-01/review/attempt-01`에서 실제 실행 성공. 모델 `claude-fable-5-1`, 8/12, NEEDS_WORK, 총70,899토큰, list cost $1.52109075. 레이어 역참조·중복·테스트 범위 부족을 지적했다. 리뷰 결과만으로 금전 정확성이 입증되지는 않는다.

첫 packet은 src만 포함하여 기존 `scripts/restart-recovery.sh` 및 은행 계약이 누락되었다. 리뷰의 restart 미제공/은행 미검증 언급은 이 제한과 함께 해석해야 한다. runner는 다음 실행부터 scripts와 은행 계약을 포함하도록 수정했다. 기존 원본 packet과 결과는 변경하지 않았으며 비용 절약을 위해 동일 파일럿 전체 리뷰를 다시 실행하지 않았다. 공통 계약도 Fable 1회 정책으로 정합화했다.

## 엄격한 결과 게이트와 무료 검증

`review_completed`는 CLI 성공뿐 아니라 JSON 구조, 6개 고정 항목, 각 정수 0..2, 합계, finding 필드, 실제 모델 exact match 및 사용량 검증이 모두 통과해야 true이다. 구조/모델 오류는 exit 2로 차단한다. 정상 NEEDS_WORK는 리뷰 수행 성공(exit 0)이지만 `quality_passed=false`이며 실험 완료가 아니다. PASS에는 10점 이상, zero 없음, 미해결 correctness finding 없음이 필요하다. `runtime_acceptance_proven`은 언제나 false이다.

`usage-ledger.jsonl`은 도구 호출을 포함한 CLI 전체 1회의 최종 usage만 보관한다. 범위는 `own_cli_call_aggregate`로 명시하며 기존 `own_request` 스키마로 가장하지 않는다. 공통 보고서는 이 ledger를 외부 reviewer phase로 한 번만 더해야 한다. assistant usage 및 modelUsage를 다시 합산하지 않는다.

```sh
python3 -m unittest discover -s payment-settlement/codex/review -v
```

무료 합성 테스트 8개 통과: malformed/no result/wrong model/score mismatch/unresolved correctness/zero/PASS/NEEDS_WORK. 최초 파일럿은 strict schema 도입 전 기록이므로 원본을 그대로 보존한다. 유료 재실행은 하지 않았다.

## 별도 C20 실행 도구

`../scripts/load_check.py`는 Fable을 호출하지 않고 기존 owned Docker stack을 측정한다.

```sh
python3 payment-settlement/codex/scripts/load_check.py \
  --project payment-codex-<owned-run> --output <new-evidence-dir> \
  --pending-sql-file <evaluator-reviewed-select.sql> \
  --audit-command-json '["python3", "<reviewed-c19-hook.py>"]' \
  --phases warmup normal
```

`--phases` 생략 시 warmup/normal/spike_recovery/soak/capacity25/50/100/200 계약 시간 그대로 순차 실행한다. 스키마별 pending query는 단일 SELECT, audit hook은 exit 0 및 stdout JSON `{"passed":true}` 또는 `{"status":"PASS"}`를 반환해야 한다. 각 phase의 stdout/stderr, k6 summary, Docker inspect, 5초 간격 cgroup memory/pids/events와 프로세스 VmRSS, app IP별 DB 연결 수, 30초 pending 수렴, 전수 audit 결과를 보존한다. 총 연결 수가 10 초과하거나 OOM/restart/관측오류면 실패한다. soak RSS 중간 초기구간과 마지막구간 중앙값 증가가 20% 초과하면 실패/조사가 필요하다.

다른 구현/빌드는 상위 controller가 중단한 독점 시간에만 실행한다. 이 도구는 다른 stack을 중지하거나 볼륨을 삭제하지 않는다. 종료된 k6 컨테이너도 증거를 위해 남긴다. 선택 phase 성공은 전체 C20 합격이 아니다. `all_acceptance_passed`는 항상 false다. spike_recovery는 동일 k6 프로세스에서 연속 부하를 발생시킨다. 전체 필수 phase와 자원/수렴/audit가 모두 통과해야 c20_passed=true다.
