# 아키텍처 개선 보고서

기준: `a34f267`의 1차 구조 개선과 2026-08-10 후속 안정화 작업

이번 변경은 가이드의 첫 권장 범위인 **Calibration 사전 검증과 실행 Manifest 도입**, 그리고 두 번째 범위의 최소 골격인 **Pipeline Context와 Stage Result 도입**에 집중했다. YOLO, 2D-3D 투영, 지주 하단점 알고리즘과 기존 SHP/프레임 JSON schema는 변경하지 않았다.

## 수행한 변경

### 1. 실행을 명시적인 Job으로 기록

- canonical `JobStatus`와 허용 상태 전이를 정의했다.
- CLI는 실행마다 안전한 `job_id`를 만들고, 웹은 기존 run ID를 동일한 job ID로 전달한다.
- `run_manifest.json` schema version 1에 입력 fingerprint, canonical config hash, Git commit, 모델 hash, calibration ID/hash, 단계 진행률·시간·count, 출력, 구조화 오류를 기록한다.
- manifest와 `run_summary.json`/`run_summary.md`는 임시 파일 + `fsync` + `os.replace`로 atomic하게 기록한다.
- thread뿐 아니라 웹/파이프라인 프로세스 간 read-modify-write도 OS file lock으로 직렬화한다.
- terminal manifest는 일반 update로 변경할 수 없다. `failed -> retrying`만 전용 전이로 허용하며 attempt, 진행률, count와 출력을 원자적으로 초기화한다. 구조화 오류 추가와 실패 terminal 전이도 한 file-lock mutation으로 commit한다.
- 같은 출력 root 재사용 시 terminal manifest와 summary를 같은 lock 아래 `run_history/`로 이동하고, 비종료 manifest는 덮어쓰지 않는다.

### 2. 설정 provenance를 고정

- 기존 argparse 설정을 보존한 `PipelineConfig`를 추가했다.
- Path와 collection을 JSON 호환 형태로 정규화하고 key 정렬 canonical JSON의 SHA-256을 계산한다.
- 설정 tree를 원본 dictionary에서 분리해 재귀적으로 고정하고, 전달된 hash가 실제 값과 다르면 생성을 거부한다.
- 내부 실행용 `_...` 속성은 기존 `serializable_config()` 계약에 따라 hash에서 제외된다.
- 웹 launcher와 자식 파이프라인 사이에는 생성된 YAML 파일의 정확한 byte SHA-256을 별도로 보존·대조한다. 정규화 전 요청 hash와 default/CLI override 적용 후 effective config hash를 구분한다.
- 기존 YAML key, 기본값과 CLI override 의미는 유지했다.

### 3. Calibration을 모델 로딩 전에 전체 사전 검증

- `CalibrationResolver`가 Job/Track 이름의 대소문자와 `.job`/`.scan` suffix를 정규화한다.
- Pegasus bundle과 Leica delivery metadata의 매칭을 동일한 구조적 결과로 표현한다.
- 모든 선택 task를 먼저 검사해 누락과 동일 우선순위 다중 매칭을 수집한 뒤, 실제 task metadata를 변경하기 전에 실패한다.
- 오류에는 normalized key, 검색 root, 사용 가능한 key sample, 후보 수와 선택 방식이 포함된다.
- 성공한 매칭의 calibration ID, source fingerprint, `matched_by` 근거를 manifest에 남긴다.
- bundle `schema_version=2`와 track 구조를 검사하고, 지원하지 않는 버전/손상 JSON을 구조화 오류로 fail-fast한다.
- precomputed resolution은 task identity와 delivery metadata에 결합하며, 동일 delivery 파일의 fingerprint는 실행당 한 번만 계산한다.
- 기존 `attach_calibration_metadata()`의 payload와 반환 계약은 compatibility adapter로 유지했다.

### 4. Application/Domain/Infrastructure 경계의 최소 골격

- `PipelineContext`, `PipelineStage` protocol, immutable `StageResult`, artifact/warning/error model을 추가했다.
- 기존 거대 `pipeline.py`를 재작성하지 않고 다음 coarse stage를 추적한다.

```text
validate_config
discover_inputs
attach_calibration
load_or_build_spatial_index
validate_inputs
detect_project_and_estimate
write_outputs
finalize_manifest
```

YAML/CLI parse와 기본 schema 검증은 `run_pipeline()` 및 manifest 생성보다 먼저 일어난다. 따라서 이 구간의 실패는 `validate_config` stage에 기록되지 않으며, 해당 stage는 manifest 생성 뒤 모델 탐색과 추가 runtime 검증부터 추적한다.

- 프레임 처리 heartbeat를 제한된 주기로 manifest에 반영한다.
- 예외는 `PipelineErrorInfo(code, stage, job_id, retryable, context, cause_type)`로 보존하며 활성 stage를 `failed`로 닫는다.

### 5. 웹과 pipeline 상태 연결

- `POST /api/runs`가 pipeline 실행 전에 pending manifest를 만든다.
- SQLite run 상태 전이는 compare-and-set update로 경쟁 상태를 줄였다.
- Run API는 기존 `status`를 유지하면서 `canonical_status`, `attempt`, `current_stage`, `versions`, `counts`, `stage_results`, `error_info`를 additive field로 제공한다.
- SSE는 반복적인 로그 파싱 대신 유효한 manifest 진행률을 우선한다.
- 자식 exit code 0만으로 성공 처리하지 않고 `succeeded` manifest를 요구한다.
- 현재 attempt가 명시한 SHP와 7개 bundle component가 실제로 존재할 때만 성공 처리한다. 다중 모델 실행은 `models_manifest.json`의 JSON schema, 모든 모델의 completed/published 상태, 모델별 SHP 목록과 root manifest 목록의 일치까지 검증한다. 출력 폴더를 재탐색하지 않으므로 과거 pole SHP를 새 산출물로 잘못 귀속하지 않는다.
- 현재 attempt에 running/failed stage 증거가 남아 있으면 manifest 저장소 자체가 `succeeded` commit을 거부한다.
- launch 실패, child 실패, 취소와 서버 재시작을 manifest terminal 상태와 조정한다. 늦은 취소·shutdown보다 이미 검증을 마친 durable `succeeded` commit을 우선하고, `starting`은 재queue하지 않아 spawn 경계의 중복 실행을 막는다.
- 새 웹 run에는 execution-contract version marker를 저장한다. marker가 있는 run은 완료 후에도 manifest와 선언 SHP를 다시 검증하고 손상 시 결과 URL·SHP 다운로드/import를 숨기며, marker가 없는 기존 이력은 legacy 결과 접근을 유지한다.
- manifest뿐 아니라 공개 TXT/JSON artifact도 JSON으로 다시 읽어 절대경로를 재귀적으로 가리고, diagnostic log 직접 다운로드를 차단한다.
- React queue는 legacy 상태보다 더 구체적인 canonical 상태와 구조화 오류를 우선 표시하고, 미래의 알 수 없는 상태도 중립적으로 표시한다.

## 변경하지 않은 동작과 호환성

| 계약 | 이번 결과 |
|---|---|
| CLI 진입점과 `run_pipeline(args)` | 유지. wrapper가 manifest lifecycle을 추가했다. |
| 기존 `config.yaml` key/default/override | 유지. canonical hash와 typed view만 추가했다. |
| Calibration task payload | 기존 구조 유지. resolver 결과를 adapter에 주입한다. |
| YOLO/투영/지주/품질 알고리즘 | 변경하지 않음. |
| 프레임별 `txt/*.txt` JSON | schema와 파일명 유지. |
| sign/pole SHP schema와 CRS sidecar | 유지. 기존 staged bundle 검증·게시 경로 사용. |
| 단일/다중 모델 출력 구조 | 유지. 출력 root의 manifest/summary만 추가. |
| 웹 v1 `status`와 `completed` | 유지. canonical 상태는 별도 optional field. TXT/JSON 다운로드는 동일 데이터를 경로 마스킹된 JSON 응답으로 제공한다. |
| 구형 웹 run | manifest가 없으면 기존 bounded log parser로 진행률을 계산. |

호환성 rollback은 `a34f267`의 부모 commit으로 코드 전체를 되돌리거나, 해당 commit에서 manifest/application/web 연동 변경만 역적용하는 방식이다. 알고리즘과 SHP schema가 바뀌지 않았으므로 데이터 migration은 필요하지 않지만, 새 웹 UI가 canonical field를 사용하므로 backend와 frontend는 같은 revision으로 배포해야 한다.

## 추가한 테스트

- `tests/test_calibration_resolver.py`: 정확한 매칭, suffix/case 정규화, delivery 방식, 누락·모호성 집계, fail-before-mutation, legacy adapter 특성
- `tests/test_execution_architecture.py`: config/effective/file hash 안정성, launcher handoff identity, boolean type 거부, manifest schema/atomic lifecycle, terminal·retry/stage 불변식, 원자적 오류 commit과 archive rollback, 입력/output bundle·symlink/junction·models manifest 경계, pipeline 성공·실패와 stale SHP 배제
- `tests/test_webapp_run_safety.py`: manifest 우선 projection, Windows/POSIX/UNC 경로 redaction과 URL 보존, 구조화/plain-text artifact 정책, 현재 run의 선언 SHP만 공개, child 성공 조건, cancel·spawn·shutdown 경쟁, launch/child/restart 및 DB-manifest 복구
- `tests/test_webapp_optimizer_config.py`: 웹 생성 설정과 초기 manifest 연결
- `webui/src/components/RunQueue.test.tsx`: canonical stage/error와 알 수 없는 상태 fallback
- `webui/src/lib/vworld.test.ts`: VWorld 인증 오류, WebGL 3.0 초기화 순서, route camera,
  route/frame/SHP entity와 선택 target 회귀

## 검증 결과

2026-08-10, Windows 작업 환경에서 현재 작업 트리를 검증했다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 314 passed, 4 skipped, 1 warning in 76.13s

Set-Location .\webui
npm test -- --run
# 22 files, 73 tests passed

npm run build
# TypeScript compile + Vite production build succeeded

Set-Location ..
.\.venv\Scripts\python.exe scripts\verify_environment.py
# CUDA/NMS environment_check=OK (RTX 4070 Laptop GPU)
```

- Python 4건 skip은 현재 환경에서 사용할 수 없는 symlink/junction 또는 선택적 환경 조건을 검사하는 guard다. 경고 1건은 Starlette TestClient의 httpx deprecation이다.
- 지도는 same-origin iframe의 VWorld WebGL 3.0 외부 SDK로 전환되어 Vite
  bundle에 기존 MapLibre JS/CSS chunk를 포함하지 않습니다. 대신 VWorld 인증 origin과 외부
  SDK 가용성을 런타임 운영 조건으로 관리합니다.
- 지도 overlay는 route/range/frame/SHP별 data source로 분리하고 entity 변경 이벤트를
  batch 처리하여 프레임 선택이 대용량 SHP 전체 재생성을 유발하지 않도록 했습니다.
- 변경 Python 파일의 Ruff F/I 검사와 신규 application/domain/infrastructure 모듈의 E/F/I 검사는 통과했다. 저장소 전체 `ruff check . --statistics`에는 기존 파일을 포함한 175건의 정리 부채가 남아 있어 별도 작업이 필요하다.
- 작업 전 기록은 Python `258 passed, 2 skipped`, 웹 `64 passed`였다. 이번 architecture/web 계약 테스트가 추가된 뒤 전체 suite는 위 결과로 통과했다.

## 변경 전후 결과 비교

| 항목 | 변경 전 | 변경 후 |
|---|---|---|
| 실행 식별 | 로그/출력 경로 중심 | `job_id`, attempt, canonical lifecycle |
| 실패 위치 | traceback과 문자열 로그 확인 | failed stage + 구조화 오류 + 원인 타입 |
| 설정 재현성 | effective config 파일 | 요청 YAML byte hash + canonical effective hash + source + schema |
| calibration 실패 | attach 과정에서 개별 실패 가능 | 선택된 모든 Job/Track을 추론 전에 일괄 검증 |
| provenance | 프레임 JSON과 여러 로그에 분산 | root manifest에서 Git/model/config/calibration 연결 |
| 웹 진행률 | DB 상태와 로그 문자열 추정 | schema 검증된 manifest 우선, legacy fallback 유지 |
| 웹 성공 판정 | 주로 child return code | return code 0 + stage-consistent `succeeded` manifest + 완전한 현재 SHP/model manifest 계약 |
| 실행 요약 | 로그와 model manifest | `run_summary.json`과 `run_summary.md` 추가 |

실제 MMS golden dataset에 대한 전후 SHP 좌표·feature count 비교는 수행하지 못했다. 알고리즘 호출과 SHP/JSON schema는 의도적으로 유지했고 전체 회귀 테스트는 통과했지만, 대표 실제 데이터의 byte/좌표 동등성을 이 결과만으로 보증하지 않는다.

## 성능 영향

- 추론과 점군 알고리즘은 변경하지 않았다.
- manifest는 stage 경계와 제한된 heartbeat 주기로 작은 JSON을 atomic rewrite한다. 프레임마다 무제한 쓰지 않도록 1초 단위 throttling을 사용한다.
- 입력 inventory는 선택된 image task의 전체 count/fingerprint와 최대 1,000개 상대경로 sample만 기록해 대규모 실행에서도 manifest 크기가 선형 증가하지 않는다.
- calibration bundle은 resolver에서 한 번 읽고 기존 attach 경로에 resolution을 전달한다. 동일 delivery calibration fingerprint도 경로별 한 번만 계산한다.
- 실제 노선에 대한 wall-clock, GPU memory, NAS I/O benchmark는 이번 범위에 포함하지 않았다.

## 보류된 항목

가이드 전체가 완료된 것은 아니다. 다음 항목은 의도적으로 후속 단계로 남겼다.

- 독립 `validate` dry-run CLI
- concrete stage class와 단계별 typed input/output contract
- stage checkpoint, idempotency key, 실패 단계부터 재시작
- retryable 오류별 자동 정책과 `JobExecutor` interface(수동 attempt 전이 계약만 구현)
- `ImageDetection`, `ProjectedDetection`, `PoleCandidate`, `PoleBaseEstimate` 전환
- 지주 알고리즘 method version과 `valid/uncertain/invalid` companion metadata
- JSON/SHP feature count 대조와 별도 dry-run output validator(현재는 publisher 재개방 검증 + 현재 attempt가 선언한 bundle 존재·경계 검증)
- 구조화 로그, metrics/tracing exporter, worker heartbeat timeout
- 작은 실제 golden dataset과 shadow comparison 도구
- lint/static type check/fixture regression을 강제하는 CI quality gate

## 남은 위험

- `pipeline.py`가 여전히 약 9.8k 줄이고 알고리즘·orchestration·artifact 생성 경계가 넓다.
- manifest의 coarse processing 단계만으로 검출/투영/pole 중 정확한 재시작 지점을 만들 수 없다.
- 웹 SQLite 상태와 manifest는 서로 다른 저장소다. CAS와 terminal 동기화가 경쟁을 줄이지만 강제 종료 순간의 완전한 transaction은 아니다.
- 재시작 시 저장 PID가 원 child인지 안전하게 확인할 launcher identity가 없어 orphan process를 자동 종료하지 않는다. 비정상 종료 뒤 남은 GPU child와 새 queue 작업의 자원 경합은 운영상 감시가 필요하다.
- manifest에는 로컬 운영용 절대경로가 포함될 수 있다. 웹 API는 redaction하지만 파일시스템 접근 권한은 별도로 관리해야 한다.
- `run_history/`는 manifest/summary metadata archive이며 SHP bytes를 snapshot하지 않는다. 같은 output root 재사용 시 과거 결과 파일까지 자체 완결적으로 보존하려면 별도 immutable artifact 저장소가 필요하다.
- 실제 대용량 멀티 모델/CUDA/NAS 환경의 lock, 취소, heartbeat는 unit test만으로 충분히 검증되지 않았다.
- project-level lint/type-check 설정이 없어 가이드의 전체 quality gate를 아직 충족하지 않는다.

## 다음 권장 작업

1. 대표적인 단일 Track fixture를 선정하고 기존 commit 결과를 golden baseline으로 고정한다. feature count, class count, 좌표 허용오차, 실패 이유 분포를 비교한다.
2. `write_outputs`를 concrete application stage로 분리하고, 현재 bundle 검증에 JSON/SHP feature count 대조를 추가한다.
3. `validate` CLI를 추가해 config, 입력 목록, calibration, 모델, 출력 권한, CRS까지만 실행한다.
4. 이후 input discovery와 projection을 작은 typed contract로 분리한다. pole 알고리즘은 golden gate가 생긴 뒤 마지막에 건드린다.
5. checkpoint/idempotency와 retry executor는 stage 경계가 안정된 다음 도입한다.

현재 호출 흐름과 보호 공백은 [현재 아키텍처](current_architecture.md)에 더 자세히 정리했다.
