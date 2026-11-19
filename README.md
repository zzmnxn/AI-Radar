# AI Radar

AI 기술 소식을 한국어 카드로 정리하고 원문과 인용 근거를 함께 보여줍니다.

## 로컬 미리보기

Python 3.12 이상과 Node 24 이상이 필요합니다. 테스트 데이터로 전체 발행 경로와 화면을 확인할 수 있습니다.

```sh
python -m pip install .
python -m radar run --no-llm --data .preview-data
python -m radar validate-data --data .preview-data
npm --prefix web ci
RADAR_DATA="$PWD/.preview-data" npm --prefix web run dev
```

## 실제 브리핑

`python -m radar sources verify`로 소스 응답과 날짜를 확인합니다. `python -m radar collect --dry-run`은 API 키 없이 실제 수집 요청을 보내고 소스별 항목 수, 요청 수, 누락 사유를 출력합니다. 첫 수집 범위는 최근 72시간입니다.

아래 값을 환경에 설정한 뒤 `python -m radar run`을 실행하면 검증된 스냅샷이 `data/`에 생성됩니다.

- `OPENAI_API_KEY`
- `RADAR_MODEL`
- `RADAR_DAILY_BUDGET_USD`
- `RADAR_INPUT_USD_PER_MILLION`
- `RADAR_OUTPUT_USD_PER_MILLION`

요금은 설정한 단가를 바탕으로 KST 날짜별 추정액을 계산합니다. 실제 입력·출력 토큰과 호출 수는 `data/state/llm-budget.json`에 기록합니다.

`config/interests.yaml`의 초기 편집 관심사와 `config/pipeline.yaml`의 풀 비율로 관심·영향·탐색 후보를 배정합니다. 기본 목표는 15건이며, 남은 예산으로 평가와 판정을 감당할 수 있는 만큼 줄입니다. 평가를 시작할 수 없는 금액이면 상위 후보를 `data/state/analysis_queue.json`에 보관합니다. 대기 후보는 신선도 기준 72시간까지 다시 평가할 수 있습니다. 새 후보와 함께 배정할 때 대기 후보는 실행 슬롯의 3분의 1까지 차지합니다. 실행 가능한 후보가 대기열에만 있으면 한 건부터 재시도할 수 있습니다. 일시적 요청 실패 뒤에는 각각 15분, 1시간이 지난 다음 실행에서 재시도합니다.

분석 결과는 사건 리비전과 분석기 버전별로 `data/state/analyses/`에 저장합니다. 원문은 실행 중 메모리에만 두고, 저장된 분석에는 출처 메타데이터와 확인된 인용을 남깁니다. 저장된 분석은 다시 사용할 수 있습니다.

`run --date YYYY-MM-DD`는 한국 날짜를 기준으로 실행합니다. 마지막 발행일보다 이른 날짜는 건너뜁니다. 같은 날짜를 다시 발행하려면 `--force`를 사용합니다. 과거 날짜의 미리보기는 별도 데이터 디렉터리에서 실행할 수 있습니다.

실행 요약은 `data/runs/`의 월별 JSONL에 저장합니다. 상세 로그는 `out/run-log.json`에 기록하며 Actions에서 30일간 보관합니다. 발행 도중 프로세스가 종료되면 다음 `run` 또는 `validate-data`가 마지막 커밋 상태를 복구합니다. 검증은 각 발행 파일의 해시와 최근 120일 카드의 분석·사건 참조를 확인합니다.

실제 실행의 소스별 수집 상태와 누락 구간은 `data/state/source_health.json`에 기록합니다. 범위 조회를 지원하는 소스는 다음 실행에서 최근 7일 안의 누락 구간을 먼저 재조회합니다.

`python -m radar collect --record --data .preview-data/events-sample`로 모델 호출 없이 사건과 수집 상태를 저장할 수 있습니다. 사건은 최초 수집 월(KST)에 따라 `state/events/`에 저장되며, 같은 항목을 다시 수집해도 행이 추가되지 않습니다. 제목이 비슷한 사건은 기본 설정에서 관련 사건으로 연결됩니다. 새 항목을 그룹화할 때 적용하는 URL 쌍의 병합 예외는 `config/merge_overrides.yaml`에 설정합니다.

`config/taxonomy.yaml`의 규칙은 모든 사건에 적용됩니다. 실행할 때 오늘과 어제(KST)의 집계를 `data/state/trends/<날짜>.json`에 다시 씁니다. 브리핑에는 최근 14일 신규 용어와 수집 소스 수를 보정한 최근 7일 급증 용어를 최대 3건 표시합니다. 급증 판정에는 기준 기간의 수집 기록이 최소 30일 필요합니다.

GitHub 공개 저장소 검색은 `GITHUB_TOKEN`이 있으면 인증하고, 로컬에서 토큰이 없으면 공개 검색 한도로 실행합니다. GitHub Actions에서는 실행 토큰이 설정됩니다.

GitHub에서는 API 키를 Actions secret, 나머지 값을 Actions variables로 등록합니다. Pages 소스를 GitHub Actions로 선택하고 기본 브랜치에서 **Publish AI Radar**를 수동 실행합니다. 기존 스냅샷을 다시 배포할 때는 `deploy_only`를 선택합니다.

### 발행 운영

예약 실행은 한국 시각 06:07과 07:37입니다. 같은 날 이미 성공한 브리핑은 생성과 커밋을 건너뛰고, 검증된 동일 커밋을 Pages에 다시 배포할 수 있습니다. 실행 중인 발행은 유지됩니다. 같은 그룹의 대기 실행은 새 예약이나 수동 실행으로 교체될 수 있으므로 취소된 `force`·`deploy_only` 실행은 다시 요청합니다.

- `date`와 `force`로 실행일과 같은 날 재발행 여부를 지정합니다.
- `no_llm`은 실행기 임시 디렉터리에서 합성 데이터를 검증합니다. 원격 브랜치와 Pages에 반영되지 않습니다.
- `fail_stage=verify`는 검증 단계 실패를 주입합니다. 실데이터 드릴에서는 직전 브리핑을 유지하면서 실패 상태를 배포합니다. 테스트 데이터에서 검증하려면 `no_llm`도 선택합니다.
- `deploy_only`는 브리핑 생성과 모델 호출 없이 마지막 스냅샷을 검증하고 재배포합니다.
- 실행 요약에서 Generation, Validation, Commit, Deployment를 각각 확인할 수 있습니다.
