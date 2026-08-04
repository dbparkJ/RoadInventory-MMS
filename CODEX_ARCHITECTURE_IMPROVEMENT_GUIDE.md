# RoadInventory-MMS 코드 전반 개선 가이드
## Codex 실행 지침 — 안전한 플랫폼형 파이프라인으로 전환하기

> 대상 저장소: `RoadInventory-MMS`  
> 주요 대상 모듈 예시: `pipeline.py`, `pole.py`, `calibration.py`, `config.py`, `shp_writer.py`, 테스트 코드  
> 목적: 현재 동작을 최대한 유지하면서, 대형 단일 파이프라인을 **검증 가능하고 재실행 가능하며 운영 가능한 작업 시스템**으로 점진적으로 개선한다.

---

# 0. Codex가 가장 먼저 따라야 할 지시

이 문서를 읽은 Codex는 바로 코드를 대규모로 수정하지 말고 아래 순서로 작업한다.

1. 저장소 전체 구조와 실행 진입점을 조사한다.
2. 현재 데이터 흐름과 상태 변화를 문서화한다.
3. 테스트가 보호하는 동작과 보호하지 못하는 동작을 구분한다.
4. 변경 위험이 높은 영역을 식별한다.
5. 작은 단위의 리팩터링 계획을 작성한다.
6. 각 단계마다 테스트를 먼저 보강한다.
7. 한 번에 하나의 책임만 분리한다.
8. 기존 출력 형식과 알고리즘 결과를 의도 없이 변경하지 않는다.
9. 변경 전후의 실행 결과를 비교한다.
10. 완료 후 남은 위험과 다음 작업을 문서로 남긴다.

Codex는 다음 행동을 금지한다.

- `pipeline.py` 전체를 한 번에 다시 작성하지 않는다.
- 기존 알고리즘을 이해하지 못한 상태에서 함수명과 구조만 바꾸지 않는다.
- 테스트가 없는 핵심 로직을 바로 수정하지 않는다.
- 설정 키를 임의로 삭제하거나 의미를 바꾸지 않는다.
- SHP 스키마, JSON 결과 형식, 좌표계, 파일명 규칙을 승인 없이 변경하지 않는다.
- 오류를 숨기기 위해 광범위한 `try/except Exception`을 추가하지 않는다.
- 실패한 객체를 조용히 건너뛰지 않는다.
- 성능 개선이라는 이유로 결과의 재현성을 깨뜨리지 않는다.
- 모든 기능을 클래스 하나 또는 공통 유틸리티 하나로 몰아넣지 않는다.
- 현재 동작하지 않는 코드를 추측으로 삭제하지 않는다.

---

# 1. 개선의 핵심 방향

이 프로젝트를 단순한 Python 스크립트 모음이 아니라 다음 특성을 가진 **데이터 처리 플랫폼**으로 본다.

- 입력 작업이 명확하다.
- 작업 설정이 명시적이다.
- 처리 단계가 분리되어 있다.
- 각 단계의 입력과 출력이 검증된다.
- 긴 작업을 중단하고 재시작할 수 있다.
- 실패 원인을 추적할 수 있다.
- 동일 입력과 동일 설정으로 동일 결과를 재현할 수 있다.
- 알고리즘 버전과 산출물의 관계를 추적할 수 있다.
- 새로운 검출기나 지주 추정 방법을 교체하기 쉽다.
- 일부 실패가 전체 작업을 불필요하게 망가뜨리지 않는다.
- 잘못된 변경을 이전 정상 버전으로 되돌릴 수 있다.

이를 위해 다음 다섯 가지를 우선한다.

1. **명시적인 작업 모델**
2. **단계별 파이프라인**
3. **강한 설정 검증**
4. **관측 가능성과 재현성**
5. **점진적이고 테스트 가능한 변경**

---

# 2. 먼저 현재 구조를 조사하라

Codex는 첫 수정 전에 저장소에서 다음 내용을 찾아 `docs/current_architecture.md`에 정리한다.

## 2.1 실행 진입점

다음을 조사한다.

- CLI 진입점
- 웹 실행 진입점
- worker 실행 여부
- `main()` 함수 위치
- `run_pipeline()` 호출 경로
- 설정 파일 로딩 위치
- 입력 Job/Track 검색 위치
- 출력 폴더 생성 위치
- SHP/JSON 저장 위치

## 2.2 데이터 흐름

최소한 다음 흐름을 실제 코드 기준으로 그린다.

```text
입력 경로 탐색
  → Job/Track 식별
  → 설정 로딩
  → calibration 연결
  → 이미지 및 MMS 데이터 로딩
  → 2D 검출
  → 2D-3D 투영
  → 표지 또는 시설물 대표점 계산
  → 지주 후보 추출
  → 지주 하단점 추정
  → 품질 검증
  → 중복 제거
  → SHP/JSON 작성
```

각 단계에 대해 다음을 기록한다.

- 입력 타입
- 출력 타입
- 전역 상태 사용 여부
- 파일 시스템 부작용
- 외부 라이브러리 의존성
- 실패 시 동작
- 재시도 가능 여부
- 테스트 존재 여부

## 2.3 변경 집중 영역

Git history를 사용할 수 있다면 파일별 변경 횟수와 최근 수정 빈도를 조사한다.

특히 다음 파일을 우선 점검한다.

- `pipeline.py`
- `pole.py`
- `calibration.py`
- `config.py`
- `shp_writer.py`

변경이 반복되는 큰 함수는 다음 문제 중 하나일 수 있다.

- 책임이 너무 많다.
- 알고리즘과 입출력이 결합되어 있다.
- 설정 해석이 여러 위치에 중복되어 있다.
- 실패 처리가 한곳에 몰려 있다.
- 데이터 표현이 일관되지 않다.
- 단계 간 계약이 정의되지 않았다.

Codex는 단순히 “파일이 크다”는 이유로 분리하지 말고, **함께 변경되는 이유**를 기준으로 경계를 찾는다.

---

# 3. 목표 아키텍처

최종적으로 다음과 같은 계층 구조를 지향한다.

```text
CLI / Web API
    │
    ▼
Application Layer
    ├─ 작업 생성
    ├─ 실행 계획 생성
    ├─ 단계 실행
    ├─ 상태 기록
    └─ 재시작 및 실패 처리
    │
    ▼
Domain Layer
    ├─ Calibration matching
    ├─ Detection result
    ├─ 2D-3D projection
    ├─ Pole candidate
    ├─ Pole base estimation
    ├─ Quality assessment
    └─ Feature deduplication
    │
    ▼
Infrastructure Layer
    ├─ LAS/PCDB reader
    ├─ Image reader
    ├─ Model adapter
    ├─ File state store
    ├─ SHP writer
    └─ JSON/manifest writer
```

권장 디렉터리 예시는 다음과 같다.

```text
mms_shp_detection/
├─ app/
│  ├─ pipeline_service.py
│  ├─ job_service.py
│  └─ execution_plan.py
├─ domain/
│  ├─ models.py
│  ├─ calibration.py
│  ├─ projection.py
│  ├─ pole_detection.py
│  ├─ pole_base.py
│  ├─ quality.py
│  └─ deduplication.py
├─ stages/
│  ├─ base.py
│  ├─ discover_inputs.py
│  ├─ attach_calibration.py
│  ├─ detect_objects.py
│  ├─ project_to_3d.py
│  ├─ estimate_pole_base.py
│  ├─ validate_features.py
│  └─ write_outputs.py
├─ infrastructure/
│  ├─ las_reader.py
│  ├─ image_reader.py
│  ├─ model_adapter.py
│  ├─ state_store.py
│  ├─ shp_writer.py
│  └─ manifest_writer.py
├─ config/
│  ├─ schema.py
│  ├─ loader.py
│  └─ defaults.py
├─ observability/
│  ├─ logging.py
│  ├─ metrics.py
│  └─ tracing.py
├─ cli.py
└─ pipeline.py
```

이 구조를 한 번에 만들 필요는 없다. 기존 모듈에서 안정적으로 추출 가능한 책임부터 이동한다.

---

# 4. 명시적인 작업 모델을 도입하라

현재 파이프라인 실행을 함수 호출이 아니라 하나의 **Job**으로 표현한다.

## 4.1 Job 식별자

각 실행에는 고유한 `job_id`가 있어야 한다.

권장 형식:

```text
{dataset_job}_{track}_{timestamp}_{short_hash}
```

예:

```text
Job_20250102_1434_Track01_20260804T151400_a31f92c8
```

## 4.2 Job Manifest

각 실행마다 다음 정보를 `manifest.json`으로 남긴다.

```json
{
  "schema_version": 1,
  "job_id": "Job_20250102_1434_Track01_20260804T151400_a31f92c8",
  "dataset_job": "Job_20250102_1434",
  "track": "Track01",
  "status": "running",
  "created_at": "2026-08-04T15:14:00+09:00",
  "started_at": "2026-08-04T15:14:04+09:00",
  "finished_at": null,
  "input": {
    "root": "...",
    "files": [],
    "fingerprints": {}
  },
  "versions": {
    "git_commit": "...",
    "model": "...",
    "config_hash": "...",
    "calibration_id": "...",
    "calibration_hash": "..."
  },
  "progress": {
    "current_stage": "project_to_3d",
    "completed_stages": [],
    "failed_stage": null
  },
  "counts": {
    "images": 0,
    "detections_2d": 0,
    "projected_3d": 0,
    "valid_features": 0,
    "rejected_features": 0
  },
  "outputs": {},
  "errors": []
}
```

## 4.3 상태 전이

Job 상태는 임의 문자열이 아니라 제한된 상태로 관리한다.

```text
pending
→ validating
→ running
→ succeeded

pending / validating / running
→ failed

failed
→ retrying
→ running

running
→ cancelled
```

잘못된 상태 전이를 허용하지 않는다.

예를 들어 `succeeded` 작업이 자동으로 `running`으로 되돌아가면 안 된다. 재실행은 별도의 attempt로 기록한다.

---

# 5. 파이프라인을 단계로 분리하라

각 단계는 다음 계약을 따른다.

```python
class PipelineStage(Protocol):
    name: str
    version: str

    def validate_input(self, context: PipelineContext) -> None:
        ...

    def run(self, context: PipelineContext) -> StageResult:
        ...

    def validate_output(
        self,
        context: PipelineContext,
        result: StageResult,
    ) -> None:
        ...
```

각 단계의 요구사항:

- 입력이 명확하다.
- 출력이 명확하다.
- 필요한 설정만 받는다.
- 외부 파일 접근은 명시적인 adapter를 통한다.
- 결과 요약을 반환한다.
- 실패 원인을 구조화한다.
- 재실행 가능 여부를 표시한다.
- 임시 파일과 최종 파일을 구분한다.

권장 단계:

1. `discover_inputs`
2. `validate_inputs`
3. `attach_calibration`
4. `load_or_build_spatial_index`
5. `detect_objects_2d`
6. `project_detections_to_3d`
7. `extract_pole_candidates`
8. `estimate_pole_base`
9. `validate_and_score_features`
10. `deduplicate_features`
11. `write_outputs`
12. `finalize_manifest`

## 5.1 단계 결과

```python
@dataclass(frozen=True)
class StageResult:
    stage_name: str
    stage_version: str
    status: Literal["succeeded", "failed", "skipped"]
    started_at: datetime
    finished_at: datetime
    input_count: int
    output_count: int
    rejected_count: int
    artifacts: tuple[ArtifactRef, ...]
    metrics: Mapping[str, float | int | str]
    warnings: tuple[PipelineWarning, ...]
```

결과 객체는 가능한 한 immutable하게 유지한다.

---

# 6. 설정을 코드의 중심 계약으로 관리하라

영상에서 Control Plane이 템플릿과 Context를 분리했듯, 이 프로젝트도 **알고리즘 코드와 실행 설정을 분리**한다.

## 6.1 강한 설정 모델

가능하면 Pydantic 또는 dataclass 기반의 명시적인 설정 모델을 사용한다.

```python
class PoleBaseConfig(BaseModel):
    min_point_count: int = Field(ge=3)
    depth_window_m: float = Field(gt=0)
    max_distance_m: float = Field(gt=0)
    ground_search_radius_m: float = Field(gt=0)
    cluster_eps_m: float = Field(gt=0)
    cluster_min_samples: int = Field(ge=1)
```

모든 설정 키에는 다음이 필요하다.

- 타입
- 기본값
- 허용 범위
- 단위
- 설명
- 버전
- 폐기 예정 여부

## 6.2 설정 정규화

설정은 실행 초기에 한 번만 해석한다.

금지할 패턴:

```python
value = config.get("pole", {}).get("depth_window", 1.5)
```

이런 접근이 여러 파일에 흩어지면 기본값과 의미가 달라질 수 있다.

권장:

```python
config = PipelineConfig.load(path)
depth_window = config.pole_base.depth_window_m
```

## 6.3 설정 Hash

정규화된 설정을 canonical JSON으로 변환한 후 hash를 만든다.

```text
config_hash = SHA256(canonical_config_json)
```

산출물마다 `config_hash`를 기록한다.

## 6.4 설정 버전

```yaml
schema_version: 2
pipeline:
  ...
```

설정 구조를 변경할 때 migration 함수를 둔다.

```python
def migrate_config(raw: dict) -> dict:
    if raw["schema_version"] == 1:
        raw = migrate_v1_to_v2(raw)
    return raw
```

Codex는 기존 설정을 깨뜨리는 변경을 할 때 반드시 migration 또는 명확한 오류를 제공한다.

---

# 7. Calibration 매칭을 독립된 도메인으로 분리하라

현재 발생했던 다음 오류 유형은 파이프라인 후반이 아니라 실행 전 검증 단계에서 차단해야 한다.

```text
No matching calibration for:
Job_20250102_1434/Track01 ...
```

## 7.1 Calibration Resolver 책임

`CalibrationResolver`는 다음만 담당한다.

- Job/Track 식별자 정규화
- calibration 후보 검색
- 우선순위 규칙 적용
- 다중 매칭 감지
- 미매칭 감지
- 선택 근거 반환

```python
@dataclass(frozen=True)
class CalibrationMatch:
    job_name: str
    track_name: str
    calibration_id: str
    source_path: Path
    matched_by: str
    fingerprint: str
```

## 7.2 매칭 결과를 구조화하라

단순히 calibration 객체만 반환하지 말고 선택 근거를 기록한다.

예:

```json
{
  "job": "Job_20250102_1434",
  "track": "Track01",
  "calibration_id": "CAL_20250102_A",
  "matched_by": "exact_job_track",
  "candidate_count": 1
}
```

## 7.3 Fail Fast

다음 조건에서는 실제 모델 추론 전에 실패한다.

- calibration 없음
- calibration 여러 개가 동일 우선순위로 매칭
- 필수 카메라 파라미터 없음
- 좌표계 정의 없음
- calibration 버전 미지원
- 이미지 센서와 calibration 센서 불일치

## 7.4 사전 점검 명령

Codex는 다음과 같은 dry-run 명령을 추가하는 방향을 검토한다.

```bash
python -m mms_shp_detection.cli validate \
  --config config.yaml \
  --input-root D:\MMS
```

이 명령은 실제 검출 없이 다음만 검사한다.

- 입력 Job/Track 목록
- calibration 매칭
- 필수 파일 존재 여부
- 모델 파일 존재 여부
- 출력 경로 쓰기 권한
- 설정 유효성
- 좌표계 및 class 설정

---

# 8. 도메인 모델을 dict 대신 명시적으로 정의하라

핵심 데이터가 dict와 tuple로 전달되면 필드 누락과 좌표 혼동이 발생하기 쉽다.

최소한 다음 객체를 타입으로 정의한다.

```python
@dataclass(frozen=True)
class ImageDetection:
    detection_id: str
    image_id: str
    class_id: int
    class_name: str
    confidence: float
    bbox_xyxy: BBox
    mask: MaskRef | None
    camera_id: str
    timestamp: datetime | None
```

```python
@dataclass(frozen=True)
class ProjectedDetection:
    detection: ImageDetection
    xyz_world: Point3D
    support_point_count: int
    depth_median_m: float
    depth_spread_m: float
    cluster_id: str | None
    quality: ProjectionQuality
```

```python
@dataclass(frozen=True)
class PoleCandidate:
    candidate_id: str
    source_detection_id: str
    points: PointCloudRef
    axis: Vector3D | None
    centerline: Polyline3D | None
    base_point: Point3D | None
    quality: PoleQuality
```

좌표는 이름으로 구분한다.

- `point_camera`
- `point_vehicle`
- `point_world`
- `point_map`

금지:

```python
x, y, z = point
```

문맥이 불명확한 좌표 tuple이 여러 프레임을 오가게 하지 않는다.

---

# 9. 알고리즘과 입출력을 분리하라

다음과 같은 순수 계산 함수는 파일 시스템, 로그, 설정 로딩에서 분리한다.

```python
def estimate_front_surface_anchor(
    depths: NDArray[np.float64],
    *,
    lower_quantile: float,
    min_support_points: int,
) -> float:
    ...
```

```python
def choose_nearest_valid_cluster(
    clusters: Sequence[PointCluster],
    *,
    camera_origin: Point3D,
    max_distance_m: float,
) -> PointCluster | None:
    ...
```

```python
def estimate_pole_base(
    pole_points: NDArray[np.float64],
    ground_points: NDArray[np.float64],
    config: PoleBaseConfig,
) -> PoleBaseEstimate:
    ...
```

순수 함수의 장점:

- 작은 테스트가 가능하다.
- 입력이 같으면 출력이 같다.
- 파일 없이 테스트할 수 있다.
- 알고리즘 변경 전후 비교가 쉽다.
- 병렬화하기 쉽다.
- 실패 조건을 명확히 정의할 수 있다.

---

# 10. 지주 하단점 추정은 전략 패턴으로 분리하라

지주 대표점 알고리즘은 하나의 거대한 함수가 아니라 교체 가능한 전략으로 만든다.

```python
class PoleBaseEstimator(Protocol):
    name: str
    version: str

    def estimate(
        self,
        candidate: PoleCandidate,
        ground: GroundContext,
        config: PoleBaseConfig,
    ) -> PoleBaseEstimate:
        ...
```

초기 전략 예시:

- `LowestRobustPercentileEstimator`
- `AxisGroundIntersectionEstimator`
- `RansacCylinderGroundEstimator`
- `CenterlineExtrapolationEstimator`
- `HybridPoleBaseEstimator`

## 10.1 결과에는 점뿐 아니라 근거를 포함하라

```python
@dataclass(frozen=True)
class PoleBaseEstimate:
    point_world: Point3D | None
    status: Literal["valid", "invalid", "uncertain"]
    method: str
    method_version: str
    support_point_count: int
    pole_height_m: float | None
    ground_height_m: float | None
    axis_tilt_deg: float | None
    residual_m: float | None
    confidence: float
    rejection_reasons: tuple[str, ...]
```

이렇게 해야 SHP에 점만 남기지 않고 품질을 추적할 수 있다.

## 10.2 Fail Closed

불확실한 경우 임의의 점을 만들지 않는다.

잘못된 점 하나가 도로대장 품질에 영향을 주므로 다음과 같이 분류한다.

- `valid`: 자동 산출 가능
- `uncertain`: 검수 대상
- `invalid`: 결과에서 제외하되 이유 기록

---

# 11. 비동기 실행 구조를 준비하라

현재 CLI 중심이라도 장기적으로 다음 구조를 지원할 수 있도록 Application Layer를 분리한다.

```text
CLI / Web
  → Job 생성
  → Queue 또는 로컬 실행기
  → Worker
  → Stage 실행
  → State Store
  → Output Store
```

초기에는 실제 Redis/SQS를 바로 도입하지 않아도 된다.

먼저 인터페이스를 만든다.

```python
class JobExecutor(Protocol):
    def submit(self, request: JobRequest) -> JobId:
        ...

    def run(self, job_id: JobId) -> JobResult:
        ...
```

구현:

- `InlineJobExecutor`: 현재 프로세스에서 실행
- `LocalProcessJobExecutor`: 별도 프로세스
- 향후 `QueueJobExecutor`

이렇게 하면 현재 CLI를 유지하면서 웹 worker 구조로 확장할 수 있다.

---

# 12. 재시도와 멱등성을 설계하라

긴 MMS 처리는 중간 실패가 발생할 수 있다.

## 12.1 멱등성 키

단계 산출물은 다음 조합으로 식별한다.

```text
input_fingerprint
+ config_hash
+ calibration_hash
+ model_version
+ stage_name
+ stage_version
```

동일 키의 정상 산출물이 있으면 재사용할 수 있다.

## 12.2 Atomic Write

최종 파일에 바로 쓰지 않는다.

```text
output.shp.tmp
→ 검증
→ rename
→ output.shp
```

여러 파일로 구성된 SHP는 임시 디렉터리에 작성한 뒤 전체 검증 후 최종 위치로 이동한다.

## 12.3 Checkpoint

단계별 완료 상태를 저장한다.

```text
state/
├─ 01_discover_inputs.json
├─ 02_attach_calibration.json
├─ 03_detect_objects.json
├─ 04_project_to_3d.json
└─ ...
```

재시작 시 완료된 정상 단계를 건너뛸 수 있다.

## 12.4 재시도 분류

모든 오류를 재시도하면 안 된다.

재시도 가능:

- 일시적인 파일 잠금
- GPU 메모리 일시 부족 후 batch 축소 가능
- 네트워크 저장소 일시 오류
- worker 비정상 종료

재시도 불가:

- calibration 없음
- 설정 값 범위 오류
- 지원하지 않는 좌표계
- 손상된 필수 입력
- 모델 클래스와 설정 클래스 불일치

```python
class RetryablePipelineError(PipelineError):
    pass

class NonRetryablePipelineError(PipelineError):
    pass
```

---

# 13. 구조화된 오류 모델을 도입하라

문자열 예외만 던지지 않는다.

```python
@dataclass(frozen=True)
class PipelineErrorInfo:
    code: str
    message: str
    stage: str
    job_id: str
    retryable: bool
    object_id: str | None
    context: Mapping[str, Any]
    cause_type: str | None
```

오류 코드 예시:

```text
CONFIG_INVALID
INPUT_FILE_MISSING
CALIBRATION_NOT_FOUND
CALIBRATION_AMBIGUOUS
MODEL_LOAD_FAILED
PROJECTION_NO_SUPPORT_POINTS
PROJECTION_NO_VALID_CLUSTER
POLE_BASE_INSUFFICIENT_POINTS
POLE_BASE_GROUND_NOT_FOUND
OUTPUT_SCHEMA_MISMATCH
OUTPUT_WRITE_FAILED
```

오류 메시지에는 해결 가능한 정보를 넣는다.

나쁜 예:

```text
No matching calibration
```

좋은 예:

```text
CALIBRATION_NOT_FOUND:
job=Job_20250102_1434
track=Track01
searched_roots=[...]
normalized_key=job_20250102_1434/track01
available_keys_sample=[...]
```

단, 민감하거나 지나치게 큰 정보를 로그에 남기지 않는다.

---

# 14. 관측 가능성을 기본 기능으로 만들라

## 14.1 구조화 로그

사람이 읽는 메시지와 기계가 읽는 필드를 함께 남긴다.

```python
logger.info(
    "3D projection completed",
    extra={
        "event": "projection_completed",
        "job_id": job_id,
        "track": track_name,
        "stage": "project_to_3d",
        "input_detections": len(detections),
        "valid_projections": len(valid),
        "rejected_projections": len(rejected),
        "elapsed_ms": elapsed_ms,
    },
)
```

필수 공통 필드:

- `timestamp`
- `level`
- `event`
- `job_id`
- `dataset_job`
- `track`
- `stage`
- `attempt`
- `object_id`
- `elapsed_ms`
- `error_code`

## 14.2 메트릭

최소한 다음을 집계한다.

### 처리량

- 이미지 수
- 2D 검출 수
- 3D 투영 성공 수
- 지주 후보 수
- 하단점 성공 수
- SHP feature 수

### 품질

- 평균 support point 수
- projection 실패율
- ground 미탐색률
- uncertain 비율
- 중복 제거율
- class별 유효율

### 성능

- 단계별 실행 시간
- 이미지당 추론 시간
- detection당 투영 시간
- LAS block 조회 시간
- GPU 최대 메모리
- 프로세스 최대 메모리

## 14.3 실행 요약

작업 완료 후 `run_summary.json`과 사람이 읽을 수 있는 `run_summary.md`를 생성한다.

---

# 15. 산출물에 Provenance를 남겨라

모든 결과는 어떤 조건에서 생성됐는지 추적 가능해야 한다.

SHP 속성 또는 companion JSON에 다음 정보를 기록한다.

- `job_id`
- `dataset_job`
- `track`
- `source_image`
- `source_detection_id`
- `class_id`
- `confidence_2d`
- `projection_quality`
- `pole_method`
- `pole_confidence`
- `support_points`
- `config_hash`
- `model_version`
- `calibration_id`
- `code_commit`
- `created_at`

SHP 필드 길이 제한 때문에 축약이 필요하면 전체 정보는 companion JSON 또는 GeoPackage에 보존한다.

Codex는 기존 SHP 스키마를 바로 변경하지 말고 다음 중 하나를 선택한다.

1. 기존 스키마 유지 + companion metadata 생성
2. 설정으로 확장 스키마 활성화
3. 새 schema version으로 명시적 전환

---

# 16. 출력 검증을 독립 단계로 둬라

파일 저장 성공이 작업 성공을 의미하지 않는다.

출력 검증 항목:

- SHP 구성 파일 존재 여부
- feature count 일치
- geometry type 확인
- 좌표계 존재 여부
- NaN/Inf 좌표 없음
- 허용 범위 밖 좌표 없음
- 필수 속성 누락 없음
- 동일 ID 중복 없음
- JSON과 SHP feature 수 일치
- 임시 파일 잔존 없음

가능하면 결과를 다시 읽어 검증한다.

---

# 17. 테스트 전략

리팩터링 전에 현재 동작을 보호하는 characterization test를 작성한다.

## 17.1 테스트 계층

### Unit Test

대상:

- 설정 검증
- Job/Track 정규화
- calibration 매칭
- depth anchor
- clustering 선택
- pole axis 계산
- ground 교차점
- 품질 점수
- 중복 제거

### Contract Test

대상:

- Stage 입력/출력
- 모델 adapter
- LAS reader
- SHP writer
- State Store

### Integration Test

작은 fixture 데이터로 다음 흐름을 실행한다.

```text
입력 검색
→ calibration
→ 검출 fixture
→ 3D 투영
→ 지주 하단점
→ SHP/JSON
```

### Regression Test

기존 검증 데이터셋의 결과를 baseline으로 저장한다.

단순히 파일 hash만 비교하지 말고 다음을 비교한다.

- feature count
- class별 count
- 좌표 차이
- confidence 차이
- invalid 이유 분포
- 실행 시간

## 17.2 허용 오차

부동소수점 및 병렬 처리 때문에 좌표를 exact equality로 비교하지 않는다.

예:

```python
assert distance(actual, expected) <= 0.03
```

허용 오차는 데이터와 업무 기준에 맞게 설정하고 이유를 문서화한다.

## 17.3 Golden Dataset

작지만 대표적인 검증 세트를 만든다.

포함할 사례:

- 정상 표지와 단일 지주
- 표지 2개가 한 지주를 공유
- 식생이 지주 주변에 존재
- 지주 하단이 가림
- 경사면
- 지면 class 없음
- classification이 전부 0
- 잘못된 calibration
- support point 부족
- 서로 가까운 지주 2개
- 이미지 경계의 검출
- 멀리 있는 검출
- depth outlier 존재

---

# 18. 성능 개선 원칙

Codex는 profiling 없이 성능 최적화를 시작하지 않는다.

먼저 단계별 시간을 측정한다.

우선 최적화 후보:

- LAS/PCDB spatial block 반복 로딩
- 동일 이미지 또는 calibration 반복 파싱
- 동일 좌표 변환 반복 계산
- Python loop 기반 point filtering
- 불필요한 array copy
- 전체 point cloud를 매 detection마다 순회
- GPU 모델 반복 초기화
- SHP feature별 잦은 디스크 flush

## 18.1 Cache 키

Cache는 반드시 버전 정보를 포함한다.

```text
cache_key =
input_fingerprint
+ calibration_hash
+ config_subset_hash
+ algorithm_version
```

설정이 바뀌었는데 과거 cache를 재사용하면 안 된다.

## 18.2 병렬화

병렬화 후보:

- 이미지 단위 2D 검출
- 독립 detection의 3D 투영
- Track 단위 처리

주의:

- GPU 모델을 프로세스마다 중복 로딩하지 않는다.
- 동일 출력 파일에 여러 worker가 동시에 쓰지 않는다.
- 재현성을 위해 입력 정렬을 고정한다.
- 결과 병합 순서를 명시적으로 정한다.
- multiprocessing과 CUDA 초기화 방식을 테스트한다.

---

# 19. 안전한 배포와 롤백

영상의 Control Plane처럼 잘못된 설정이나 알고리즘이 전체 결과에 확산되지 않도록 한다.

## 19.1 단계적 적용

새 알고리즘은 다음 순서로 검증한다.

```text
Unit Test
→ Golden Dataset
→ 단일 Track
→ 소수 Job
→ 기존 결과와 Shadow 비교
→ 전체 적용
```

## 19.2 Shadow Mode

새 알고리즘 결과를 공식 산출물로 사용하지 않고 기존 알고리즘과 동시에 실행한다.

비교 항목:

- 검출 수
- 유효 점 수
- 평균 위치 차이
- 실패 이유
- 실행 시간
- 메모리 사용량

## 19.3 Last Known Good

다음을 보존한다.

- 안정 버전 Git commit
- 설정 파일
- 모델 버전
- calibration 버전
- baseline 결과
- 실행 명령

새 버전 품질이 기준 이하이면 즉시 이전 조합으로 재실행할 수 있어야 한다.

---

# 20. 품질 게이트

Codex는 다음 조건을 충족하지 못한 변경을 완료로 처리하지 않는다.

## 필수 게이트

- 기존 테스트 통과
- 신규 테스트 추가
- 타입 검사 통과
- lint 통과
- 핵심 fixture 실행 성공
- 결과 스키마 검증 성공
- 실행 manifest 생성
- 변경 전후 결과 비교 보고
- 문서 업데이트

## 알고리즘 변경 추가 게이트

- Golden Dataset 정확도 저하 없음
- 위치 오차 기준 충족
- invalid/uncertain 증가 원인 설명
- 성능 저하가 허용 범위 이내
- 새로운 설정 값 문서화
- 알고리즘 버전 증가
- rollback 방법 존재

---

# 21. 우선순위별 개선 로드맵

## Phase 1 — 현재 동작 보호

목표: 리팩터링 전에 깨지는 것을 감지할 수 있게 한다.

작업:

- 실행 진입점 문서화
- 핵심 파이프라인 호출 그래프 작성
- characterization test 추가
- 작은 Golden Dataset 정의
- 기존 SHP/JSON 결과 비교 도구 작성
- 구조화된 오류 코드 기초 도입
- config schema validation 강화

완료 조건:

- 현재 대표 데이터셋을 자동 실행할 수 있다.
- 변경 전후 결과 차이를 수치로 볼 수 있다.
- calibration 미매칭이 실행 초기에 탐지된다.

## Phase 2 — 상태와 실행 추적

목표: 각 실행이 재현 가능해야 한다.

작업:

- `job_id`
- `manifest.json`
- config hash
- code commit
- model/calibration version 기록
- 단계별 타이밍과 count 기록
- 임시 출력과 atomic write

완료 조건:

- 산출물만 보고 생성 조건을 추적할 수 있다.
- 중간 실패 위치를 확인할 수 있다.

## Phase 3 — Pipeline Stage 분리

목표: `pipeline.py`의 orchestration과 알고리즘을 분리한다.

순서 권장:

1. 출력 작성 분리
2. calibration resolution 분리
3. config loading 분리
4. input discovery 분리
5. projection 순수 로직 분리
6. pole base estimator 분리
7. deduplication 분리

완료 조건:

- `pipeline.py`는 단계 조립과 실행 순서 위주가 된다.
- 핵심 알고리즘을 파일 없이 단위 테스트할 수 있다.

## Phase 4 — 재시작과 캐시

목표: 긴 작업의 실패 비용을 줄인다.

작업:

- stage checkpoint
- artifact fingerprint
- 재실행 시 정상 단계 재사용
- retryable/non-retryable 오류 분리
- stale cache 검증

완료 조건:

- 출력 단계 실패 후 전체 추론을 다시 하지 않는다.
- 설정 변경 시 잘못된 cache를 사용하지 않는다.

## Phase 5 — Worker 구조

목표: CLI와 웹 실행을 동일 application service로 통합한다.

작업:

- `JobExecutor` 인터페이스
- `InlineJobExecutor`
- `LocalProcessJobExecutor`
- 상태 조회 API
- 취소 처리
- worker heartbeat

완료 조건:

- CLI와 웹이 동일한 pipeline service를 호출한다.
- 긴 작업이 웹 요청 수명과 분리된다.

## Phase 6 — 운영 품질

목표: 여러 Job을 안정적으로 처리한다.

작업:

- 메트릭
- 실패율 대시보드용 summary
- 작업별 리소스 사용량
- shadow mode
- canary rollout
- 자동 품질 게이트
- 장기 저장 정책

---

# 22. Codex 작업 방식

각 개선 작업에서 Codex는 다음 형식으로 먼저 계획을 작성한다.

```markdown
## 변경 목적

## 현재 코드에서 확인한 사실

## 변경하지 않을 동작

## 수정 대상 파일

## 새로 추가할 타입 또는 인터페이스

## 테스트 계획

## 호환성 위험

## 롤백 방법
```

수정 후 다음 형식으로 결과를 남긴다.

```markdown
## 수행한 변경

## 추가한 테스트

## 변경 전후 결과

## 성능 영향

## 남은 위험

## 다음 권장 작업
```

---

# 23. Codex용 Master Prompt

아래 문장을 Codex 작업 시작 시 그대로 사용할 수 있다.

```text
이 저장소는 MMS 이미지와 포인트클라우드 데이터를 처리하여 도로 시설물과 지주 하단점을 SHP/JSON으로 생성하는 파이프라인이다.

먼저 CODEX_ARCHITECTURE_IMPROVEMENT_GUIDE.md를 전부 읽어라.

이번 작업의 최우선 원칙은 현재 알고리즘 결과와 외부 출력 호환성을 보호하면서 코드의 검증 가능성, 재현성, 실패 추적성, 단계별 재실행 가능성을 높이는 것이다.

바로 대규모 리팩터링하지 마라. 먼저 저장소 구조, 실행 진입점, 설정 로딩, calibration 연결, 데이터 흐름, 출력 생성, 테스트 현황을 조사하라.

그 다음 아래 결과를 먼저 제시하라.

1. 현재 파이프라인 호출 흐름
2. 변경 위험이 높은 함수와 이유
3. 테스트로 보호되지 않는 핵심 동작
4. 가장 작은 첫 번째 개선 단위
5. 해당 변경의 테스트 계획과 롤백 방법

코드를 수정할 때 다음 규칙을 지켜라.

- 한 번에 하나의 책임만 분리한다.
- 기존 public 함수와 CLI 호환성을 가능한 한 유지한다.
- 알고리즘과 파일 입출력을 분리한다.
- 핵심 dict와 tuple을 명시적 dataclass 또는 typed model로 바꾼다.
- 설정은 한 번만 정규화하고 강하게 검증한다.
- calibration 오류는 실제 처리 전에 fail fast한다.
- 오류를 구조화하고 retryable 여부를 구분한다.
- 실행마다 job_id, config hash, code version, model version, calibration version을 기록한다.
- 단계별 입력, 출력, 처리 시간, 성공/실패 개수를 기록한다.
- 최종 출력은 atomic write 후 다시 읽어 검증한다.
- 기존 결과와 신규 결과를 fixture와 Golden Dataset으로 비교한다.
- 테스트 없이 핵심 알고리즘을 변경하지 않는다.
- 불확실한 지주 하단점은 임의로 확정하지 말고 uncertain 또는 invalid로 기록한다.
- 성능 최적화 전 profiling 결과를 제시한다.
- 완료 후 변경 전후 차이, 남은 위험, 다음 작업을 문서화한다.

첫 작업은 저장소 분석과 보호 테스트 추가에 집중하라. pipeline.py 전체 재작성은 금지한다.
```

---

# 24. 첫 번째 실제 작업으로 권장하는 범위

처음부터 전체 구조를 바꾸지 말고 다음 작업부터 시작한다.

## 작업명

`Calibration 사전 검증과 실행 Manifest 도입`

## 이유

- 현재 확인된 실제 실패 사례와 직접 연결된다.
- 알고리즘 결과를 바꾸지 않는다.
- 파이프라인 전체의 실행 추적 기반이 된다.
- 이후 단계 분리를 위한 Job Context를 만들 수 있다.

## 구현 범위

1. `PipelineConfig` 검증
2. 입력 Job/Track 목록 확정
3. 모든 Job/Track에 calibration이 존재하는지 사전 검사
4. 다중 매칭 검사
5. 실행 전 `manifest.json` 생성
6. 실패 시 구조화된 오류 기록
7. 성공 시 사용한 calibration ID와 hash 기록
8. 기존 `attach_calibration_metadata()` 동작을 characterization test로 보호

## 완료 기준

- calibration이 없으면 모델 로딩 전에 종료된다.
- 누락된 모든 Job/Track을 한 번에 보여준다.
- 검색한 calibration root와 정규화 key가 오류에 포함된다.
- 정확히 하나가 매칭된 경우 선택 근거가 manifest에 기록된다.
- 기존 정상 데이터의 최종 SHP 결과가 변경되지 않는다.

---

# 25. 두 번째 실제 작업으로 권장하는 범위

## 작업명

`Pipeline Context와 Stage Result 도입`

## 구현 범위

기존 함수 인자를 한 번에 전부 변경하지 말고 다음 객체를 추가한다.

```python
@dataclass
class PipelineContext:
    job_id: str
    config: PipelineConfig
    input_root: Path
    output_root: Path
    dataset_job: str
    track: str
    calibration: CalibrationMatch
    manifest: RunManifest
```

초기에는 기존 함수에 필요한 값만 꺼내 전달한다.

```python
result = existing_projection_function(
    config=context.config,
    calibration=context.calibration,
    ...
)
```

그 후 단계적으로 함수가 Context 전체가 아닌 필요한 명시적 타입만 받도록 정리한다.

---

# 26. 세 번째 실제 작업으로 권장하는 범위

## 작업명

`지주 하단점 추정 결과 구조화`

현재 XYZ 또는 `None`만 반환한다면 `PoleBaseEstimate`를 도입한다.

반드시 기록할 항목:

- 결과 점
- 사용한 방법
- 알고리즘 버전
- support point 수
- ground 탐색 성공 여부
- 축 기울기
- residual
- confidence
- invalid 이유

이 변경은 기존 SHP 출력에는 영향을 주지 않도록 adapter를 둔다.

```python
if estimate.status == "valid":
    legacy_point = estimate.point_world
else:
    legacy_point = None
```

그 후 별도 JSON에 상세 품질을 저장한다.

---

# 27. 코드 리뷰 체크리스트

Codex가 PR 또는 변경 검토 시 다음을 확인한다.

## 구조

- 함수가 하나의 책임을 가지는가?
- orchestration과 계산이 분리됐는가?
- 파일 시스템 접근이 도메인 로직에 섞이지 않았는가?
- 전역 설정이나 숨은 상태가 추가되지 않았는가?
- 이름이 좌표계와 단위를 드러내는가?

## 안정성

- 입력이 검증되는가?
- 실패를 조용히 무시하지 않는가?
- partial output이 최종 결과로 오인되지 않는가?
- 재실행 시 중복 산출물이 생기지 않는가?
- 동일 입력으로 결과를 재현할 수 있는가?

## 품질

- invalid 이유가 기록되는가?
- confidence의 의미가 문서화됐는가?
- 좌표 오차 기준이 있는가?
- ground/classification이 없는 경우 fallback이 명확한가?
- fallback 결과가 정상 결과와 구분되는가?

## 테스트

- 정상 사례가 있는가?
- 경계값 테스트가 있는가?
- 실패 사례가 있는가?
- 과거 버그를 재현하는 테스트가 있는가?
- 출력 schema 테스트가 있는가?
- 테스트가 구현 세부사항보다 외부 동작을 검증하는가?

## 운영

- 로그에 `job_id`와 `stage`가 있는가?
- 실행 시간과 count가 기록되는가?
- 설정 및 코드 버전이 결과에 남는가?
- 롤백 가능한가?
- 변경이 전체 Job에 바로 적용되지 않도록 검증 방법이 있는가?

---

# 28. 최종 목표

이 개선의 최종 목적은 단순히 파일을 작게 나누는 것이 아니다.

좋은 결과는 다음과 같다.

```text
현재 무엇을 처리 중인지 알 수 있다.
왜 실패했는지 알 수 있다.
어떤 설정과 코드로 결과가 생성됐는지 알 수 있다.
실패한 단계부터 다시 실행할 수 있다.
새 알고리즘을 기존 알고리즘과 안전하게 비교할 수 있다.
작성자가 없어도 다른 개발자가 운영할 수 있다.
```

Codex는 “코드가 더 세련돼 보이는가”가 아니라 아래 기준으로 개선을 판단한다.

- 변경이 안전해졌는가?
- 실패가 이해 가능해졌는가?
- 실행이 재현 가능해졌는가?
- 테스트가 핵심 동작을 보호하는가?
- 일부 기능을 독립적으로 교체할 수 있는가?
- 운영자가 결과의 근거를 추적할 수 있는가?
- 다음 개발자가 시스템 전체를 다시 읽지 않고도 수정할 수 있는가?

이 기준을 충족하는 방향으로만 점진적으로 수정한다.
