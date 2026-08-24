# RoadInventory P0 — Record-Preserving Object Crop Backend AI-agent 구현 명세

- 대상 저장소: `dbparkJ/RoadInventory-MMS`
- 최초 검토 기준: `main@dc82ffcbc444cdf3ff44b15be268591a85810479`
- A0 재검증 기준: `main@49f53fe29989fde42043394d06e43a69ccc8f4cb`
- A0 기준 diff: 위 두 commit 사이 object-crop 핵심 모듈과 직접 관련 테스트 변경 없음
- 권장 작업 브랜치: `feat/record-preserving-object-crops-v1`
- consumer: `geonLabs/3d_twin_ai_modeling`
- output schema: `mms_object_crop/1.0.0`
- 우선순위: P0
- 기존 QA crop 호환: MUST
- 원본 MMS 수정: 금지

---

## 1. Mission

현재 파이프라인은 영상 검출과 점군 투영을 통해 다음을 생성한다.

```text
point_crops/
pole_crops/
image_crops/
point_previews/
detected_signs.shp
pole_bottoms.shp
```

이 산출물은 운영 검수와 SHP 구축에는 유용하지만 3D 모델 학습의 권위 입력으로는 부족하다.

새 백엔드는 기존 산출물을 삭제하거나 의미를 바꾸지 않고 다음을 병렬 생성해야 한다.

```text
object_crops_v1/
```

새 산출물의 목적:

- 여러 프레임 observation을 물리 객체로 결합
- 원본 LAS record를 정확히 보존
- 판, 지주 전체 표면, 지면, 주변 context를 함께 제공
- source file hash와 source point index 제공
- object-local 좌표와 world transform 제공
- geometry family와 review reason 제공
- `3d_twin_ai_modeling`이 직접 읽을 수 있는 `sample.npz + metadata.json` 생성

---

## 2. 현재 코드의 문제를 정확히 이해할 것

### 2.1 현재 `point_crops`의 의미

`mms_shp_detection/pipeline.py`의 `POINT_CROP_SEMANTICS`는 현재 point crop을 다음으로 정의한다.

```text
derived_selected_points_visualization
XYZ만 원본 좌표로 보존
RGB는 rectified sphere image에서 재투영
source point attributes는 보존하지 않음
```

`collect_detection_points_at_range()`는 현재 `PointCloudReaderCache.read_block_points()`를 호출해 XYZ, RGB, intensity를 읽지만 실제 선택 결과에는 XYZ, pixel, distance만 유지한다. 원본 RGB와 intensity도 이후 selection 계보에서 끊어진다.

`extract_points_for_detection()`은 선택된 점의 색을 panorama crop에서 다시 가져온 뒤 `write_las()`로 저장한다.

`write_las()`는 LAS 1.4 point format 2를 새로 만들고 XYZ와 이미지 RGB만 쓴다.

결과적으로 현재 `point_crops`만으로는 다음을 복원할 수 없다.

- 원본 LAS 파일
- 원본 point record index
- 원본 RGB
- intensity
- classification
- GPS time
- return metadata
- point source ID
- 여러 observation의 membership

### 2.2 현재 `pole_crops`의 의미

`write_pole_las()`는 pole fit의 `result.point_indices`만 저장한다.

이 점들은 지주 전체 표면 crop이 아니라 다음 subset이다.

```text
derived pole-axis inliers
```

일부 LAS attribute를 새 파일에 복사하지만 다음 문제는 남는다.

- source file table 없음
- stable source point index를 output에서 권위 있게 제공하지 않음
- 전체 지주 표면이 아님
- base 주변, non-axis surface와 assembly context 손실
- panel crop과 동일한 record selection transaction이 아님

### 2.3 현재 point reader에서 재사용 가능한 것

`mms_shp_detection/pointcloud.py`의 `_read_las_records()`와 `read_block_records()`는 이미 다음을 반환한다.

```text
xyz
rgb (8-bit normalized)
intensity
classification
gps_time
gps_time_type
return_number
number_of_returns
source_index
```

여기서 `source_index`는 해당 LAS 파일의 0-based record index다. 따라서 record-preserving backend는 완전히 새로 시작할 필요가 없다.

그러나 다음은 보강해야 한다.

- source file identity/index
- raw RGB16
- point_source_id
- scan angle
- user_data
- flight-line/scan flags
- field availability
- source SHA-256 inventory

### 2.4 물리 객체 생성 Hook

현재 최종 처리 흐름은 대략 다음이다.

```text
collect_detection_records
collect_pole_records
reconcile/cluster poles
attach_support_ids_to_detection_records
deduplicate_sign_and_pole_observations
write SHP
```

학습용 object crop은 detection 안에서 즉시 만들면 안 된다. 최종 support reconciliation과 observation dedupe가 끝난 뒤 물리 assembly graph를 만든 다음 생성해야 한다.

---

## 3. Non-goals

이번 P0에서 하지 않는다.

- 기존 `point_crops` 포맷 변경
- 기존 `pole_crops` 포맷 변경
- 기존 SHP 필드 제거
- YOLO 재학습
- 3D Twin 모델 추론 통합
- Residual SDF
- 모든 도로시설물 지원
- 불확실한 ray를 위조
- 수직 datum 자동 추정
- fitted 값을 manual GT로 승격
- UI 대규모 재설계

---

## 4. Branch와 Commit 전략

```bash
git switch main
git pull --ff-only
git switch -c feat/record-preserving-object-crops-v1
```

권장 commit 단위:

```text
docs: freeze object crop v1 contract
feat: add exact source inventory and raw record fields
feat: persist observation-level source record evidence
feat: build physical assembly graph
feat: publish record-preserving object crops
test: add crop provenance and atomic publish acceptance
docs: record integration results and limitations
```

하나의 거대한 commit으로 만들지 않는다.

---

## 5. 기존 출력 호환 규칙

다음 경로와 semantics는 그대로 유지한다.

```text
point_crops/
pole_crops/
image_crops/
point_previews/
forward_views/
txt/
shp/
```

새 경로:

```text
object_crop_evidence/
object_crops_v1/
```

기존 output의 byte hash가 설정상 동일한 실행에서 바뀌지 않는 회귀 테스트를 추가한다. 새 object crop 기능을 껐을 때 기존 pipeline 결과가 달라지면 안 된다.

---

## 6. 권장 신규 모듈

`pipeline.py`에 모든 코드를 추가하지 않는다.

```text
mms_shp_detection/object_crops/
├── __init__.py
├── contracts.py
├── vocabulary.py
├── ids.py
├── source_inventory.py
├── source_records.py
├── observation_evidence.py
├── assembly_graph.py
├── geometry_router.py
├── selection.py
├── roles.py
├── canonicalization.py
├── writer.py
├── validator.py
├── publisher.py
└── manifest.py
```

### 역할

| 파일 | 책임 |
|---|---|
| `contracts.py` | dataclass/TypedDict, schema version, dtype/shape 계약 |
| `vocabulary.py` | point role, reason code, family, availability bit |
| `ids.py` | stable observation/panel/support/object/sample ID |
| `source_inventory.py` | exact Job/Track scope, source relative URI, SHA-256 |
| `source_records.py` | source record table, mask/take/concat/dedupe |
| `observation_evidence.py` | detection별 panel/pole/ground evidence 저장 |
| `assembly_graph.py` | multi-frame observation→panel→support→object graph |
| `geometry_router.py` | direct/remote/cantilever/multi/unknown routing |
| `selection.py` | full object/context source record 재선택 |
| `roles.py` | panel/pole/ground/context role assignment |
| `canonicalization.py` | object-local transform |
| `writer.py` | NPZ/JSON/optional LAZ 작성 |
| `validator.py` | source lookup, transform, graph, schema 검증 |
| `publisher.py` | staging, atomic publish, rollback |
| `manifest.py` | accepted/review/rejected JSONL과 dataset metadata |

---

## 7. `pointcloud.py` 수정 지시

### 7.1 기존 API 유지

다음은 계속 동작해야 한다.

```python
read_block_points(...)
read_block_records(...)
```

기존 반환 필드의 의미를 조용히 바꾸지 않는다.

### 7.2 Raw field 확장

`_read_las_records()`에 source dimension이 존재할 때 다음을 추가한다.

```text
rgb_raw
point_source_id
scan_angle
user_data
edge_of_flight_line
scan_direction_flag
```

RGB:

- 현재 `rgb`는 8-bit normalized view로 유지
- `rgb_raw`는 LAS의 uint16 값을 bit-exact 보존
- source에 RGB가 없으면 availability false
- 0 fill을 valid로 표시하지 않음

point source ID:

- LAS dimension `point_source_id`를 그대로 읽음
- 이것을 source record index 대신 사용하지 않음

scan angle:

- point format에 따라 `scan_angle` 또는 `scan_angle_rank`
- source semantic과 dtype을 metadata에 기록

### 7.3 Availability

각 block record에 field availability를 제공한다.

권장:

```python
records["field_availability"] = {
    "rgb_raw": True,
    "intensity": True,
    ...
}
```

per-point validity 차이가 있으면 field별 bool array를 둔다.

### 7.4 Source identity

`read_block_records()` 자체에 전역 file index를 숨겨 넣기보다 caller가 `pointcloud_file` catalog row와 함께 명시적으로 붙이는 것이 안전하다.

신규 구조 예:

```python
@dataclass(frozen=True)
class SourcePointBatch:
    records: dict[str, np.ndarray]
    source_file_uri: str
    source_file_index: int
    source_file_sha256: str
    block_start: int
```

### 7.5 Source hash 비용

수천만 점 LAS의 SHA-256을 sample마다 계산하지 않는다.

```text
run start
→ exact source inventory
→ file SHA-256 1회
→ cache
→ sample source table에서 참조
```

source size/mtime이 달라지면 cache를 무효화한다.

### 7.6 PCDB 정책

PCDB는 표준 LAS record index와 raw attributes가 부족할 수 있다.

```text
source_type=pcdb
provenance_complete=false
training=false
```

첫 실제 학습용 authoritative source는 LAS/LAZ/COPC로 제한한다. PCDB는 legacy/QA로 보존한다.

---

## 8. Exact Job/Track Source Scope

### 8.1 문제

현재 catalog가 job 기준으로 넓게 index될 수 있어 같은 job의 다른 Track이 catalog에 들어갈 수 있다. 실제 선택점이 우연히 Track01만 사용되었다고 해서 안전 계약이 완성된 것은 아니다.

### 8.2 신규 config

권장 YAML:

```yaml
object_crops:
  enabled: false
  schema_version: "1.0.0"

  source_scope:
    strict: true
    jobs:
      - job_id: "Job_..."
        tracks:
          - "Track01"

  output:
    directory_name: "object_crops_v1"
    write_audit_laz: true
    write_preview: true
```

### 8.3 정책

- index 생성 전에 source identity를 parse
- exact job과 track allowlist
- substring match 금지
- identity를 못 읽는 파일은 strict 모드에서 reject
- sidecar의 job/track과 LAS catalog가 불일치하면 publish 금지
- Track02 fixture가 섞이면 test가 실패해야 함

가능한 구현:

```python
build_pointcloud_catalog(
    ...,
    include_scope=[{"job_id": "...", "track_ids": ["Track01"]}],
)
```

기존 `include_jobs`는 호환 유지한다.

---

## 9. Observation-level Record Evidence

### 9.1 새 collector

기존 `collect_detection_points_at_range()`를 깨지 말고 신규 record-aware 함수를 추가한다.

```python
collect_detection_record_evidence_at_range(...)
```

또는 기존 함수에 내부 record path를 추가하되 기존 return contract를 유지한다.

### 9.2 핵심 규칙

`read_block_points()` 대신 `read_block_records()`를 사용한다.

모든 selection mask를 모든 per-point array에 동일하게 적용한다.

권장 helper:

```python
def take_records(batch: PointRecordBatch, mask_or_indices) -> PointRecordBatch
def concat_record_batches(batches) -> PointRecordBatch
def dedupe_source_records(batch) -> PointRecordBatch
```

금지:

```python
xyz = xyz[mask]
# 다른 raw array에 mask를 적용하지 않는 코드
```

### 9.3 보존해야 하는 selection 단계

각 point에 bit flag로 evidence를 남긴다.

```text
range valid
projection valid
inside rectified crop
inside padded mask
inside core mask
front-surface candidate
selected cluster
pole neighborhood
pole axis inlier
ground support
```

### 9.4 Cluster index 보존

현재 `cluster_extracted_points()`는 선택된 array만 반환하므로 source row identity가 사라지기 쉽다.

다음 중 하나를 구현한다.

- input row index를 함께 전달하고 반환
- `PointRecordBatch`를 직접 take
- selected index array를 반환

가장 안전한 것은 selected index를 권위 결과로 두는 것이다.

```python
{
    "selected_indices": np.ndarray[int64],
    "representative_xyz": ...
}
```

### 9.5 Evidence artifact

큰 per-point 배열을 sidecar JSON에 넣지 않는다.

```text
object_crop_evidence/
└── observations/
    └── <observation_id>.npz
```

sidecar에는 URI와 SHA-256만 기록한다.

---

## 10. Pole Evidence 수정

### 10.1 Axis inlier와 full pole을 구분

현재 `result.point_indices`는 다음으로만 사용한다.

```text
pole_axis_inlier_mask
```

이를 `pole_observed` 전체로 표시하지 않는다.

### 10.2 필요한 evidence

- pole candidate base
- axis direction
- observed z range
- radial fit quality
- axis inlier source tuples
- pole neighborhood source tuples
- ground support source tuples
- direct/remote association evidence
- horizontal connection evidence
- fit status와 reason

### 10.3 구현 방식

`extract_pole_for_detection()`의 JSON return에 대형 배열을 넣지 않는다.

별도 evidence NPZ:

```text
<observation_id>__pole_evidence.npz
```

최소 배열:

```text
neighborhood source file/index
axis_inlier mask
ground_support mask
connection_support mask
```

기존 `pole_crops`는 그대로 axis-inlier QA LAZ로 유지한다.

---

## 11. Stable Observation ID

detection 시작 시 stable ID를 만든다.

입력:

```text
run_fingerprint
job_id
track_id
image logical URI
detection index
model key
detection mapping version
```

sidecar detection payload에 추가:

```json
{
  "observation_id": "OBS_..."
}
```

기존 detection index를 object ID로 사용하지 않는다.

---

## 12. 물리 Assembly Graph

### 12.1 생성 시점

다음 이후:

```text
attach_support_ids_to_detection_records
deduplicate_sign_and_pole_observations
```

단, dedupe가 member observation을 버리면 안 된다.

### 12.2 Dedupe 보강

최종 record에 다음을 추가한다.

```text
member_observation_ids
member_detection_ids
source_sidecar_uris
```

기존 SHP writer가 모르는 추가 필드는 무시하거나 별도 graph에서 사용한다.

### 12.3 Graph

노드:

```text
observation
panel
support
assembly
```

edge:

```text
observes
same_panel
mounted_on
same_support
member_of
```

edge evidence:

- method
- distance
- confidence
- observation count
- fit status
- direct/remote
- fallback used

### 12.4 Family routing

최소:

```text
direct_single_pole_single_panel
remote_mounted_panel
cantilever
multi_panel
multi_pole
false_positive
unknown
```

최초 decoder 지원은 direct family만 true다.

---

## 13. Object-level Full Crop 생성

### 13.1 Detection mask crop을 최종 학습 crop으로 쓰지 않는다

per-detection selected front cluster는 panel evidence일 뿐이다.

최종 object crop은 source LAS에서 다시 읽는다.

### 13.2 Preliminary geometry

assembly graph에서 다음을 얻는다.

- panel evidence points
- support base
- pole axis
- observed pole z range
- ground plane/support
- panel-support connection
- multi-frame camera direction

### 13.3 Preliminary local frame

권장 schema convention:

```text
origin = pole base
+Z = ground normal 또는 stabilized pole axis의 위쪽
+Y = panel front normal의 수평 성분
+X = +Y × +Z
```

이때 `X × Y = Z`를 만족한다.

panel front 부호는 여러 camera observation 또는 road heading으로 결정한다. 모호하면 confidence를 낮추고 REVIEW한다.

현재 model decoder의 neutral orientation이 다르면 consumer adapter가 명시적 고정 transform을 적용한다. schema convention을 조용히 바꾸지 않는다.

### 13.4 Crop envelope

preliminary local frame에서 OBB를 만든다.

필수 포함:

- panel observed evidence
- pole axis/base
- pole surface 주변
- ground context
- connection region
- configurable context margin

예시 config:

```yaml
object_crops:
  selection:
    panel_margin_m: [0.5, 1.0, 0.5]
    pole_radial_margin_m: 0.5
    base_ground_radius_m: 1.5
    vertical_bottom_margin_m: 0.3
    vertical_top_margin_m: 0.5
    context_margin_m: 0.75
    max_points: 200000
```

숫자는 초기 config 값이며 실제 운영 threshold로 간주하지 않는다.

### 13.5 Source 재선택

1. OBB와 교차하는 source file/block 탐색
2. exact source record 읽기
3. OBB 내부 선택
4. `(source_file_index, source_point_index)`로 dedupe
5. source order 또는 canonical tuple order 고정
6. raw attributes와 availability 보존
7. selection hash 계산

### 13.6 Multi-observation 중복

같은 source point가 여러 frame mask에서 선택될 수 있다.

최종 point는 한 번만 저장하고 observation membership을 합친다.

```text
unique key = source file SHA + source point index
```

---

## 14. Point Role 할당

### Panel

- multi-frame mask vote
- front-surface/depth evidence
- selected cluster evidence
- panel plane/outline fit

### Pole

- pole axis 주변 radial envelope
- observed z range
- axis-inlier는 별도 mask
- entire surface 후보를 보존

### Ground

- ground plane distance
- base 주변 radius
- ground fit support

### Context

- crop envelope 내부지만 위 역할에 해당하지 않는 점

### 정책

- role은 L0 fitted evidence
- manual review 전 GT가 아님
- ambiguous point는 clutter/context
- source classification을 role로 복사하지 않음
- source classification은 별도 raw field

---

## 15. Canonicalization

### 15.1 Output

```text
points_xyz object-local float32
world_to_local float64
local_to_world float64
canonical origin/axes/method/confidence
```

### 15.2 Validation

- inverse error ≤ `1e-8`
- world round-trip ≤ `1e-5m`
- rotation determinant +1
- orthonormal
- affine last row valid
- base origin tolerance
- local Z upward
- finite values

### 15.3 Fallback

순서 예:

```text
ground plane + pole axis + panel normal
pole axis + panel normal
ground normal + panel PCA
insufficient
```

fallback method와 confidence를 기록한다. insufficient면 training false.

---

## 16. Ray 정책

P0에서는 ray를 강제로 생성하지 않는다.

point별 LiDAR origin을 복원하려면 다음이 필요하다.

- point GPS time semantic
- GPS week
- authoritative trajectory
- LiDAR lever arm/extrinsic
- time interpolation
- source point index

검증되지 않은 경우:

```json
{
  "sensor_data_availability": {
    "sensor_origins": false,
    "ray_directions": false,
    "ranges": false
  },
  "eligibility": {
    "ray_loss": false
  },
  "reason_codes": ["SENSOR_RAYS_UNAVAILABLE"]
}
```

영상 camera origin을 모든 LiDAR point origin으로 사용하지 않는다.

---

## 17. Config 변경

`config.py`와 `config.yaml`에 nested `object_crops`를 추가한다.

권장:

```yaml
object_crops:
  enabled: false
  schema_version: "1.0.0"
  fail_pipeline_on_error: false

  source_scope:
    strict: true
    jobs: []

  output:
    directory_name: "object_crops_v1"
    evidence_directory_name: "object_crop_evidence"
    write_audit_laz: true
    write_preview: true

  selection:
    point_order: "source_file_hash_then_record_index"
    panel_margin_m: [0.5, 1.0, 0.5]
    pole_radial_margin_m: 0.5
    base_ground_radius_m: 1.5
    context_margin_m: 0.75
    max_points: 200000

  canonicalization:
    convention: "panel_front_plus_y_z_up"
    min_confidence_for_model_input: 0.5
    min_confidence_for_training: 0.8

  routing:
    direct_max_panel_support_offset_m: 0.75
    keep_unsupported_samples: true

  rays:
    mode: "off"
```

기본 `enabled=false`로 기존 실행을 보존한다.

`fail_pipeline_on_error`:

- false: 기존 SHP pipeline은 성공할 수 있으나 object crop stage는 failed/review로 manifest 기록
- true: object crop publication 실패 시 전체 pipeline 실패

production training build에서는 true를 권장한다.

---

## 18. Output 구조

```text
object_crop_evidence/
├── observations/
├── poles/
└── manifest.jsonl

object_crops_v1/
├── dataset.json
├── vocabulary/
├── manifests/
├── source_inventory/
└── samples/
```

sample:

```text
sample.npz
metadata.json
audit_world.laz
preview.png
```

권위 schema는 `docs/12_CROSS_REPO_OBJECT_CROP_HANDOFF_SCHEMA_V1.md`와 동일하게 구현한다.

---

## 19. Atomic Publish

각 sample:

```text
object_crops_v1/.staging/<run>/<sample_id>/
```

순서:

1. NPZ/JSON 작성
2. schema validation
3. source point lookup
4. transform validation
5. hash 계산
6. final sample path로 atomic rename
7. manifest append 또는 atomic rebuild

dataset 전체:

- accepted/review/rejected manifest를 staging에서 생성
- 모든 참조 파일 존재 확인
- final manifest atomic replace
- 실패 시 partial final sample을 남기지 않음

Windows replace/lock 처리 패턴은 기존 manifest writer를 참고한다.

---

## 20. Run Manifest 통합

기존 `RunManifestStore`를 무리하게 변경하지 않아도 된다.

선택지:

### A. 별도 ObjectCropManifestStore

권장.

```text
object_crops_v1/run_manifest.json
```

### B. 기존 outputs에 summary 추가

```json
{
  "outputs": {
    "object_crops_manifest": "object_crops_v1/dataset.json"
  }
}
```

기존 shapefile validator가 새 manifest 구조를 잘못 검증하지 않게 분리한다.

stage:

```text
build_object_crop_source_inventory
persist_observation_evidence
build_assembly_graph
select_object_records
validate_object_crops
publish_object_crops
```

---

## 21. Sidecar Schema 변경

현재 result schema 18에 필드를 추가하면 version을 올린다.

권장 schema 19 추가 필드:

```text
observation_id
record_evidence_uri
record_evidence_sha256
record_evidence_schema
source_scope_id
```

기존 field를 삭제하거나 rename하지 않는다.

consumer adapter는 18과 19를 명시적으로 구분한다.

---

## 22. 테스트 파일

신규:

```text
tests/test_object_crop_contracts.py
tests/test_object_crop_source_inventory.py
tests/test_object_crop_record_selection.py
tests/test_object_crop_graph.py
tests/test_object_crop_canonicalization.py
tests/test_object_crop_writer.py
tests/test_object_crop_publisher.py
tests/test_object_crop_integration.py
```

기존 보강:

```text
tests/test_pointcloud.py
tests/test_pipeline_helpers.py
tests/test_execution_architecture.py
tests/test_pole.py
tests/test_config.py
```

---

## 23. 필수 Fixture

### LAS fixture

작은 LAS에 서로 다른 값을 넣는다.

- XYZ
- RGB16
- intensity
- classification
- GPS time
- return number
- number of returns
- point source ID
- scan angle
- user data

selection 후 source lookup이 bit-exact인지 검사한다.

### Cross-track fixture

```text
JobA/Track01
JobA/Track02
```

Track01 scope에서 Track02 point가 하나라도 나오면 실패.

### Multi-view fixture

동일 source point가 두 observation에 들어가도 final sample에는 한 번만 저장되고 membership은 둘 다 남아야 한다.

### Family fixture

- direct
- remote
- multi-panel
- null support
- false positive

---

## 24. Acceptance Test

### Source

- `SRC-001`: exact Job/Track
- `SRC-002`: source SHA stable
- `SRC-003`: source immutable
- `SRC-004`: sidecar/source identity consistency

### Point

- `PNT-001`: `(file,index)` XYZ round-trip 1mm
- `PNT-002`: raw attrs bit-exact
- `PNT-003`: per-point array length alignment
- `PNT-004`: deterministic point order/hash
- `PNT-005`: unavailable value not fabricated
- `PNT-006`: full pole/context preserved
- `PNT-007`: legacy crop training false

### Geometry

- `GEO-001`: transform inverse
- `GEO-002`: point round-trip
- `GEO-003`: right-handed rotation
- `GEO-004`: local convention
- `GEO-005`: vertical datum policy

### Object

- `OBJ-001`: stable ID
- `OBJ-002`: graph integrity
- `OBJ-003`: null support routing
- `OBJ-004`: multi-panel routing
- `OBJ-005`: remote/cantilever routing
- `OBJ-006`: false positive routing

### Dataset

- `DST-001`: split leakage 0
- `DST-002`: Track atomicity
- `DST-003`: manifest completeness
- `DST-004`: eligibility consistency
- `DST-005`: atomic publish
- `DST-006`: idempotent rebuild
- `DST-007`: consumer loader smoke

---

## 25. 단계별 구현

### P0-A — 계약과 source inventory

- 신규 package skeleton
- vocabulary와 contracts
- exact scope
- source SHA inventory
- raw record extension
- targeted tests

종료 조건:

```text
small LAS fixture source lookup PASS
Track02 exclusion PASS
기존 read_block APIs PASS
```

### P0-B — Observation evidence

- stable observation ID
- record-aware collection
- aligned masks
- cluster selected indices
- pole/ground evidence
- evidence NPZ
- sidecar schema update

종료 조건:

```text
source tuple 유지
raw attrs 유지
same input byte-identical evidence
```

### P0-C — Assembly graph

- member observation 보존
- panel/support/object IDs
- direct/remote/multi routing
- graph validation

종료 조건:

```text
fixture family routing PASS
graph referential integrity PASS
```

### P0-D — Object crop publish

- full envelope source re-read
- role assignment
- canonicalization
- NPZ/JSON
- optional LAZ
- atomic publish
- manifest

종료 조건:

```text
all schema/round-trip tests PASS
consumer loader smoke PASS
```

### P0-E — Real integration

- Track01 read-only integration
- Track02 exclusion evidence
- legacy/new crop comparison
- reason count
- REVIEW queue
- no automatic training approval

---

## 26. 테스트 비용 관리

AI-agent는 다음 순서를 지킨다.

### 각 작은 변경

```bash
python -m pytest tests/test_object_crop_<relevant>.py -q
```

### package milestone

```bash
python -m pytest \
  tests/test_pointcloud.py \
  tests/test_pipeline_helpers.py \
  tests/test_object_crop_*.py -q
```

### P0 완료 시 한 번

```bash
python -m pytest -q
```

실데이터 전체 pipeline을 작은 코드 수정마다 반복하지 않는다. synthetic fixture와 제한된 integration sample로 먼저 검증한다.

---

## 27. Done Definition

- [ ] 기존 QA crop 의미/결과 보존
- [ ] exact Job/Track source scope
- [ ] source SHA inventory
- [ ] source file/index per point
- [ ] raw attributes/availability
- [ ] observation evidence
- [ ] pole axis vs full pole 분리
- [ ] physical assembly graph
- [ ] geometry family routing
- [ ] full object/context crop
- [ ] object-local transform
- [ ] NPZ/JSON schema
- [ ] optional audit LAZ
- [ ] accepted/review/rejected manifest
- [ ] atomic publish
- [ ] legacy training false
- [ ] consumer loader smoke
- [ ] full test result와 남은 결손 문서화

---

## 28. AI-agent 시작 프롬프트

```text
docs/ROADINVENTORY_P0_RECORD_PRESERVING_OBJECT_CROP_AGENT_SPEC.md와
docs/MMS_DATA_SPEC.md를 먼저 읽어라.

현재 A0 재검증 기준은 main@49f53fe29989fde42043394d06e43a69ccc8f4cb이다.
최초 검토 기준 dc82ffcbc444cdf3ff44b15be268591a85810479 이후
object-crop 핵심 모듈과 직접 관련 테스트에는 변경이 없다.
새 branch feat/record-preserving-object-crops-v1에서만 작업하라.

기존 point_crops, pole_crops, SHP 출력과 의미를 바꾸지 마라.
새 object_crops_v1을 병렬 생성하라.

처음부터 pipeline.py에 대규모 코드를 넣지 말고
mms_shp_detection/object_crops package를 분리하라.

먼저 P0-A만 수행하라.

1. 현재 pointcloud.py의 read_block_records와 source_index 계약을 테스트로 고정한다.
2. exact Job/Track source scope와 source SHA inventory를 구현한다.
3. raw RGB16, point_source_id와 가능한 LAS raw field를 availability와 함께 추가한다.
4. PCDB는 provenance incomplete, training false로 처리한다.
5. 작은 LAS fixture와 Track01/Track02 fixture를 만든다.
6. 관련 targeted test만 먼저 실행한다.
7. 기존 read_block_points/read_block_records와 기존 pipeline test가 깨지지 않는지 확인한다.
8. 수행 명령, 변경 파일, 통과/실패 테스트와 다음 P0-B 위험을 보고한다.

P0-A Gate를 통과하기 전 observation evidence, graph, writer를 한꺼번에 구현하지 마라.
```
