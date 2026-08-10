# 현재 아키텍처

이 문서는 `a34f267`의 1차 구조 개선과 2026-08-10 후속 안정화를 반영한 실제 실행 경로와 데이터 흐름을 기록한다. 목표 구조를 설명하는 문서가 아니라, 지금 코드가 어디에서 상태를 바꾸고 파일을 쓰며 어떤 테스트로 보호되는지 확인하기 위한 기준선이다.

## 실행 진입점

### CLI

```text
scripts/run_pipeline.py
  -> mms_shp_detection.pipeline.main()
  -> build_arg_parser()
  -> config.parse_args_with_config()
  -> pipeline.run_pipeline()
  -> pipeline._run_pipeline_impl()
  -> 단일 모델 또는 다중 모델 실행
```

- `parse_args_with_config()`가 `config.yaml`과 CLI override를 기존 `argparse.Namespace`로 합친다. YAML 키, 타입, 범위, 상호 제약을 여기서 검증한다. 이 검증은 `run_pipeline()`과 manifest 생성보다 먼저 실행되므로 YAML/CLI parse 실패는 `validate_config` stage에 기록되지 않고 프로세스 오류로 반환된다.
- parser는 launcher가 넘긴 YAML 파일의 정확한 byte SHA-256도 계산한다. 웹이 pending manifest에 저장한 `request_file_hash`와 자식이 읽은 파일 hash가 다르면 같은 `job_id`로 실행을 이어가지 않는다. default와 CLI override를 적용한 뒤에는 별도의 canonical `effective_hash`를 commit한다.
- 공개 `run_pipeline()`은 기존 처리 함수를 감싸는 application 경계다. `PipelineConfig`와 `job_id`를 만들고, `run_manifest.json`을 생성한 뒤 단계와 오류를 기록한다.
- 기존 핵심 처리는 `_run_pipeline_impl()` 아래에 유지된다. 한 모델이면 `_run_single_model_pipeline()`, 여러 모델이면 설정에 따라 순차 실행하거나 공유 입력/프레임 생산자를 사용하는 병렬 실행으로 분기한다.
- 이전 실행의 terminal(`succeeded`, `failed`, `cancelled`) manifest와 summary가 같은 출력 루트에 있으면 하나의 file lock 아래 `run_history/`로 보관한다. 종료되지 않은 manifest가 있으면 덮어쓰지 않고 실패한다.

### 웹

```text
scripts/run_web.py
  -> webapp.create_app()
  -> FastAPI + SQLite RunStore + RunManager

POST /api/runs
  -> 입력 범위와 설정 검증
  -> <state-dir>/runs/<run_id>/config.yaml 생성
  -> output/run_manifest.json 생성(pending)
  -> SQLite queue 등록
  -> RunManager가 scripts/run_pipeline.py를 별도 프로세스로 실행
```

- 웹은 요청마다 격리된 작업 디렉터리를 만들고, `MMS_PIPELINE_JOB_ID=<run_id>`를 자식 프로세스에 전달한다. CLI와 웹 실행은 최종적으로 같은 `pipeline.run_pipeline()` 계약을 사용한다.
- `RunManager`는 한 `state-dir`에서 하나만 동작하는 로컬 큐 실행기다. Redis/SQS 같은 외부 queue나 분산 worker는 없다.
- SQLite 상태는 기존 UI 호환 상태(`queued`, `preparing`, `running`, `completed` 등)를 유지한다. 유효한 manifest가 있으면 API가 `canonical_status`, 현재 단계, 진행률, count, 버전, 구조화 오류를 추가한다.
- 자식 프로세스의 종료 코드가 0이어도 유효한 `succeeded` manifest와 완전한 현재 실행 SHP bundle이 없으면 웹 작업은 실패로 처리한다. 다중 모델 실행에서는 `models_manifest.json`의 schema version, 모든 모델의 terminal publication 상태, 모델별 SHP 선언과 root manifest 선언의 일치도 확인한다.
- 서버 재시작 시 아직 spawn되지 않은 `preparing`만 queue로 되돌린다. 이미 spawn됐을 수 있는 `starting`과 `running`/`cancelling`은 중복 실행하지 않고 terminal 상태로 조정하며, 재시작 직전에 완전한 `succeeded` manifest가 커밋됐다면 그 canonical 결과를 우선한다. DB terminal 전이 직후 manifest 동기화 전에 중단된 새 계약 run도 시작 시 다시 조정한다.
- 서버 절대경로는 공개 Run 응답과 TXT/JSON artifact에서 재귀적으로 가린다. 출력 `logs/`와 `.log` 파일은 일반 artifact endpoint로 내려받을 수 없다.

### worker와 병렬 처리

- 웹 worker는 `asyncio.create_subprocess_exec()`로 CLI 프로세스를 하나씩 실행한다.
- 파이프라인 내부에서는 설정에 따라 이미지 작업용 process worker와 다중 모델 queue를 사용한다.
- 모델 객체, logger, mutable runtime dictionary, `argparse.Namespace`, process-local point-cloud cache가 아직 기존 orchestration에 남아 있다.
- stage checkpoint를 읽어 완료 단계부터 재개하는 기능은 없다. 다만 호환되는 프레임별 JSON은 `skip_existing` fingerprint 검사로 재사용할 수 있다.

## 현재 계층 경계

```text
CLI / FastAPI / React UI
        |
        v
Application
  app.pipeline_service
  - PipelineContext
  - PipelineStage protocol
  - stage 추적, job_id, 오류 변환, manifest progress
        |
        v
Domain
  domain.models
  - JobStatus / StageResult / PipelineErrorInfo
  domain.calibration
  - Job/Track 정규화, 후보 선택, 누락/모호성 판정
        |
        v
Legacy processing modules
  pipeline / geometry / pointcloud / pole / shp_writer
        |
        v
Infrastructure
  infrastructure.manifest_writer
  - process-safe atomic manifest와 summary
  기존 LAS/PCDB/image/SHP 파일 adapter
```

이 계층은 점진적 전환의 첫 경계다. `PipelineStage` protocol은 존재하지만 모든 처리 단계가 독립 class로 옮겨진 것은 아니다. 검출·투영·지주 추정은 현재 하나의 manifest 단계인 `detect_project_and_estimate` 아래에서 기존 함수들로 실행된다.

## 데이터 흐름과 단계 계약

```text
YAML + CLI override
  -> 정규화된 유효 설정 + config hash
  -> 입력 경로 탐색
  -> Job/Track 및 이미지 task 확정
  -> 전체 calibration 사전 매칭
  -> LAS/PCDB catalog 로드 또는 생성
  -> CRS·공간 근접성·파노라마 정렬 검증
  -> 이미지 decode 및 정면/파노라마 YOLO 추론
  -> bbox/mask와 점군의 2D-3D 투영·대표점 계산
  -> 지주 후보·축·로컬 지면·하단점 추정
  -> 프레임별 JSON(TXT 확장자) atomic write
  -> 복수 관측 병합·중복 제거
  -> staged SHP bundle 검증·atomic publish
  -> manifest와 run summary 종료 기록
```

| 논리 단계 | 주요 코드 | 입력 → 출력 | 상태·파일 부작용 | 실패/재시도 | 현재 테스트 보호 |
|---|---|---|---|---|---|
| 설정 로딩·검증 | `config.py`, `pipeline.main()` | YAML/CLI → `Namespace`, `PipelineConfig`, SHA-256 | 설정 파일 읽기; 실행 전 manifest 생성 | 알 수 없는 키, 타입·범위·상호 제약 오류는 즉시 종료; 자동 재시도 없음 | `test_config.py`, `test_execution_architecture.py` |
| 입력 탐색 | `dataset.scan_image_tasks()`, `prepare_shared_pipeline_context()` | 데이터 root와 선택 filter → 정렬된 image task 목록 | 이미지/pose/납품 메타데이터 읽기 | 빈 작업 범위와 손상된 필수 입력은 모델 로딩 전에 실패 | dataset·core·pipeline helper tests |
| calibration 사전 연결 | `domain.calibration.CalibrationResolver`, `calibration.attach_calibration_metadata()` | 전체 Job/Track task + bundle/납품 메타데이터 → `CalibrationResolution`, task metadata | schema v2 calibration JSON/INI/internal orientation 읽기; manifest에 ID/hash/선택 근거 기록 | 잘못된 schema, 누락과 동일 우선순위 다중 매칭을 모델 로딩 전에 구조화해 실패; task identity 오용 거부 | `test_calibration_resolver.py`, 기존 calibration characterization tests |
| 점군 catalog | `pointcloud.build_pointcloud_catalog()` | 선택 task + LAS/PCDB root → catalog/index | cache JSON과 point-cloud metadata 읽기/쓰기 | 파일·index·CRS 불일치 시 실패; stage 재시도 없음 | `test_pointcloud.py`, pipeline helper tests |
| 입력 품질 검증 | `validate_pose_pointcloud_proximity()`, `alignment.run_panorama_alignment_qa()` | task + catalog + CRS → 검증된 공유 context | alignment QA JSON/이미지와 cache 작성 | CRS 없음/불일치, 거리·정렬 기준 위반 시 추론 전 실패 | `test_alignment.py`, core/pipeline helper tests |
| 2D 검출 | `_run_single_model_pipeline()` 또는 병렬 모델 producer | image task + YOLO model + view 설정 → bbox/mask detections | QA 이미지, 로그, progress heartbeat | worker/model 실패는 누적·전파; CUDA OOM의 제한된 직렬 fallback 외 자동 Job retry 없음 | pipeline helper tests; 실제 모델 golden fixture 없음 |
| 2D-3D 투영·대표점 | `extract_points_for_detection()`, `project_representative_point_pixel()`, `geometry.py` | detection + pose/calibration + LAS/PCDB 점 → world XYZ와 품질 근거 | point crop/preview 작성 | 지지점·cluster gate 실패를 JSON 사유로 보존; 객체별 정책과 Job 실패가 혼재 | `test_core.py`, `test_pipeline_helpers.py`, `test_pointcloud.py` |
| 지주 하단점 | `extract_pole_for_detection()`, `pole.find_pole_bases()` | 표지점 주변 점군 + pole 설정 → 축/지면/하단점 payload | pole crop/debug 작성 | 불확실하면 `REVIEW` 또는 미산출; 기존 알고리즘 정책 유지 | `test_pole.py`, `test_pole_accuracy_regressions.py` |
| 프레임 결과 | `process_image_task()` | 검출/투영/지주 payload → `txt/<record>/<frame>.txt` JSON | 임시 파일 후 replace; fingerprint가 맞으면 재사용 | 쓰기 실패는 worker/Job 오류; 호환 fingerprint 단위 재처리 가능 | pipeline helper/core tests |
| 병합·SHP 출력 | `deduplicate_sign_and_pole_observations()`, `write_shapefile()`, `publish_shapefile_bundles()` | 프레임 JSON → sign/pole `POINTZ` bundle | 임시 bundle 생성, 재개방 검증 후 lock 아래 최종 교체 | bundle 일부만 최종본으로 보이지 않게 atomic publish; 완료 단계 재시작은 없음 | `test_shp_dedupe.py`, `test_core.py`의 publish/rollback tests |
| 실행 종료 | `RunManifestStore`, `run_pipeline()` | 단계 결과·count·오류·현재 attempt 산출물 → manifest/summary | SHP component 존재 검증, lock + fsync + `os.replace` | 과거/stale SHP 귀속, 불완전 bundle, 잘못된 상태 전이/manifest schema는 성공 처리 거부 | `test_execution_architecture.py`, `test_webapp_run_safety.py` |

외부 의존성의 중심은 Ultralytics/PyTorch(CUDA 추론), NumPy/SciPy/scikit-learn 계열 계산, OpenCV/Pillow 영상 처리, laspy/pyproj 점군·CRS, pyshp SHP 입출력, FastAPI/SQLite/React 웹 계층이다. 정확한 설치 버전은 고정 requirements와 `scripts/verify_environment.py`가 관리한다.

## Job과 manifest 계약

CLI의 기본 `job_id`는 다음 구성으로 생성된다.

```text
{dataset_job}_{track}_{UTC timestamp}_{8-char hash}
```

웹 실행은 웹의 `run_<uuid>`를 그대로 사용한다. 허용 상태와 전이는 다음과 같다.

```text
pending -> validating -> running -> succeeded
   |           |           |
   +-----------+-----------+-> failed
   +-----------+-----------+-> cancelled
failed -> retrying -> running | failed | cancelled
```

`succeeded`, `failed`, `cancelled`는 일반 update에 대해 immutable이다. 실패한 실행만 명시적인 `failed -> retrying` 전이로 새 attempt를 열 수 있으며, 현재 executor는 이 전이를 자동으로 사용하지 않고 attempt 단위 재실행 UI도 아직 없다.

`run_manifest.json` schema version 1의 주요 계약은 다음과 같다.

- 실행 식별: `job_id`, `attempt`, `dataset_job`, `track`, 생성/시작/종료 시각
- provenance: 요청 YAML byte hash, canonical effective config hash/schema, Git commit, 모델 이름과 SHA-256, calibration ID/hash/선택 근거
- 입력: 입력 root, 선택된 image task 전체 count, 최대 1,000개 상대경로 sample, truncated 여부, 선택 task의 dataset fingerprint
- 실행: 고정 execution plan, 현재/완료/실패 단계, 0–100 진행률, heartbeat
- 결과: 이미지·2D 검출·3D 투영·유효/거부 feature count, 게시된 SHP와 model manifest
- 진단: 단계별 version/시간/count/metric/warning과 `PipelineErrorInfo`

manifest read-modify-write는 thread lock과 OS file lock으로 직렬화하고, 임시 파일에 `fsync`한 뒤 `os.replace()`한다. `run_summary.json`과 `run_summary.md`도 같은 방식으로 쓰며 성공과 실패 모두에서 생성을 시도한다. 현재 attempt에 running/failed stage가 남아 있으면 `succeeded` commit 자체를 거부한다. manifest가 이미 `succeeded`로 commit된 뒤 파생 summary 쓰기만 실패한 경우에는 성공 상태와 프로세스 종료 코드를 서로 모순되게 만들지 않도록 경고로 남긴다.

## 출력 위치

단일 모델 호환 모드는 기존 모델 하위 폴더 없이 쓰고, `model_dir` 모드는 모델별 하위 폴더를 유지한다. 새 실행 메타데이터는 출력 root에 추가된다.

```text
<output_dir>/
  run_manifest.json
  run_summary.json
  run_summary.md
  .run_manifest.json.lock
  run_history/*.manifest.json        # 동일 root의 이전 terminal 실행
  run_history/*.summary.{json,md}    # 해당 실행의 이전 summary
  models_manifest.json               # 다중 모델일 때
  forward_views/
  logs/
  <model_stem>/                      # model_dir 모드
    image_crops/ point_crops/ point_previews/
    pole_crops/ pole_debug/ txt/ logs/
    shp/
      detected_signs.{shp,shx,dbf,prj,cpg,qpj,wkt2}
      pole_bottoms.{shp,shx,dbf,prj,cpg,qpj,wkt2}
```

기존 SHP 필드, JSON payload, 좌표계, 파일명 규칙은 이 architecture tranche에서 변경하지 않았다. provenance는 기존 산출물 schema를 확장하는 대신 companion manifest/summary에 추가했다.

`run_history/`는 terminal manifest와 summary metadata만 옮긴다. SHP와 `models_manifest.json`을 snapshot하거나 content-addressed archive로 복제하지 않으므로, 동일 output root를 재사용하면 archived manifest의 상대 output 경로가 과거 bytes를 보존한다는 보장은 없다. 감사 가능한 산출물 이력에는 실행별 output root 또는 별도 immutable artifact 저장소가 필요하다.

## 변경 집중 영역과 위험

`git log`와 파일 크기를 `a34f267`에서 확인한 결과다. commit 수는 현재 저장소에 남아 있는 해당 파일의 history 수이므로 장기 변경량의 절대 지표가 아니라 상대적인 참고값이다.

| 파일 | 줄 수 | history commit 수 | 위험 이유 |
|---|---:|---:|---|
| `pipeline.py` | 9,855 | 8 | 설정 해석, orchestration, 추론, 투영, debug artifact, worker, 출력 조립이 함께 있어 변경 파급이 가장 크다. |
| `pole.py` | 3,003 | 5 | 지주 후보·축·연결봉·지면·품질 gate가 수치적으로 결합되어 결과 민감도가 높다. |
| `shp_writer.py` | 1,358 | 4 | deduplication과 최종 schema/publish가 함께 있으며 외부 납품 호환성에 직접 영향이 있다. |
| `calibration.py` | 465 | 3 | 납품 형식별 metadata mutation과 compatibility adapter를 담당한다. resolver 분리 후에도 legacy payload 계약을 보존해야 한다. |
| `config.py` | 574 | 5 | 모든 CLI/YAML 기본값과 범위가 모여 있어 작은 의미 변경도 전체 결과 fingerprint와 알고리즘에 영향을 준다. |

우선 경계는 파일 크기가 아니라 함께 변경되는 책임을 기준으로 잡는다. 현재 가장 안전하게 분리된 책임은 calibration resolution, manifest persistence, 현재 attempt가 선언한 SHP bundle 존재 검증이다. 다음 후보는 JSON/SHP feature count 대조와 dry-run validator, application orchestration 분리이며 projection·pole 계산은 golden dataset 없이 구조 변경하지 않는다.

## 테스트가 보호하는 것

- 설정 YAML/CLI mapping, 잘못된 키·타입·범위와 canonical config hash
- Job 상태 전이, manifest schema/atomic update, 단계 성공·실패, summary 생성
- calibration의 schema version, 대소문자와 `.job`/`.scan` 정규화, 정확한 매칭, 누락, 모호성, delivery identity/fingerprint cache, legacy task mutation 호환성
- 입력 선택, fingerprint, alignment, LAS/PCDB catalog와 점군 검색 helper
- projection/pole의 다수 순수 helper 및 과거 정확도 regression 사례
- 관측 병합과 SHP bundle의 staged publish/rollback
- 웹 queue CAS 전이, manifest 합성, 종료 코드·현재 SHP bundle 성공 조건, stale SHP 배제, 늦은 취소와 restart 동기화, 구조화 artifact의 Windows/POSIX 경로 redaction과 로그 제한
- React 작업 큐의 canonical stage/error 표시와 알 수 없는 상태 fallback

## 테스트가 아직 보호하지 못하는 것

- 작지만 실제인 MMS golden dataset의 전체 `입력 -> YOLO -> 투영 -> pole -> SHP/JSON` 결과 비교
- 변경 전후 feature 수, class 분포, 좌표 오차, invalid/uncertain 이유 분포의 자동 보고
- 실제 GPU·다중 모델·multiprocessing 장시간 실행 중 manifest heartbeat와 취소 경합
- stage checkpoint 재사용과 자동 retry executor(수동 failed→retrying attempt 초기화 계약만 존재)
- 모든 detection/projection/pole payload의 명시적 schema와 static type 검사
- JSON과 SHP feature count를 서로 대조하는 end-to-end 품질 validator(현재 bundle completeness와 publisher 재개방 검증은 존재)
- NAS 파일 잠금, 디스크 부족, 프로세스 강제 종료 같은 fault injection

## 운영상 현재 한계

- `PipelineConfig`, `PipelineContext`, `StageResult`가 추가됐지만 기존 처리 함수 대부분은 여전히 `Namespace`와 dictionary를 받는다.
- manifest 단계는 운영 단위의 coarse stage다. 검출, 투영, 지주 후보, 하단 추정, 품질 평가를 각각 독립 재실행할 수 없다.
- `RetryablePipelineError`와 `retrying` 상태는 계약만 있으며 retry 정책과 executor가 연결되지 않았다.
- calibration preflight는 정상 실행에 통합됐지만, 검출 없이 실행하는 별도 `validate` CLI는 없다.
- 웹과 CLI가 같은 child entry point를 사용하지만 공통 `JobExecutor` interface로 추출되지는 않았다.
- 재시작 시 저장된 PID가 같은 child인지 안전하게 증명할 launcher identity가 없어 이전 프로세스를 강제로 종료하지 않는다. 시작된 child가 OS에 남는 장애에서는 새 queue 작업과 GPU 자원이 겹칠 수 있으므로 운영 감시와 수동 정리가 필요하다.
- structured manifest는 있으나 구조화 로그/metrics exporter/tracing은 전면 도입되지 않았다.
- 알고리즘 method version과 `valid/uncertain/invalid` 전용 `PoleBaseEstimate` domain model은 아직 도입하지 않았다. 기존 `AUTO/REVIEW` 결과를 유지한다.
- lint, static type check, golden dataset을 묶는 repository CI quality gate가 없다.

다음 단계와 검증 결과는 [아키텍처 개선 보고서](ARCHITECTURE_IMPROVEMENT_REPORT.md)에 기록한다.
