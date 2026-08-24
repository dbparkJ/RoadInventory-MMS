# Record-Preserving Object Crop 구현 인벤토리

## 0. A0 결론과 조사 기준

이 문서는 `docs/CODEX_EXECUTION_PROMPTS.md`의 A0 결과다. 구현은 시작하지 않았다.

- 원 명세 기준: `main@dc82ffcbc444cdf3ff44b15be268591a85810479`
- 현재 조사 기준: `main@49f53fe29989fde42043394d06e43a69ccc8f4cb`
- 기준 이후 변경: 커밋 1개, `feat: add missed-detection review workspace`
- 비교 결과: A0가 지정한 핵심 모듈과 직접 관련 테스트는 원 명세 기준과 현재 `HEAD` 사이에 변경이 없다.
- 작업트리 상태: `main`, dirty entry 85개. 대부분 기존 web/webui 및 관련 테스트 작업이며 사용자 변경으로 취급한다.
- 브랜치 상태: 요구 브랜치 `feat/record-preserving-object-crops-v1`가 아니다.
- 권위 handoff 문서: 명세가 참조하는 `docs/12_CROSS_REPO_OBJECT_CROP_HANDOFF_SCHEMA_V1.md`는 현재 저장소에 없다.

따라서 이 A0 진단은 현재 핵심 코드에도 유효하다. 다만 P0-A 구현은 기존 dirty 변경을 안전하게 분리한 뒤 현재 `main@49f53fe`에서 새 브랜치 또는 별도 worktree로 시작해야 한다. 권위 handoff 문서가 확보되기 전에는 cross-repo 계약을 최종 freeze할 수 없다.

### 단계 판정

| 단계 | 판정 | 이유 |
|---|---|---|
| A0 / 코드 조사 | 완료 | 아래 8개 필수 항목을 코드 근거로 확정 |
| A1 / P0-A | 미착수 | A0의 구현 금지 지시, dirty `main`, 누락된 권위 handoff schema |
| P0-B 이후 | 미착수 | P0-A gate 전 observation evidence, graph, writer 구현 금지 |

---

## 1. 현재 point crop에서 source attributes가 끊기는 정확한 흐름

현재 `point_crops`는 의도적으로 source-record crop이 아니라 파노라마 색으로 다시 칠한 QA 시각화 LAS다.

```text
process_image_task()
  -> extract_points_for_detection()
  -> collect_detection_points_at_range()
  -> PointCloudReaderCache.read_block_points()
  -> range/projection/crop/mask 필터
  -> cluster_extracted_points()
  -> crop_points_for_las_export()
  -> write_las()
```

### 정확한 손실 지점

1. `mms_shp_detection/pointcloud.py:1878`의 `read_block_points()`는 cached record 중 `xyz`, normalized `rgb`, `intensity`만 3-tuple로 반환한다.
2. `mms_shp_detection/pipeline.py:6249`의 `collect_detection_points_at_range()`는 `pipeline.py:6280`에서 반환을 `coords_xyz, _raw_rgb, _intensity`로 받아 RGB와 intensity도 즉시 버린다.
3. `pipeline.py:6287` 이후 range, projection, finite, crop, padded detection mask를 통과시키지만 row별로 남기는 값은 XYZ, rectified pixel, distance뿐이다. source file, block start, source index를 함께 전달하지 않는다.
4. `pipeline.py:6053`의 `cluster_extracted_points()`는 cluster/trim 결과 배열만 반환한다. 입력 row에 대한 `selected_indices` 또는 selection mask가 없어서 source identity를 사후 복원할 수 없다.
5. `pipeline.py:6803`은 source RGB가 아니라 `rectified_rgb[pixel]`에서 색을 다시 샘플링한다.
6. `pipeline.py:6007`의 `crop_points_for_las_export()`는 XYZ와 panorama RGB만 대표점 주변 cube로 한 번 더 자른다.
7. `pipeline.py:4192`의 `write_las()`는 새 LAS 1.4 point format 2를 만들고 XYZ와 RGB만 기록한다. intensity, classification, GPS time, return metadata, source tuple은 이 시점에 복원 불가능하다.

이 의미는 `pipeline.py:117`의 `POINT_CROP_SEMANTICS`와 `tests/test_pipeline_helpers.py:1086`의 회귀 테스트에 명시돼 있다. 신규 학습용 crop을 만들기 위해 이 흐름이나 `write_las()`를 record-preserving으로 바꾸면 안 된다. P0-B에서 별도의 record-aware collector를 병렬 추가해야 한다.

### 병렬 경로에 필요한 최소 계약

```text
collect_detection_record_evidence_at_range()
  -> read_block_records()
  -> 모든 row-aligned 배열에 동일 mask/take
  -> selected_indices를 권위 결과로 유지
  -> (source_file_index, source_index)와 selection flags 저장
```

기존 `collect_detection_points_at_range()`, `cluster_extracted_points()`, `write_las()`의 반환과 출력은 그대로 둔다.

---

## 2. pole axis inlier와 full pole/context가 분리되는 지점

`mms_shp_detection/pipeline.py:5265`의 `extract_pole_for_detection()`에는 이미 full neighborhood와 axis inlier가 논리적으로 분리돼 있다.

1. 내부 `load_and_search()`는 `pipeline.py:5386`에서 `read_block_records()`를 호출한다.
2. `pipeline.py:5394` 이후 sign 주변 3D envelope를 적용하고 모든 record 배열에 같은 `keep`을 적용한다.
3. `pipeline.py:5418` 이후 각 block의 record 배열을 이어 붙인 `selected_records`가 full pole/ground/context neighborhood다.
4. `find_pole_bases_with_corridor_fallback()`에 `selected_records["xyz"]`를 넘긴다. 결과의 인덱스는 이 `selected_records` row 공간을 기준으로 한다.
5. `mms_shp_detection/pole.py:344`의 `PoleAxisCandidate.point_indices`는 축 fitting에 채택된 inlier만 담는다.
6. `pole.py:413`의 `PoleSearchResult.point_indices`는 후보별 axis inlier의 union이다.
7. `pipeline.py:5742`는 이 union만 `write_pole_las(records, result.point_indices, ...)`에 넘긴다. 따라서 기존 `pole_crops`는 full pole/context가 아니라 axis-inlier QA crop이다.

추가 손실도 있다.

- `pole.py:332`의 `GroundEstimate.support_xyz`에는 ground support 좌표만 있고 source row index가 없다.
- `pole.py:2507`의 `ground_indices`는 full neighborhood index지만, `estimate_local_ground(points[ground_indices])`에 subset을 넘긴 뒤 원 index mapping을 버린다. ground fitting은 cell별 synthetic sample을 만들기 때문에 좌표 nearest-match로 source tuple을 역복원하면 안 된다.
- `pole.py:2395`의 `connection_indices`도 계산 중에는 존재하지만 coherent tube 선택 결과가 count/quality 지표로 축약돼 source row tuple이나 mask가 남지 않는다.
- `selected_records`에는 LAS 파일 내부 `source_index`가 있지만 source file identity가 row별로 붙지 않는다.
- `find_pole_bases_with_corridor_fallback()`의 반환 corridor mask는 expanded 검색을 시도한 경우 expanded mask가 될 수 있어 최종 선택 candidate의 정확한 evidence mask로 간주할 수 없다. strict/expanded/physical-fallback evidence를 candidate와 함께 이동시켜야 한다.

P0-B에서는 `result.point_indices`를 오직 `pole_axis_inlier_mask`로 해석해야 한다. `selected_records` 전체가 `pole_neighborhood`, ground와 connection은 각각 별도 source-index mask여야 한다. 기존 `pole_crops`와 `write_pole_las()`는 변경하지 않는다.

`mms_shp_detection/manual_pole_base.py:929`도 `GroundEstimate`를 직접 생성한다. `GroundEstimate`에 evidence 필드를 추가하는 방식은 이 수동 proposal 경로를 깨므로 optional default를 두거나 별도 `PoleEvidenceMasks` 계약으로 분리해야 한다. 수동 proposal을 자동 object-crop observation ground truth로 섞지 않는다.

---

## 3. 이미 존재하는 source record index 경로

### LAS

`mms_shp_detection/pointcloud.py:1657`의 `_read_las_records()`는 다음 기존 배열을 만든다.

```text
xyz                  float64, LAS scale/offset 적용 좌표
rgb                  uint8, 기존 normalized view
intensity            uint16
classification       int16
gps_time             float64
gps_time_type        int8, 파일-level global encoding을 row별 반복
return_number         uint8
number_of_returns     uint8
source_index          int64
```

`pointcloud.py:1728`의 `np.arange(start, start + len(points))` 때문에 `source_index`는 물리 LAS 파일 내부의 정확한 0-based record index다. block start가 보존되므로 P0-A는 이 계약을 새로 만들 필요가 없다.

다만 `(source_index)`만으로는 전역 식별자가 아니다. 아래가 추가로 필요하다.

- deterministic `source_file_index`
- root-relative POSIX logical URI
- source file SHA-256
- parsed exact job/track identity
- `provenance_complete`와 `training`

권위 source tuple은 `(source_file_index, source_index)`로 고정하고 `point_source_id`를 record index 대용으로 사용하지 않는다.

### PCDB

`pointcloud.py:1731`의 `_read_pcdb_records()`는 LAS-only 필드를 sentinel로 만들고 `source_index=-1`을 기록한다. 현 catalog와 record에는 이 불완전성을 정책으로 표시하는 값이 없다. P0-A에서 다음을 명시해야 한다.

```text
source_type = pcdb
provenance_complete = false
training = false
```

PCDB의 native XYZ/RGB8/intensity는 legacy QA에 사용할 수 있지만 첫 authoritative training source로 승격하면 안 된다.

### P0-A에서 추가할 raw fields

현재 누락된 필드는 다음과 같다.

```text
rgb_raw                LAS RGB uint16 bit-exact
point_source_id
scan_angle 또는 scan_angle_rank의 source semantic/dtype
user_data
edge_of_flight_line
scan_direction_flag
field availability
```

`pointcloud.py:1445`의 `_rgb8_from_las()`는 RGB가 없으면 기존 view 호환용 neutral 128을 만들고, RGB16을 uint8로 축소한다. 따라서 `rgb`를 바꾸지 말고 `rgb_raw`와 availability를 별도 추가해야 한다. 0 fill을 valid raw 값으로 표시하면 안 된다.

`pointcloud.py:1752`의 `_freeze_records()`와 `tests/test_pointcloud.py:610`은 현재 record mapping의 모든 값이 ndarray라고 가정한다. `records["field_availability"]`에 nested dict를 바로 넣으면 pole의 generic mask/concat과 cache freeze가 깨진다. P0-A에서는 다음 중 하나로 계약을 명시하고 테스트로 고정해야 한다.

- row-aligned boolean availability 배열을 record mapping에 추가하고 batch-level metadata는 `SourcePointBatch`에 둔다.
- 또는 `SourcePointBatch(records, field_availability, source_identity)`를 새 API로 추가하고 기존 `read_block_records()`는 array-only mapping으로 유지한다.

기존 `read_block_points()` 3-tuple과 기존 record key의 dtype/의미는 바꾸지 않는다.

### 현재 source scope와 hash의 결손

- `pointcloud.py:102`의 `_source_signature()`는 absolute path, relative path, size, mtime만 기록하며 content SHA-256이 없다.
- `pointcloud.py:132`의 discovery는 `.pcdb`와 `.las`만 찾는다. `.laz`/COPC 지원은 현재 코드에 없다.
- `pointcloud.py:228`의 `_parse_las_identity()`는 알려진 이름/폴더 규약만 parse하고 unknown identity를 허용한다.
- `pointcloud.py:985`의 `build_pointcloud_catalog()`는 `include_jobs`만 지원한다. canonical equality라 substring match는 아니지만 track allowlist와 strict reject가 없다.
- mixed/auto catalog의 PCDB는 job filter를 적용받지 않는다.
- `pointcloud.py:1295`의 `match_nearest_pointcloud_files()`는 exact job/track가 없으면 same job, same track, 전체 files로 fallback한다. legacy projection에는 필요한 호환 동작이지만 strict object-crop source scope에는 허용할 수 없다.
- `pipeline.py:7737`의 scoped catalog cache key와 `pipeline.py:7885`의 catalog build도 선택 job만 넘기므로 같은 job의 Track02가 catalog에 들어올 수 있다.

P0-A의 `include_scope`는 source를 열기 전에 exact job/track pair로 거르고, identity를 parse할 수 없는 source를 strict mode에서 거부하며, normalized scope를 catalog/cache identity에 포함해야 한다. legacy matcher의 permissive fallback은 바꾸지 않고 object-crop strict 경로에서 우회하는 편이 안전하다.

---

## 4. post-dedup physical-object hook 위치

### 사용해야 할 최종 경로

권위 최종 수렴점은 `mms_shp_detection/pipeline.py:7982`의 `finalize_prepared_model_run()`이다.

```text
collect_detection_records()                     pipeline.py:8004
collect_pole_records()                          pipeline.py:8017
reconcile_remote_supports_from_direct_anchors() pipeline.py:8022
cluster/filter pole observations                pipeline.py:8031
attach_support_ids_to_detection_records()       pipeline.py:8050
deduplicate_sign_and_pole_observations()        pipeline.py:8051
<physical object graph/crop hook>               pipeline.py:8059
staged SHP write + atomic publish               pipeline.py:8060
```

single-model과 parallel multi-model이 모두 이 함수로 수렴하므로, `pipeline.py:8059`의 dedupe 직후와 SHP staging 전이 유일한 권위 hook이다.

### 사용하면 안 되는 경로

`pipeline.py:2659`의 `refresh_shapefile_from_txt()`에도 유사한 collect/attach/dedupe 흐름이 있지만 이는 best-effort `*.in_progress.*` heartbeat 출력이다. 중간 상태에서 object crop을 만들면 같은 sample의 중복/부분 publish와 물리 객체 ID 변동이 생긴다. 여기에 hook을 두지 않는다.

### hook 전에 보존할 입력

`mms_shp_detection/shp_writer.py:836`의 `deduplicate_sign_and_pole_observations()`는 대표 record 하나만 반환한다. 현재 보존 aggregate는 `observation_count`와 `source_detection_ids`뿐이며, pole relation도 canonical detection별 한 행만 남긴다.

따라서 대입 전후로 다음을 모두 보존해야 한다.

- attach 완료된 pre-dedup detection observation lookup
- reconcile 완료 per-frame pole observation lookup
- filter 전/후 clustered support relations
- final deduplicated detection/pole records
- observation evidence index와 sidecar logical URI

또한 `shp_writer.py:450`의 `collect_detection_records()`와 `shp_writer.py:502`의 `collect_pole_records()`는 `accepted_for_shp=false` 또는 XYZ가 없는 관측을 제외한다. accepted/review/rejected object-crop manifest를 final SHP record만으로 만들 수 없다. P0-B부터 별도 sidecar/evidence index를 graph input으로 유지해야 한다.

기존 dedupe의 signature, 2-tuple 반환, representative 선정, 정렬과 SHP 입력은 그대로 둔다. `member_observation_ids`, `member_detection_ids`, `source_sidecar_uris`는 별도 graph mapping에 집계하거나 반환 representative copy의 extra key로만 추가한다. SHP writer는 명시적 DBF whitelist를 사용하므로 extra key는 SHP schema에 쓰지 않는다.

---

## 5. sidecar schema 변경 필요 여부

변경이 필요하지만 P0-A에서는 아직 변경하지 않는다.

현재 `mms_shp_detection/pipeline.py:110`의 `RESULT_SCHEMA_VERSION`은 18이고, `process_image_task()`가 `pipeline.py:7228`에서 per-frame sidecar에 기록한다. P0-B에서 다음 detection-level 필드를 추가할 때 schema 19가 필요하다.

```text
observation_id
record_evidence_uri
record_evidence_sha256
record_evidence_schema
source_scope_id
```

큰 per-point 배열은 sidecar JSON에 넣지 않고 NPZ URI와 SHA-256만 둔다. 기존 필드는 삭제하거나 rename하지 않는다.

### 호환 migration

- object crop 비활성 실행: schema 18과 기존 sidecar bytes/skip semantics 유지
- object crop 활성 실행: schema 19로 쓰고 위 필드 검증
- reader/collector: 18과 19를 명시적으로 구분해 둘 다 읽기
- `pipeline.py:2148`의 `missing_result_artifacts()`: schema 19 evidence 파일 존재와 hash 검증 추가
- `pipeline.py:7073`의 skip-existing: effective schema별 비교
- run fingerprint와 stage version 사용처: effective schema에 맞게 분기
- `shp_writer.py:450`은 detection dict를 copy하므로 신규 필드를 자연히 받을 수 있지만 sidecar URI는 현재 추가하지 않는다.
- `shp_writer.py:502`는 pole record를 whitelist로 재구성하므로 `observation_id`와 evidence reference를 명시적으로 전달해야 한다.

전역 상수를 무조건 19로 올리면 object crop을 끈 실행도 기존 schema 18 cache를 전부 재처리하고 run fingerprint가 바뀐다. feature-on에서만 19를 선택하는 명시적 effective result schema가 기존 출력 불변 요구와 가장 잘 맞는다.

### run manifest

`mms_shp_detection/infrastructure/manifest_writer.py:24`의 실행 단계와 `manifest_writer.py:404`의 run manifest schema 1은 현재 legacy pipeline 성공/실패와 SHP 출력을 관리한다. `validate_published_outputs()`는 SHP와 optional `models_manifest.json`만 검증한다.

object crop은 명세대로 별도 `ObjectCropManifestStore`와 publisher를 두는 것이 안전하다. legacy run manifest에는 검증된 object-crop dataset manifest의 상대 URI/hash/status pointer만 추가한다. optional object-crop 실패를 main manifest의 failed stage로 남기면 succeeded invariant와 충돌하므로 `require_success` 정책을 별도 상태로 표현해야 한다.

원자적 게시 구현은 `RunManifestStore`의 unique temp + flush/fsync + lock + replace와 SHP bundle의 staging/rollback을 참고한다. 다만 `pipeline.py:1722`의 단순 `atomic_write_text()`는 lock과 directory transaction이 없어 authoritative object dataset에 사용할 수 없다. `RunManifestStore`의 SMB 호환용 in-place rewrite fallback도 lock을 모르는 reader에게 strict atomic이 아니므로 object-crop 최종 commit pointer에는 사용하지 않는다.

---

## 6. 신규 package와 기존 파일 변경 목록

### 신규 package

최종 목표 package는 명세 구조를 유지한다.

```text
mms_shp_detection/object_crops/
  __init__.py
  contracts.py
  vocabulary.py
  ids.py
  source_inventory.py
  source_records.py
  observation_evidence.py
  assembly_graph.py
  geometry_router.py
  selection.py
  roles.py
  canonicalization.py
  writer.py
  validator.py
  publisher.py
  manifest.py
```

P0-A에서 실제 동작을 구현할 범위는 `contracts.py`, `vocabulary.py`, `source_inventory.py`, `source_records.py`와 package skeleton뿐이다. 나머지는 import-safe skeleton 또는 후속 단계에서 추가하며 P0-A가 observation evidence/graph/writer를 선행 구현하지 않는다.

현재 `config.py:305`의 YAML loader는 section을 조직 단위로만 보고 nested mapping을 leaf까지 flatten하며, opaque mapping 예외는 `model_filters`뿐이다. 명세의 `object_crops.source_scope.jobs[].tracks`는 이 방식으로 표현할 수 없다. `object_crops` 전체를 하나의 parser destination으로 유지하고 신규 contracts parser가 YAML mapping/CLI JSON을 canonical mapping으로 검증하는 것이 안전하다. `enabled=false`일 때 이 mapping은 legacy run fingerprint, output directory 생성, stage plan과 sidecar schema에 영향을 주면 안 된다.

### 기존 파일별 변경 예정

| 파일 | 단계 | 최소 변경 |
|---|---|---|
| `mms_shp_detection/pointcloud.py` | P0-A | raw LAS arrays와 availability, exact `include_scope`, source identity metadata; 기존 두 read API 호환 |
| `mms_shp_detection/pipeline.py` | P0-A | object-crop config parsing, 선택 task의 exact job/track scope 전달, run-level source inventory 1회 생성; legacy crop 함수 불변 |
| `mms_shp_detection/config.py` | P0-A | `object_crops`를 검증된 단일 opaque mapping으로 처리. 중첩 scope의 unknown key/type/range/cross-field 검증 |
| `config.yaml` | P0-A | 전체 nested default를 `enabled: false`로 추가하고 parser destination과 동기화 |
| `tests/test_pointcloud.py` | P0-A | source index/raw attrs/availability, PCDB policy, Track02 exclusion 회귀 |
| `tests/test_object_crop_contracts.py` | P0-A | schema/vocabulary/dtype/shape/serialization 계약 |
| `tests/test_object_crop_source_inventory.py` | P0-A | logical URI, deterministic SHA/inventory, strict scope와 mutation 검증 |
| `tests/test_pipeline_helpers.py` | P0-A | exact pair scope가 catalog/cache identity로 전달되는지, disabled legacy 경로 회귀 |
| `mms_shp_detection/pole.py` | P0-B | ground/connection source index evidence 노출. 기존 axis 계산과 결과 수치 불변 |
| `mms_shp_detection/shp_writer.py` | P0-B/P0-C | observation/evidence reference 전달과 member aggregate; 기존 dedupe 대표와 DBF schema 불변 |
| `mms_shp_detection/infrastructure/manifest_writer.py` | P0-D/P0-E | 검증된 object dataset pointer를 main output에 노출할 때만 additive 확장 |
| `tests/test_pole.py` | P0-B | axis/full neighborhood/ground/connection mask 정렬 |
| `tests/test_shp_dedupe.py` | P0-C | deterministic member observation/sidecar aggregate와 기존 dedupe 회귀 |
| `tests/test_execution_architecture.py` | P0-D/P0-E | staging, atomic publish, rollback, main manifest pointer 검증 |
| `tests/test_config.py` | P0-A | enabled/strict/scope schema 및 잘못된 조합 거부 |

다음 legacy 구현은 변경 대상이 아니다.

- `write_las()`와 `POINT_CROP_SEMANTICS`
- `write_pole_las()`와 `POLE_CROP_SEMANTICS`
- `write_shapefile()`, `write_pole_shapefile()`의 field/schema/order
- `refresh_shapefile_from_txt()`의 intermediate publication 의미

---

## 7. targeted test 계획

### P0-A gate용 테스트

1. **작은 LAS raw record fixture**
   - 서로 다른 XYZ, 비-257배 RGB16, intensity, classification, GPS time, returns, point source ID, scan angle, user data, scan/flight flags를 기록한다.
   - block `start > 0`으로 읽고 `source_index`가 파일 내부 원래 index인지 확인한다.
   - XYZ는 source LAS scale 허용오차 이내, raw attributes는 dtype과 값이 bit-exact인지 확인한다.
   - 기존 `rgb` uint8 view의 의미는 그대로인지 확인한다.

2. **scan-angle format fixture**
   - legacy point format의 `scan_angle_rank`와 modern format의 `scan_angle`을 각각 만들고 source semantic/dtype metadata가 구분되는지 확인한다.

3. **필드 부재 fixture**
   - RGB/GPS 등이 없는 LAS에서 기존 fallback 배열은 유지하되 raw availability는 false인지 확인한다.
   - 모든 per-point 배열 길이 정렬과 cache immutability를 확인한다.

4. **exact Job/Track fixture**
   - `JobA/Track01`과 `JobA/Track02`를 함께 두고 Track01 strict scope에서 Track02를 file open 전에 제외한다.
   - unknown identity, exact track 부재와 sidecar/catalog identity 불일치를 fail-closed로 확인한다.
   - Track01과 Track02 scope의 cache signature가 다른지 확인한다.

5. **source inventory fixture**
   - logical URI가 root-relative POSIX이고 absolute path/`..`가 없는지 확인한다.
   - logical URI 정렬로 `source_file_index`가 안정적인지 확인한다.
   - SHA-256이 `hashlib` 기준과 같고 동일 입력의 inventory bytes/hash가 결정적인지 확인한다.
   - size/mtime 변경 시 cache가 무효화되며, 실행 중 source mutation을 검출하는지 확인한다.

6. **PCDB fixture**
   - `source_index=-1`, LAS raw fields unavailable, `provenance_complete=false`, `training=false`인지 확인한다.

7. **기존 API 회귀**
   - `read_block_points()`가 같은 3-tuple을 반환한다.
   - 기존 `read_block_records()` key의 dtype/의미와 shared immutable decode가 유지된다.
   - 기존 include-jobs/split-selection/catalog tests를 통과한다.

8. **config 회귀**
   - repository YAML과 parser destination이 동기화되고 `enabled=false`가 기본인지 확인한다.
   - enabled+strict에서 빈 scope, duplicate job/track, unknown nested key, unsafe output path와 잘못된 type을 거부한다.
   - disabled config가 legacy run fingerprint와 output directory/sidecar schema를 바꾸지 않는지 확인한다.

### P0-A 권장 명령

```powershell
python -m pytest -q `
  tests/test_object_crop_contracts.py `
  tests/test_object_crop_source_inventory.py `
  tests/test_pointcloud.py `
  tests/test_config.py `
  tests/test_pipeline_helpers.py
```

전체 suite는 P0-A에서 실행하지 않는다. 현재 전역 `C:\Python313\python.exe`에는 `pytest`가 설치돼 있지 않은 것이 확인됐으므로, A1 시작 전에 저장소의 검증된 virtual environment 또는 명시된 개발 환경을 선택해야 한다.

### 후속 gate 테스트

- P0-B: aligned masks, cluster selected indices, axis/full/ground/connection evidence, schema 18/19 dual read/write, evidence hash와 누락 artifact invalidation
- P0-B pole: 모든 mask 길이가 neighborhood N과 같고 `axis_inlier ⊆ neighborhood`인지, ground/connection raw row mapping과 strict/expanded candidate evidence가 함께 선택되는지 확인
- P0-C: multi-view member 보존, graph integrity, null/multi/remote routing, source-record dedupe와 membership 보존
- P0-D: NPZ/JSON round-trip, bit-exact source lookup, deterministic order/hash, audit LAZ, staging/rollback/idempotency
- P0-E: object-crop off의 legacy output parity, final hook 1회/model, consumer CPU smoke

---

## 8. 기존 출력 회귀 위험

| 위험 | 현재 원인 | 방어선 |
|---|---|---|
| `point_crops` 의미/bytes 변경 | 기존 writer를 record-preserving으로 재사용 | 신규 writer만 병렬 추가, 기존 point format 2 테스트 유지 |
| `pole_crops`가 full pole로 오인됨 | `result.point_indices`가 axis inlier union | 기존 crop 불변, 신규 evidence에서 mask 이름을 명시 |
| SHP feature/대표점/order 변경 | dedupe signature나 representative 선택 수정 | 기존 dedupe 결과를 그대로 소비하고 별도 graph mapping 작성 |
| SHP DBF schema 변경 | member/evidence 필드를 DBF에 추가 | extra metadata는 sidecar/object graph에만 저장 |
| Track02 source leakage | catalog는 job만 제한하고 matcher가 fallback | strict job/track source inventory를 legacy matcher와 분리 |
| source tuple 충돌 | 파일 내부 `source_index`만 사용 | deterministic file index + file SHA + source index |
| 부재 raw 값이 실제 0으로 오인 | 현재 fallback/sentinel에 availability 없음 | availability를 계약 필수로 하고 validator에서 fail-closed |
| PCDB가 학습 source로 승격 | 불완전 provenance 정책 필드 없음 | `provenance_complete=false`, `training=false` 고정 |
| schema 18 cache 전체 무효화 | 전역 version을 19로 일괄 증가 | feature-off 18, feature-on 19의 effective schema |
| run fingerprint/DBF run ID 변경 | `build_run_fingerprint()`가 args와 핵심 코드 SHA를 포함 | disabled 설정의 fingerprint 정책을 명시하고 byte/semantic parity fixture 추가 |
| 중간 object crop 중복 publish | heartbeat refresh에도 dedupe flow 존재 | 최종 `finalize_prepared_model_run()`에만 hook |
| 잘못된 pole corridor evidence | expanded/fallback 검색 mask와 최종 candidate가 분리됨 | evidence를 candidate별로 보존하고 candidate 선택과 함께 이동 |
| member observation 손실 | dedupe는 대표 row만 반환 | pre-dedup observation/evidence index 보존 |
| rejected 관측 누락 | SHP collectors가 invalid/rejected를 제외 | sidecar/evidence manifest를 graph의 독립 입력으로 사용 |
| main run 성공 계약 훼손 | optional object stage 실패를 main failed stage로 기록 | 별도 object manifest 상태와 `require_success` 정책 사용 |
| partial dataset 노출 | sample마다 최종 경로에 직접 쓰기 | `.staging` + validate + atomic publish + rollback |
| 원자성 착각 | 단순 text replace 또는 SMB in-place fallback 재사용 | strict publisher와 commit-last dataset pointer를 별도 구현 |
| source가 hash 후 변경 | size/mtime cache만 신뢰 | run inventory SHA와 publish 전 immutability 검증 |
| Windows path가 logical URI에 유입 | 현재 `relative_path`는 native separator | source inventory에서 `as_posix()`와 root containment 검증 |

### 기존 테스트 보호선

- `tests/test_pipeline_helpers.py:896`: requested range가 block/point에 모두 적용됨
- `tests/test_pipeline_helpers.py:1086`: point crop은 non-record-preserving point format 2
- `tests/test_pipeline_helpers.py:1102`: pole crop의 현재 core attributes/GPS encoding
- `tests/test_shp_dedupe.py:56`: 대표 관측, source detection IDs, complete-link, input non-mutation
- `tests/test_execution_architecture.py:158`: atomic manifest lifecycle
- `tests/test_pointcloud.py:143`: GPS time encoding과 기존 record API
- `tests/test_pointcloud.py:190`: include-jobs와 cache identity
- `tests/test_pointcloud.py:580`: point/record shared immutable decode

---

## 9. P0-A 착수 체크포인트

다음 조건을 만족한 뒤에만 A1/P0-A를 시작한다.

1. 현재 dirty `main`의 사용자 변경을 커밋, stash 또는 별도 worktree로 안전하게 분리한다.
2. `main@49f53fe` 기준 `feat/record-preserving-object-crops-v1` 브랜치를 만든다.
3. 누락된 `docs/12_CROSS_REPO_OBJECT_CROP_HANDOFF_SCHEMA_V1.md`를 확보하거나, P0-A contracts를 embedded 명세 기준의 provisional 상태로 명시 승인한다.
4. project test interpreter를 확인한다.
5. P0-A 범위를 package/contracts/scope/inventory/raw records/targeted tests로 제한한다.
6. 작은 LAS, Track01/Track02, PCDB fixture gate를 모두 통과한다.

P0-A gate 전에는 stable observation evidence, result schema 19 write, assembly graph, object selection/writer/publisher를 구현하지 않는다.
