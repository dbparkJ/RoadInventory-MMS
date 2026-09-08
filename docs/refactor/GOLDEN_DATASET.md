# MMS 결과 비교 계약과 실제 검증 준비

`scripts/compare_mms_results.py`는 기존 프레임 TXT(JSON)와 SHP를 읽는 별도 비교 도구다. 계산 pipeline, 추론, 지주 추정, 납품 writer를 변경하지 않는다. 합성 fixture는 테스트 실행 시 임시 디렉터리에 생성하며 원본 MMS와 모델은 Git에 포함하지 않는다.

P0-3a는 합성 정상/오류 fixture로 비교기의 false pass를 방지하는 단계다. **P0-3b는 현재 `BLOCKED_VALIDATION`**이다. GPU/모델/데이터의 부재가 아니라, 지정된 실제 대표 fixture, 검토된 baseline, 입력·모델·설정·calibration fingerprint manifest, 실제 반복 실행 분산에 근거한 승인된 비교 기준이 아직 없기 때문이다. 합성 테스트나 `source_kind` 라벨 변경은 실제 결과 동등성 증거가 아니다.

## 실행과 상태

프로젝트 Python 환경에서 실행한다. 아래 경로는 비공개 산출물 별칭 예시이며 실제 검증을 실행했다는 뜻이 아니다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_compare_mms_results.py -v
.\.venv\Scripts\python.exe scripts/compare_mms_results.py --baseline <baseline-bundle> --candidate <candidate-bundle> --profile <reviewed-profile.json> --require-real
```

| 상태 | 종료 코드 | 의미 |
|---|---:|---|
| `PASS` | 0 | 선언한 비교 범위에서 일치. `real_data_verified: false`이면 합성 검증일 뿐이다. |
| `FAIL` | 1 | 결과 차이, 모호한 identity, 손상된 schema, 실패 run 또는 증거 불일치. |
| `BLOCKED_VALIDATION` | 2 | 필요한 파일/읽기 권한/검토된 profile/실제 실행 증거 부족. |

JSON 보고서는 baseline/candidate fixture·commit·source 종류, frame/observation/sign/pole 수, 클래스 및 실패·검토 사유 분포, missing/extra/schema/attribute/CRS 차이, 최대 XY/Z 오차와 사례, 명시 제외 필드, 실제 데이터 검증 여부를 출력한다. 출력 파일 경로는 상대 경로와 비공개 별칭을 사용한다. 오류에 OS 절대경로를 포함하지 않는다. 현재 도구는 결과를 읽기만 하며 baseline을 갱신하지 않는다.

## 비교 bundle

각 실행의 출력 루트에 별도의 `comparison.json`을 둔다. 단일 모델 출력 root를 대상으로 한다. 실제 evidence gate도 단일 모델·단일 calibration run manifest가 필요하며, multi-model 실행의 일부 출력만 가져와 전체 실행 증거를 주장할 수 없다. 다음은 **미완성 형식 예시**이며 placeholder는 검증을 통과하지 않는다.

```json
{
  "schema_version": 1,
  "fixture_id": "reviewed-fixture-alias",
  "source_kind": "real_mms",
  "source_commit": "<full-40-character-tested-commit>",
  "input_fingerprint": "<sha256>",
  "model_fingerprint": "<sha256>",
  "calibration_fingerprint": "<sha256>",
  "config_fingerprint": "<sha256>",
  "comparison_profile": "<reviewed-profile-id>",
  "frame_schema_version": 18,
  "coordinate_contract": {
    "horizontal_crs": "<verified-CRS>",
    "vertical_reference": "<verified-reference-or-explicit-unknown>",
    "units": "metre",
    "axis_order": "x,y,z"
  },
  "run": {
    "status": "succeeded",
    "job_id": "<current-job-id>",
    "attempt": 1,
    "fingerprint": "<run-fingerprint-sha256>"
  },
  "frames_directory": "txt",
  "layers": [
    {"name": "signs", "kind": "sign", "path": "shp/detected_signs.shp"},
    {"name": "poles", "kind": "pole", "path": "shp/pole_bottoms.shp"}
  ],
  "observation_groups": {
    "<canonical-det-id>": ["<source-det-id-1>", "<canonical-det-id>"]
  },
  "assets": []
}
```

- fingerprint 네 가지, fixture ID, 좌표 계약, 관측 그룹은 baseline/candidate 간 같아야 한다. source commit은 다를 수 있다. fingerprint는 placeholder나 경로 대신 실제 SHA-256을 기록한다. profile의 제외 규칙으로 이 계약 차이를 숨길 수 없다.
- `frames_directory` 아래 모든 `*.txt`를 읽는다. 빈 detection 목록은 유효하지만 프레임 파일이 하나도 없으면 통과하지 않는다. 다른 JSON sidecar는 프레임으로 추측하지 않는다. Job/Track과 프레임 선택은 `record_name/image_name`, 검출은 기존 `make_detection_id(record_name, image_name, detection_index)` 계약으로 식별한다.
- 기존 안정 `det_id`에 의한 매칭만 지원한다. 중복 identity, 누락, 추가 객체는 실패한다. ID가 변경되는 알고리즘은 이 profile 범위 밖이며 별도로 검토된 클래스·관계·공간 제약의 일대일 matcher가 필요하다. 독립 nearest 매칭으로 통과시키지 않는다.
- 각 `observation_groups` 키는 실제 내보낸 sign `det_id`여야 하며 자기 자신을 포함한다. 수출 가능한 프레임 검출은 정확히 한 그룹에 속해야 한다. `x`가 있고 `accepted_for_shp`가 `false`가 아닌 기존 수출 정책을 따른다. 그룹의 클래스와 대표 좌표가 SHP와 같아야 한다. 프레임 2개가 SHP 1개가 되는 정상 병합을 허용한다.
- 관측 그룹은 **검토된 baseline에서 별도로 고정**한다. 후보 pipeline의 dedupe 함수를 다시 실행해 정답을 만들지 않는다. 최종 SHP는 `source_detection_ids`를 저장하지 않으므로 이 보조 manifest가 필요하다. 자동 그룹 생성/정답 덮어쓰기는 제공하지 않는다.
- pole은 canonical sign에 대응하는 관계 feature로 읽는다. `support_id`와 클래스가 sign과 같아야 하며 같은 support의 좌표도 일치해야 한다. 하나의 지주에 여러 표지가 연결된 관계를 허용한다. pole `obs_count`와 raw detection 수를 같다고 가정하지 않는다.
- `.shp/.shx/.dbf/.prj/.cpg/.qpj/.wkt2`가 모두 필요하다. PointZ, feature/record/SHX 수, DBF 전체 필드 이름·형식·폭·소수점, UTF-8 한글 값, 좌표 속성과 geometry의 정합성을 검사한다. 내부 DBF 반올림은 해당 필드의 선언된 소수점 자릿수에 따르며 결과 간 허용오차와 구별한다.
- `.prj`는 기존 ESRI WKT1 수평 CRS, `.qpj/.wkt2`는 전체 CRS 계약으로 비교한다. WKT1 round trip은 PROJ `equals` 또는 100% confidence authority 일치로만 허용한다. 수직 기준은 명시적으로 기록하고 baseline/candidate WKT2 전체를 비교한다. 수평 CRS가 같다는 이유로 Z 변환을 수행하지 않는다.

## 검토된 profile

합성 exact profile 예시다. 이 값은 실제 MMS용 허용오차 승인이 아니다.

```json
{
  "id": "synthetic-exact-v1",
  "reviewed_by": "synthetic-fixture-test",
  "rationale": "Deterministic synthetic fixture: exact equality.",
  "xy_tolerance": 0,
  "z_tolerance": 0,
  "exclude": [],
  "numeric_tolerances": []
}
```

XY/Z tolerance는 필수이며 임의 기본값이 없다. 단위는 `coordinate_contract.units`다. 실제 profile은 권위 좌표의 정밀도, LAS scale, 동일 commit 반복 실행 분산을 조사하고 승인 주체와 근거를 기록한다. `0.1m` 같은 편의값이나 자동 golden 갱신을 사용하지 않는다. 모든 비좌표 수치·속성은 기본 exact 비교다.

규칙 경로는 JSON Pointer이며 한 segment의 `*`만 허용한다. 프레임 key는 `record_name|image_name`, 검출 key는 `det_id`, layer feature key도 `det_id`다. `~`와 `/`는 각각 `~0`, `~1`로 escape한다. 예를 들어 실행 중 생성된 시각만 제외하려면 다음을 `exclude`에 넣는다.

```json
{"path": "/frames/*/metadata/generated_at", "reason": "Reviewed wall-clock generation time only"}
```

숫자 leaf의 근거 있는 절대 오차는 `numeric_tolerances`의 `{"path": "/frames/*/detections/*/confidence", "absolute": 0, "reason": "Fixed detections"}` 형식이다. `**`, 마지막 segment `*`, 중복 규칙은 거부한다. container나 metadata 전체를 제거하지 않으며, 제외 대상이라도 key 유무와 자료형의 변화는 계속 검사한다. 좌표 XYZ에는 오직 XY/Z profile을 적용한다. 실제 촬영 timestamp는 입력 identity 근거이므로 nondeterministic runtime timestamp와 혼동하지 않는다.

## crop 및 원본 보존 범위

프레임 JSON의 crop 의미, source identity, 원본 속성 metadata도 일반 속성으로 비교한다. 원본 record 보존을 검증할 파일은 `assets`에 상대 경로·의미·SHA-256을 명시한다.

```json
{"path": "point_crops/fixture-records.bin", "semantics": "Original LAS records and source indices", "sha256": "<sha256>"}
```

선언된 asset은 실제 bytes hash와 기준 hash가 같아야 하며 baseline/candidate inventory도 비교한다. `point_crops`, `pole_crops`, QA용 `image_crops/point_previews`를 같은 원본 데이터로 취급하지 않는다. `assets: []`는 raw record byte 보존을 검증하지 않았다는 뜻이다. JPG/PNG/LAS를 의미적으로 재해석하는 decoder, 선언되지 않은 crop 파일의 자동 수집, 원본 MMS 전체를 비교하는 기능은 이 도구 범위 밖이다. 실제 fixture 승인 시 필요한 보존 파일을 inventory에 포함해야 한다.

## 실제 MMS evidence와 다음 단계

`source_kind: real_mms`일 때는 `--require-real` 유무와 무관하게 `real_evidence`가 필요하다. 다음 필드를 `comparison.json`에 추가한다.

- `reviewed_by`: 실제 입력·실행 증거를 확인한 검토자 식별자.
- `input_inventory_reference`, `input_inventory_sha256`: 비공개 원본 목록의 안전한 별칭과 digest.
- `execution_log_reference`, `execution_log_sha256`: 비공개 실행 기록의 안전한 별칭과 digest.
- `execution`: 위의 `source_commit`, 네 가지 fingerprint, `run` 값을 갖는 객체. 현재 bundle과 정확히 일치해야 한다.
- `run_manifest`: 실제 `run_manifest.json` 상대 경로. 기존 manifest validator와 published-output validator로 실제 `versions.git_commit`·`model_hashes`·`calibration_hash`/성공/현재 job·attempt·설정 hash·현재 단계·최종 SHP 목록을 검사한다.
- `output_sha256`: 모든 프레임 TXT, SHP 7종 component, 선언 asset, actual run manifest의 상대경로→SHA-256 객체. 누락이나 추가 key, bytes 불일치를 거부한다.

증거 검증은 검토자가 실제 MMS라고 확인한 기록과 산출물의 일관성을 확인한다. 외부 비공개 원본 목록/로그를 직접 가져오거나 진위를 독립적으로 증명하지 않는다. 합성 데이터를 실제라고 허위 선언하는 것을 암호학적으로 방지하는 기능이 아니다. 테스트의 인위적 evidence validator 사례도 실제 MMS gate 통과 기록으로 사용하지 않는다.

다음 작업은 대표 실제 범위를 지정하고 같은 입력·모델·calibration·설정으로 **서로 별도 출력 루트**에서 기준 commit과 후보 commit을 실행하는 것이다. calibration 다중 매칭 fixture는 단일 fingerprint scope로 나누거나 명시적인 aggregate 계약을 추가한다. 반복 실행 분산과 보존 대상 crop inventory를 검토하고 profile/관측 그룹/evidence를 확정한 다음 `--require-real`을 실행한다. 누락된 gate가 충족될 때까지 계산 경로 변경의 실제 동등성 및 운영 활성화를 주장하지 않는다.
