# 두 저장소용 Codex 실행 프롬프트

## 1. 사용 원칙

- 한 세션에서 두 저장소를 동시에 수정하지 않는다.
- 각 저장소마다 별도 branch와 PR을 만든다.
- upstream schema가 freeze된 뒤 consumer adapter를 최종 고정한다.
- 작은 변경마다 전체 테스트를 반복하지 않는다.
- milestone에서만 전체 suite를 실행한다.
- 실데이터는 read-only다.
- Track01 5개는 tuning에 사용하지 않는다.

---

# Part A — `RoadInventory-MMS`

## A0. 문서 반영과 코드 조사

```text
현재 저장소의 branch, HEAD, dirty state를 확인하라.

다음 문서를 읽어라.

- docs/ROADINVENTORY_P0_RECORD_PRESERVING_OBJECT_CROP_AGENT_SPEC.md
- docs/MMS_DATA_SPEC.md
- docs/current_architecture.md

그리고 다음 코드를 조사하라.

- mms_shp_detection/pipeline.py
- mms_shp_detection/pointcloud.py
- mms_shp_detection/pole.py
- mms_shp_detection/shp_writer.py
- mms_shp_detection/infrastructure/manifest_writer.py
- mms_shp_detection/config.py
- 관련 tests

기준 main commit이 문서의 commit과 다른 경우 변경 diff를 분석하고
문서를 현재 코드에 맞게 갱신하라.

아직 구현하지 말고 다음을 docs/object_crop_implementation_inventory.md에 작성하라.

1. 현재 point crop에서 source attributes가 끊기는 정확한 함수 흐름
2. pole axis inlier와 full pole/context가 분리되는 지점
3. source record index가 이미 존재하는 코드 경로
4. post-dedup physical-object hook 위치
5. sidecar schema 변경 필요 여부
6. 신규 package와 기존 파일 변경 목록
7. targeted test 계획
8. 기존 출력 회귀 위험

추측하지 말고 코드 위치와 함수명을 근거로 작성하라.
```

## A1. P0-A Source inventory와 raw records

```text
ROADINVENTORY_P0 명세의 P0-A만 구현하라.

요구사항:

- 새 branch feat/record-preserving-object-crops-v1
- mms_shp_detection/object_crops package skeleton
- schema/vocabulary/contracts
- exact Job/Track allowlist
- run-level source SHA-256 inventory
- source-relative logical URI
- raw RGB16, point_source_id, scan-angle 계열, user data, flags
- field availability
- PCDB provenance incomplete 정책
- 기존 read_block APIs 호환

테스트:

- 작은 LAS fixture의 source index/XYZ/raw attrs bit-exact
- Track02 exclusion
- source hash deterministic
- 기존 tests/test_pointcloud.py 관련 회귀

전체 pipeline이나 전체 pytest는 아직 실행하지 마라.
관련 targeted test를 실행하고 결과를 보고하라.
P0-A 종료 기준을 통과하지 못하면 P0-B를 시작하지 마라.
```

## A2. P0-B Observation evidence

```text
P0-A commit과 테스트 결과를 확인하라.
통과한 경우에만 P0-B를 구현하라.

요구사항:

- stable observation_id
- record-aware detection collection
- 모든 per-point array에 동일 mask 적용
- selection flag
- cluster selected row index 보존
- source file/index dedupe
- pole neighborhood/axis/ground evidence
- evidence NPZ + SHA
- sidecar에는 URI/hash만 기록
- 기존 point_crops/pole_crops unchanged
- result schema version migration

대형 배열을 sidecar JSON에 넣지 마라.
pipeline.py에 모든 로직을 넣지 마라.

테스트:

- mask alignment
- deterministic evidence
- multi-block source index
- existing point crop regression
- pole axis mask subset
```

## A3. P0-C Assembly graph와 Router

```text
P0-B를 검증한 뒤 P0-C만 수행하라.

여러 frame observation을 다음 graph로 결합하라.

observation -> panel -> support -> assembly

요구사항:

- member_observation_ids 보존
- stable panel/support/object ID
- graph referential integrity
- direct/remote/cantilever/multi-panel/multi-pole/false-positive/unknown routing
- unsupported family는 보존하되 current model training false
- 기존 SHP support ID는 evidence이지 무조건 GT가 아님
- null/ambiguous support reason code

fixture 기반으로 모든 family를 테스트하라.
Track01 실제 결과를 router threshold tuning에 사용하지 마라.
```

## A4. P0-D Object crop publisher

```text
P0-C Gate 이후 object crop publisher를 구현하라.

권위 schema:
docs/12_CROSS_REPO_OBJECT_CROP_HANDOFF_SCHEMA_V1.md와
RoadInventory P0 명세를 따른다.

요구사항:

- post-dedup physical object 단위
- source LAS full envelope 재선택
- panel/pole/ground/context role
- pole_axis_inlier 별도 mask
- canonical object-local frame
- sample.npz + metadata.json
- optional audit_world.laz + preview
- accepted/review/rejected manifests
- source lookup validator
- transform validator
- staging + atomic publish
- idempotent rebuild
- legacy crop training false

처음에는 ray mode off로 구현하라.
영상 camera origin을 LiDAR ray origin으로 사용하지 마라.
```

## A5. P0-E Integration

```text
P0-D unit/smoke가 모두 통과한 뒤 제한된 실제 integration을 수행하라.

실데이터는 read-only다.

보고:

- exact source inventory
- 다른 Track 배제 증거
- legacy point/pole crop과 새 object crop semantics 비교
- source provenance completeness
- point-role count
- family/reason distribution
- transform error
- accepted/review/rejected count
- training/ray/registration eligibility count
- 기존 SHP와 QA crop 회귀
- 미확정 vertical datum, GPS week, ray 상태

현재 L0 데이터를 자동 ACCEPT training GT로 승격하지 마라.
전체 pytest는 이 milestone에서 한 번 실행하라.
```

---

# Part B — `3d_twin_ai_modeling`

## B0. 문서와 현재 상태 갱신

```text
다음 문서를 저장소 docs에 반영하고 읽어라.

- 09_CURRENT_STATE_AND_NEXT_AGENT_ENTRY.md
- 10_PHASE3_1_MODEL_ABLATION_AND_HYBRID_SPEC.md
- 11_PHASE4_REAL_DATA_READINESS_AND_LABELING_SPEC.md
- 12_CROSS_REPO_OBJECT_CROP_HANDOFF_SCHEMA_V1.md

AGENTS.md의 필수 읽기 목록을 현재 Phase에 맞게 수정하라.
START_HERE.md를 새 저장소 초기화 문서가 아니라 현재 상태 진입 문서로 갱신하라.

현재 baseline commit/config/manifests/checkpoint/results hash를 검증하라.
아직 모델 구조를 변경하지 마라.
```

## B1. Cross-repo schema adapter

```text
RoadInventory object crop v1 fixture를 입력으로 받는
RecordPreservingObjectCropAdapter를 구현하라.

기존 adapter를 삭제하지 마라.

필수 검증:

- schema major version
- source file/index
- logical URI
- source hash
- transform inverse/round-trip
- point array alignment
- availability mask
- role vocabulary
- object graph
- decision/reason
- eligibility
- label valid mask

legacy crop:
training=false
provenance_complete=false

새 schema fixture를 load해 현재 model batch로 변환하는 CPU smoke를 작성하라.
```

## B2. Oracle/Predicted template 평가

```text
현재 학습 surface decode가 GT template을 사용하는 경로와
최종 평가가 predicted template을 사용하는 경로를 분리해 진단하라.

평가 report에 다음을 추가하라.

- oracle_template_geometry
- predicted_template_geometry
- template_error_penalty
- confusion-by-geometry
- invalid prediction coverage

기존 기록된 metric 의미를 조용히 변경하지 말고
새 protocol/version을 사용하라.

고정 checkpoint와 synthetic held-out으로 deterministic 평가를 두 번 실행해
byte-identical artifact를 확인하라.
```

## B3. Hybrid residual

```text
10_PHASE3_1_MODEL_ABLATION_AND_HYBRID_SPEC.md를 따라
rule + AI residual baseline을 구현하라.

비교:

- frozen rule
- current direct AI
- hybrid residual
- hybrid + direct fallback

rule parameter, valid mask와 fit QA는 실제 추론에서 계산 가능한 값만 사용하라.
GT template/parameter leakage를 금지한다.
unsupported family는 router에서 reject/review한다.

먼저 tiny CPU forward와 synthetic tiny-overfit을 통과시켜라.
```

## B4. Sampling ablation

```text
frozen synthetic manifests에서 point-count와 sampling ablation을 수행하라.

- 256 / 512 / 1024 또는 측정된 메모리 한계
- uniform
- part-balanced
- edge-aware

각 최종 후보는 최소 3개 seed를 사용한다.

기록:

- metric mean/std
- paired delta
- peak VRAM
- step time
- inference latency
- invalid coverage
- parameter별 MAE

Track01을 선택 기준으로 사용하지 마라.
```

## B5. Pose/Ray synthetic ablation

```text
pose variation과 ray-consistency를 별도 experiment로 구현하라.

- physical pose variation
- canonicalization noise
- ray loss off/on
- unknown-behind-hit 보호

실제 ray가 없으므로 synthetic 결과로만 보고하라.
Phase 4 실제 성능 향상 또는 운영 승인을 주장하지 마라.
```

---

# Part C — Cross-repo Gate

## C1. Contract test

```text
RoadInventory가 만든 작은 공개/합성 object crop fixture를
3d_twin_ai_modeling consumer에서 읽어라.

다음을 양쪽 저장소에서 동일하게 검증하라.

- schema/version
- vocabulary digest
- sample/metadata hash
- source tuple
- role/availability
- transform
- graph
- eligibility
- deterministic order

producer validator와 consumer validator의 결과가 다르면
실제 학습을 시작하지 말고 계약부터 수정하라.
```

## C2. Phase 4 Readiness Report

```text
두 저장소의 current state를 읽고 Phase 4 readiness report를 작성하라.

PASS/FAIL:

- record-preserving crops
- independent Job/Track splits
- L1 labels
- L2 completion
- vertical datum
- ray availability
- family labels
- split leakage
- consumer load
- review workflow
- operational thresholds

하나라도 필수 P0가 FAIL이면 상태를
BLOCKED_BY_REAL_DATA_CONTRACT로 유지하라.
```
