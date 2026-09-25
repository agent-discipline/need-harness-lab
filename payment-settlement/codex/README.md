# Codex 실행 영역

이 폴더는 Codex만 소유한다. 공통 계약·판정기는 상위 폴더를 참조하고, 코드와 대화 기록은 아래처럼 분리한다.

```text
codex/
├── README.md
├── 1/                          # 짧은 요청 → 한 항목씩 수정
│   └── core1/ ... core10/      # 독립 시행별 Kotlin 서버
├── 2/                          # 상세 프롬프트 → 구조화된 수정
│   └── core1/ ... core10/
├── 3/                          # 하네스 auto → 외부 관리자의 대화 제어
│   └── core1/ ... core10/
├── docs/
│   ├── experiments/
│   │   ├── 1/core1/ ... core10/
│   │   ├── 2/core1/ ... core10/
│   │   └── 3/core1/ ... core10/
│   │       ├── run.json        # 모델·상태·격리·캐시·기준 SHA
│   │       ├── transcript.md   # 1. 프롬프트 / 1. 결과물 분석 / 2. 프롬프트 …
│   │       └── events.jsonl    # 원문과 해시를 보존하는 기록
│   ├── results/               # 실행 순서·집계·준비 검증 증거
│   ├── runbook.md
│   └── isolation-and-measurement.md
├── template/                  # 동일한 Kotlin 초기 서버
├── prompts/                   # 공통 입력·조건별 절차·후속 입력 형식
├── runner/                    # 별도 Docker Codex exec/resume와 원문 수집
├── scripts/                   # 복제·동일성·사전 점검·시행별 커밋
├── harness-source/            # 고정 SHA의 하네스 원본, 자동 설치하지 않음
├── compose.yaml               # 공통 DB·은행·판정기를 연결하는 Codex 설정
└── .runs/                     # 실제 격리 workspace·세션 수집 원본, Git 제외
```

`coreN`은 독립 반복 번호다. **각 조건10회, 총30회**를10사이클로 실행한다. 사이클 내부의1·2·3 구현은 병렬, 부하 측정은 순차이며 세 시행의 문서화/커밋 후 다음 사이클로 넘어간다. 시행 기록은 서버 코드 폴더에 넣지 않는다. `.runs/<run>/workspace`에는 해당 서버만 내보내므로 다른 시행과 외부 평가 기록을 볼 수 없다.

공통 기준: [계획](../docs/experiment-plan.md), [요구사항](../docs/requirements.md), [판정 계약](../docs/acceptance.md), [인프라](../infra/README.md), [측정 어댑터](../measurement/README.md).

## 실행

저장소 루트에서 실행한다.

```bash
python3 payment-settlement/codex/scripts/lab.py verify --core 1
docker compose -p payment-lab-smoke -f payment-settlement/codex/compose.yaml up --build -d --wait
docker compose -p payment-lab-smoke -f payment-settlement/codex/compose.yaml --profile test run --rm k6 run /tests/smoke.js
```

Codex는 최초 `exec`, 이후 정확한 세션ID의 `resume`로 제어한다. goal은 사용하지 않는다. [runner 사용법](runner/README.md)과 [시행 절차](docs/runbook.md)를 따른다. 각 시행 종료 시 문서·사용량·검증 증거를 마감하고 그 시행 코드와 `docs/experiments` 기록을 함께 커밋한다. 같은 사이클의 세 시행 마감 후 다음 사이클로 진행한다.

현재 결과: [초기 검증](docs/results/setup/README.md). 초기 서버와 실제 Codex2턴 재개를 확인했고, 본 실험 상태는 각 `run.json`이 정본이다.
