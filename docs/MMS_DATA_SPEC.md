# TRK500Neo MMS 데이터 납품 명세서

> 문서 상태: 초안 v1.1  
> 목적: 교통표지 탐지 결과를 영상에서 3차원 점군으로 안정적으로 연결하고, 표지 지주 축과 지면의 교점을 재현 가능하게 산출하기 위한 수집·가공 데이터의 계약 및 검수 기준  
> 적용 대상: TRK500Neo 또는 동등한 MMS 장비로 수집한 최종 Sphere 영상, 영상 외부표정, 점군, 궤적 및 캘리브레이션 자료

## 1. 적용 원칙

이 명세서는 특정 제조사의 프로젝트 폴더, 내부 SQLite DB, 비공개 바이너리 또는 전용 프로그램을 납품 형식으로 요구하지 않는다. 공급자는 원천 시스템이 무엇이든 아래에 정의한 JPEG, CSV, JSON, WKT2, LAS/LAZ/COPC와 같은 명시적이고 독립적으로 읽을 수 있는 표준 export를 제공해야 한다.

제조사 원본 프로젝트나 내부 DB는 추적성 확보를 위한 보조 자료로 추가할 수 있지만, 필수 산출물을 대신할 수 없다. 검수는 표준 export와 그 메타데이터만으로 재현 가능해야 한다.

요구 수준은 다음과 같이 구분한다.

- **[필수]** 누락되거나 기준을 만족하지 않으면 검수 실패이다.
- **[조건부]** 해당 종류의 원시 데이터나 처리 방식을 납품할 때만 필수이다.
- **[확인 필요]** 본 계약 체결 전에 발주자와 공급자가 값을 확정해 `manifest.json` 및 최종 명세에 기록해야 한다. 미확정 상태로 납품할 수 없다.

## 2. 현재 TRK500Neo 자료에서 확인된 사항

현재 제공된 실측 자료를 기준으로 다음 사항이 확인되었다. 이는 새 납품 자료의 고정값을 뜻하지 않으며, 공급자가 각 납품 건의 실제 값을 manifest에 다시 명시해야 한다.

| 항목 | 실측 자료 확인 결과 | 계약상 처리 |
|---|---|---|
| Sphere 영상 | JPEG, 7040 × 3520, 2:1 equirectangular | 크기와 투영 정보를 manifest와 sidecar에 명시 |
| Sphere sidecar | `ImageSize`, `SphereRadius`, `HeightLimits`, `WidthLimits`, `PanoramaHotSpot` 존재 | 같은 이름의 텍스트 sidecar 필수 |
| 영상 pose | 세미콜론 구분, 헤더 없는 17열 CSV | 본 문서의 17열 스키마로 고정 |
| 시간 | GPS seconds-of-week, 해당 작업의 GPS week는 2357 | GPS week를 파일명에서 추정하지 말고 manifest에 필수 기록 |
| 자세행렬 | 3 × 3 행렬이 row-major로 저장됨 | 행렬을 기하 계산의 권위값으로 사용 |
| 행렬 축 | `R`은 local→world, `right=col0`, `up=col1`, `forward=-col2` | 본 문서의 축 정의를 필수 준수 |
| 점군 | LAS 1.4, Point Format 7, XYZ·Intensity·GPS time·RGB 포함, 좌표 scale 0.001 m | LAS/COPC 헤더 및 manifest에서 동일하게 선언 |
| 점군 classification | 샘플의 일부 지주 후보점에서 class `84`가 관찰됨 | class `84`는 **관찰값일 뿐 공식 의미가 확정된 코드가 아니다.** 공급자의 공식 class map과 QA 근거 없이 `pole`로 간주하거나 처리 설정에 고정하지 않음 |
| 수평좌표 | 좌표값과 장비 로그는 WGS 84 / UTM zone 52N, EPSG:32652와 일치 | EPSG:32652를 명시하고 모든 자료에서 일치시킴 |
| 수직좌표 | LAS WKT는 EGM2008 height(EPSG:3855)를 선언하지만 궤적 QA는 `H-Ell`이고 Z 값도 타원체고일 가능성을 보임 | **[확인 필요]** 공급자가 실제 높이 종류를 증빙해 하나로 확정하고 잘못된 WKT를 수정해야 함 |
| 영상 캘리브레이션 | Front/Rear 원시 카메라의 EUCM 내부표정과 고정 외부표정이 존재 | 이미 합성된 Sphere에는 이 값을 다시 적용하지 않음 |
| 궤적 | 최적화 및 PCC 조정 계열 산출물이 함께 존재 | 어떤 최종 solution으로 영상과 점군을 만들었는지 단일 `solution_id`로 고정 |
| 점군 분할 | 동일 track의 전체 LAS와 번호 분할 LAS가 함께 존재할 수 있음 | authoritative 파일 집합과 중복 관계를 manifest/index에 명시 |

현재 샘플의 수직좌표 표기는 내부적으로 모순된다. 예를 들어 점군 WKT의 수직 구성요소는 EGM2008 정표고를 뜻하지만, 궤적 품질 파일에는 `H-Ell`이 사용된다. 또한 장비 로그에서 약 43 m의 `height`와 약 18 m의 `displayheight`가 동시에 관찰된다. 발주자는 이를 근거로 높이 종류를 임의 추정하지 않는다. 공급자는 기준점 비교, 처리 설정 및 변환 이력을 근거로 실제 Z가 타원체고인지 EGM2008 정표고인지 서면 확정해야 한다.

## 3. 납품 디렉터리 구조

### 3.1 표준 구조

**[필수]** 하나의 납품 루트는 아래 구조를 따른다. 경로는 `manifest.json`을 기준으로 한 상대경로이며, manifest 안에서는 운영체제와 무관하게 `/`를 구분자로 사용한다.

```text
MMS_<project_id>_<delivery_date>_r<revision>/
  manifest.json
  checksums.sha256
  README_delivery.md
  crs/
    horizontal.wkt2
    vertical.wkt2
  imagery/
    <job_id>/
      <track_id>/
        sphere/
          <job_id>_<track_id>_Sphere.csv
          <job_id>_<track_id>_Sphere.txt
          <job_id>_<track_id>_Sphere_00001.jpg
          ...
  trajectory/
    <job_id>/
      <track_id>/
        trajectory.csv
        trajectory_quality.csv
        processing_report.pdf
  pointcloud/
    classification_map.csv
    <job_id>/
      <track_id>/
        <job_id>_<track_id>_001.copc.laz
        ...
  index/
    pointcloud_index.json
  calibration/
    calibration.json
    calibration_report.pdf
  qa/
    control_points.csv
    pole_ground_truth.csv
    pole_observations.csv
    occlusion_intervals.csv
    point_density_report.csv
    ground_breaklines.gpkg
    pole_samples/
    accuracy_report.pdf
    validation_report.json
```

LAS 분할 납품을 선택한 경우 `.copc.laz` 대신 `.las` 또는 `.laz`를 사용할 수 있다. 파일 형식은 track 안에서 혼용하지 않는 것을 원칙으로 한다.

### 3.2 파일명과 경로

- **[필수]** 파일명은 대소문자를 구분하는 것으로 취급한다.
- **[필수]** 같은 디렉터리에서 대소문자만 다른 파일명을 사용할 수 없다.
- **[필수]** 경로에는 `..`, 절대경로, 드라이브 문자, 제어문자를 포함할 수 없다.
- **[필수]** `job_id`, `track_id`, 영상 파일명은 해당 CSV, trajectory, pointcloud index에서 동일해야 한다.
- **[필수]** 파일명 변경이 필요한 경우 원본명과 납품명을 연결하는 매핑을 manifest에 포함한다.
- **[권장]** 영문, 숫자, `_`, `-`, `.`만 사용하고 공백은 사용하지 않는다.

## 4. `manifest.json`

### 4.1 공통 요구사항

`manifest.json`은 UTF-8, JSON 형식이어야 하며 JSON 주석, `NaN`, `Infinity`, 후행 쉼표를 허용하지 않는다.

**[필수]** 다음 정보를 포함한다.

- manifest schema 이름과 버전
- 납품 ID, 개정 번호, 생성 시각, 공급자 및 담당자
- 장비 모델, 장비 일련번호, 센서 일련번호
- job/track 목록과 각각의 영상·pose·점군·궤적 파일 경로 및 건수
- 수평·수직 CRS, 단위, 좌표축 순서
- 시간 scale, GPS week, GPS-UTC 차이, trigger latency 정의와 적용 여부
- 영상 EO 기준점과 lever arm 적용 여부
- 자세행렬의 방향과 축 정의
- 최종 trajectory `solution_id` 및 처리 단계
- 적용한 캘리브레이션 ID, 버전, 유효기간 및 파일 경로
- 점군 형식, point format, GPS time encoding, RGB와 classification 의미
- ASPRS 표준 및 custom classification의 공식 code map, 생성 이력과 QA 결과
- 지주·표지·지면의 점밀도 및 지주 수직 연속성 통계
- 가림 구간, 동일 객체의 멀티프레임 관측 연결과 관측 가능 여부
- 지주 유형별 ground-truth, 독립 검사점 및 축-지면 교점 검수 자료 경로
- 분할 파일의 authoritative/duplicate/alternate 역할
- checksum 파일과 공간 index 경로
- 미수집·제외 구간 및 제외 사유

모든 자료에 동일한 `delivery_id`, `solution_id`, CRS ID 및 calibration ID가 연결되어야 한다. 서로 다른 solution으로 만든 영상 pose와 점군을 같은 track으로 납품할 수 없다.

### 4.2 JSON manifest 예시

아래 예시는 구조 설명용이다. 특히 `vertical.height_type`의 예시값 `ellipsoidal`은 현재 샘플의 실제 높이를 확정한다는 뜻이 아니며, 공급자가 검증한 값으로 작성해야 한다.

```json
{
  "schema": "mms-delivery-manifest",
  "schema_version": "1.1.0",
  "delivery_id": "SEOUL_SIGN_20250311_R01",
  "revision": 1,
  "created_utc": "2025-03-20T06:00:00Z",
  "supplier": {
    "name": "Example Survey Co.",
    "contact": "qa@example.com"
  },
  "system": {
    "manufacturer": "Leica Geosystems",
    "model": "TRK500Neo",
    "system_serial": "TRK500NEO-EXAMPLE",
    "sensor_serials": {
      "camera_front": "GX047139",
      "camera_rear": "GX047140",
      "imu": "IMU-EXAMPLE",
      "lidar": ["LIDAR-EXAMPLE-01"]
    }
  },
  "crs": {
    "axis_order": ["easting", "northing", "height"],
    "unit": "m",
    "horizontal": {
      "authority": "EPSG",
      "code": 32652,
      "name": "WGS 84 / UTM zone 52N",
      "wkt2_path": "crs/horizontal.wkt2"
    },
    "vertical": {
      "height_type": "ellipsoidal",
      "name": "WGS 84 ellipsoidal height",
      "vertical_epsg": null,
      "reference_frame_realization": "WGS 84 (G2139)",
      "coordinate_epoch": 2025.19,
      "geoid_model": null,
      "wkt2_path": "crs/vertical.wkt2"
    }
  },
  "time": {
    "scale": "GPS",
    "value": "seconds_of_week",
    "gps_week": 2357,
    "gps_minus_utc_seconds": 18,
    "leap_second_table_version": "IERS_BULLETIN_C_2025-01",
    "pose_time_reference": "exposure_midpoint",
    "trigger_to_exposure_midpoint_seconds": 0.000000,
    "trigger_latency_applied_to_pose": true,
    "point_gps_time_encoding": "gps_week_time"
  },
  "frames": {
    "world": "EPSG:32652 plus declared vertical datum",
    "sphere_local": {
      "rotation_direction": "local_to_world",
      "storage": "row_major",
      "right_axis": "column_0",
      "up_axis": "column_1",
      "forward_axis": "negative_column_2"
    },
    "transform_convention": "p_target = R_target_from_source * p_source + t_target_from_source"
  },
  "calibration": {
    "snapshot_path": "calibration/calibration.json",
    "calibration_set_id": "TRK500NEO-EXAMPLE-CAL-20240202",
    "version": "1.0",
    "valid_from_utc": "2024-02-02T14:07:24Z",
    "valid_to_utc": "2025-12-31T23:59:59Z",
    "applied_during_export": true
  },
  "solution": {
    "solution_id": "JOB20250311_OPT1_PCCADJ_V1",
    "type": "tightly_coupled_multi_pass_adjusted",
    "final": true,
    "processing_software": "Supplier Processing Suite",
    "processing_software_version": "X.Y.Z"
  },
  "jobs": [
    {
      "job_id": "Job_20250311_1043",
      "track_id": "Track01",
      "imagery": {
        "projection": "equirectangular",
        "width_px": 7040,
        "height_px": 3520,
        "jpeg_count": 443,
        "pose_count": 443,
        "pose_csv": "imagery/Job_20250311_1043/Track01/sphere/Job_20250311_1043_Track01_Sphere.csv",
        "sidecar": "imagery/Job_20250311_1043/Track01/sphere/Job_20250311_1043_Track01_Sphere.txt",
        "pose_csv_delimiter": ";",
        "pose_csv_has_header": false,
        "eo_reference_point": "stitched_sphere_center",
        "camera_lever_arm_applied": true,
        "raw_camera_calibration_applied_to_stitch": true
      },
      "trajectory": {
        "path": "trajectory/Job_20250311_1043/Track01/trajectory.csv",
        "quality_path": "trajectory/Job_20250311_1043/Track01/trajectory_quality.csv",
        "solution_id": "JOB20250311_OPT1_PCCADJ_V1",
        "orientation_frame": "body_to_world"
      },
      "pointcloud": {
        "format": "COPC_1.0_LAZ",
        "las_version": "1.4",
        "point_format_id": 7,
        "coordinate_scale_m": [0.001, 0.001, 0.001],
        "dimensions": [
          "X",
          "Y",
          "Z",
          "intensity",
          "return_number",
          "number_of_returns",
          "classification",
          "scan_angle",
          "point_source_id",
          "gps_time",
          "red",
          "green",
          "blue"
        ],
        "rgb_encoding": "uint16_linear_0_65535",
        "gps_time_encoding": "gps_week_time",
        "classification_map": "pointcloud/classification_map.csv",
        "partition_group": "JOB20250311_TRACK01_PC_V1",
        "authoritative_set": "copc",
        "files": [
          "pointcloud/Job_20250311_1043/Track01/Job_20250311_1043_Track01_001.copc.laz"
        ]
      }
    }
  ],
  "indexes": {
    "pointcloud": "index/pointcloud_index.json"
  },
  "qa": {
    "control_points": "qa/control_points.csv",
    "pole_ground_truth": "qa/pole_ground_truth.csv",
    "pole_observations": "qa/pole_observations.csv",
    "occlusion_intervals": "qa/occlusion_intervals.csv",
    "point_density_report": "qa/point_density_report.csv",
    "validation_report": "qa/validation_report.json"
  },
  "integrity": {
    "algorithm": "SHA-256",
    "checksum_file": "checksums.sha256",
    "checksum_scope": "all files except checksums.sha256"
  },
  "known_gaps": []
}
```

## 5. Sphere 영상과 sidecar

### 5.1 Sphere JPEG

- **[필수]** 최종 합성된 360° Sphere 영상을 JPEG로 제공한다.
- **[필수]** JPEG는 정상 디코딩되어야 하며 EXIF 회전 플래그 없이 픽셀 배열 자체가 올바른 방향이어야 한다.
- **[필수]** 현재 TRK500Neo 표준 export는 7040 × 3520이다. 다른 크기를 사용할 경우 모든 영상이 manifest 및 sidecar의 선언과 일치해야 하며, 2:1 equirectangular 투영을 유지해야 한다.
- **[필수]** 영상 파일명은 pose CSV 첫 번째 열과 바이트 단위로 일치해야 한다.
- **[필수]** 영상 순번 누락, 재촬영, 중복 또는 폐기 영상은 manifest의 `known_gaps`에 원인과 범위를 기록한다.
- **[필수]** 색공간은 sRGB로 선언하고 ICC profile이 있으면 보존한다. 재압축 여부와 JPEG quality 설정을 처리 보고서에 기록한다.
- **[권장]** 검수용 영상은 과도한 sharpening, AI 보간 또는 비가역적 노이즈 제거를 적용하지 않는다.

최종 Sphere는 원시 Front/Rear 카메라 영상에 내부표정, 카메라 간 고정 외부표정 및 stitching이 이미 적용된 산출물로 취급한다. 그러므로 납품 데이터 소비자는 원시 카메라 EUCM이나 boresight를 Sphere에 다시 적용하지 않는다.

### 5.2 Sphere sidecar

각 pose CSV와 같은 basename의 `.txt` 파일을 같은 디렉터리에 둔다.

```text
ImageSize=7040,3520
SphereRadius=100.0000
HeightLimits=-90.0000,90.0000
WidthLimits=-180.0000,180.0000
PanoramaHotSpot=0,0
```

| 키 | 수준 | 의미와 검수 기준 |
|---|---|---|
| `ImageSize` | 필수 | `width,height`, 양의 정수, 실제 JPEG와 정확히 일치 |
| `SphereRadius` | 필수 | metre 단위 렌더링 메타데이터. 기하 투영의 실제 거리로 오용하지 않음 |
| `HeightLimits` | 필수 | degree 단위, 표준 전체 Sphere는 `-90,90` |
| `WidthLimits` | 필수 | degree 단위, 표준 전체 Sphere는 `-180,180` |
| `PanoramaHotSpot` | 필수 | 표준 납품은 `0,0` |

**[확인 필요]** `PanoramaHotSpot`을 0이 아닌 값으로 납품하려면 공급자는 값의 단위, 축, 부호, 적용 전후 순서와 기준 프레임을 문서화하고 기준 방향이 표시된 테스트 영상을 제공해야 한다. 의미가 불명확한 non-zero 값은 허용하지 않는다.

표준 픽셀-방향 규약은 다음과 같다. `W`, `H`는 영상 크기이며, `right`, `up`, `forward`는 7절에서 정의한다.

```text
longitude = ((u / W) - 0.5) * 2π
latitude  = (0.5 - (v / H)) * π
ray_world = right   * cos(latitude) * sin(longitude)
          + up      * sin(latitude)
          + forward * cos(latitude) * cos(longitude)
```

따라서 영상 중앙은 `forward`, 상단은 `up`, 좌우 경계는 뒤쪽 seam에 해당한다. 픽셀 좌표 원점은 좌상단이고 `u`는 오른쪽, `v`는 아래쪽으로 증가한다.

## 6. 영상 pose CSV 17열 스키마

### 6.1 직렬화 규칙

- **[필수]** UTF-8 인코딩과 `;` delimiter를 사용한다.
- **[필수]** 한 행은 한 JPEG에 대응하며 정확히 17개 열이어야 한다.
- **[필수]** 소수점 기호는 `.`이고 천 단위 구분자와 따옴표를 사용하지 않는다.
- **[필수]** `NaN`, `Inf`, 빈 필드를 허용하지 않는다.
- **[필수]** 기본 형식은 헤더 없음이다. 헤더를 넣는 경우 아래 필드명을 정확히 한 번 사용하고 `manifest.jobs[].imagery.pose_csv_has_header=true`로 선언한다.
- **[필수]** 좌표는 최소 0.001 m, GPS SOW는 최소 0.000001 s, 행렬은 최소 소수점 이하 9자리 해상도로 기록한다.

### 6.2 열 정의

| 열 | 필드명 | 형식/단위 | 정의 |
|---:|---|---|---|
| 1 | `image_filename` | string | 같은 디렉터리의 JPEG basename |
| 2 | `gps_sow` | decimal second | GPS week의 시작부터 경과한 GPS 초 |
| 3 | `x` | m | 카메라 EO 기준점의 world easting |
| 4 | `y` | m | 카메라 EO 기준점의 world northing |
| 5 | `z` | m | 선언된 수직 datum의 높이 |
| 6 | `omega_gon` | gon | Omega |
| 7 | `phi_gon` | gon | Phi |
| 8 | `kappa_gon` | gon | Kappa |
| 9 | `r00` | dimensionless | `R[0,0]` |
| 10 | `r01` | dimensionless | `R[0,1]` |
| 11 | `r02` | dimensionless | `R[0,2]` |
| 12 | `r10` | dimensionless | `R[1,0]` |
| 13 | `r11` | dimensionless | `R[1,1]` |
| 14 | `r12` | dimensionless | `R[1,2]` |
| 15 | `r20` | dimensionless | `R[2,0]` |
| 16 | `r21` | dimensionless | `R[2,1]` |
| 17 | `r22` | dimensionless | `R[2,2]` |

행렬은 다음과 같이 row-major로 복원한다.

```text
R_local_to_world = [
  [r00, r01, r02],
  [r10, r11, r12],
  [r20, r21, r22]
]
```

### 6.3 샘플 행

현재 TRK500Neo export에서 확인한 형식의 예시는 다음과 같다.

```csv
Job_20250311_1043_Track01_Sphere_00001.jpg;180096.447723;329703.430;4153507.556;42.345;-104.0209699152;-4.9369865188;198.2212273728;-0.9966053569;-0.0278532944;-0.0774722953;-0.0790510152;0.0609345761;0.9950064896;-0.0229934672;0.9977530613;-0.0629295562
```

### 6.4 OPK와 행렬의 우선순위

- **[필수]** `omega_gon`, `phi_gon`, `kappa_gon`의 단위는 gon이며 `400 gon = 360° = 2π rad`이다.
- **[필수]** 공급자는 manifest 또는 `README_delivery.md`에 OPK의 회전축, 회전 순서, intrinsic/extrinsic 여부, 부호, world/local 방향을 명시한다.
- **[필수]** 실제 영상-점군 투영에는 9개 행렬 열을 권위값으로 사용한다. OPK는 교차검증과 추적성 용도이다.
- **[필수]** 공급자가 선언한 OPK 규약으로 재구성한 행렬은 납품 행렬과 허용오차 내에서 일치해야 한다.

**[확인 필요]** 현재 샘플의 OPK 구성 규약은 CSV 자체에 기록되어 있지 않다. 공급자는 제조사 내부 규약을 그대로 암묵적으로 요구하지 말고, 회전식을 명시적으로 제공해야 한다.

## 7. 자세행렬과 카메라 축

### 7.1 고정 규약

`R`은 카메라/Sphere local 벡터를 world 벡터로 변환한다.

```text
v_world = R_local_to_world · v_local
p_world = C_world + R_local_to_world · p_local
```

행렬의 열은 다음과 같이 해석한다.

```text
right_world   = R[:, 0]
up_world      = R[:, 1]
back_world    = R[:, 2]
forward_world = -R[:, 2]
```

즉 local camera 축은 `+X=right`, `+Y=up`, `-Z=forward`이다. 다음 조건을 만족해야 한다.

```text
RᵀR = I
det(R) = +1
cross(forward, up) = right
```

world 축은 `+X=easting`, `+Y=northing`, `+Z=up`이다. 공급자는 transpose, world→local 행렬 또는 `col2=forward`인 다른 규약을 같은 17열 스키마에 넣을 수 없다. 다른 원본 규약은 export 과정에서 본 규약으로 변환한다.

### 7.2 행렬 자동검수

- 모든 원소가 유한수인지 확인한다.
- 각 축 norm이 1인지 확인한다.
- 축 간 내적이 0인지 확인한다.
- `max(abs(RᵀR-I)) ≤ 1e-5`인지 확인한다.
- `abs(det(R)-1) ≤ 1e-5`인지 확인한다.
- `cross(forward,up)`과 `right`의 최대 절대차가 `1e-6` 이하인지 확인한다.
- 인접 영상의 자세 변화가 비정상적으로 뒤집히지 않는지 확인한다.

## 8. 시간 기준, GPS week, leap second 및 trigger latency

### 8.1 GPS 시간

- **[필수]** pose CSV 2열은 GPS time scale의 seconds-of-week이다.
- **[필수]** `0 ≤ gps_sow < 604800`이어야 한다.
- **[필수]** 각 job/track의 `gps_week`를 manifest에 정수로 기록한다. 파일명이나 작업 날짜로 week를 추정하게 해서는 안 된다.
- **[필수]** 수집 당시의 `GPS-UTC` 차이를 초 단위로 기록하고, 사용한 leap-second table의 버전 또는 출처와 유효일을 함께 기록한다.
- **[필수]** GPS week rollover 처리 여부를 기록한다.
- **[필수]** UTC를 함께 제공할 경우 ISO 8601 UTC(`YYYY-MM-DDThh:mm:ss.ssssssZ`)로 제공하고 GPS 값과 상호 일치해야 한다.

변환식은 다음과 같다.

```text
UTC = GPS epoch (1980-01-06T00:00:00Z)
    + gps_week × 7 days
    + gps_sow seconds
    - gps_minus_utc_seconds
```

현재 샘플은 `gps_week=2357`, `gps_sow=180096.447723`, `GPS-UTC=18 s`일 때 `2025-03-11T02:01:18.447723Z`에 해당한다. `18 s`는 현재 샘플의 값일 뿐이며 미래 납품에 하드코딩하지 않는다.

### 8.2 영상 trigger와 노출 시각

- **[필수]** pose CSV의 시간은 최종 Sphere 영상의 기준 노출시각, 원칙적으로 `exposure_midpoint`여야 한다.
- **[필수]** `trigger_to_exposure_midpoint_seconds`는 `t_exposure_midpoint = t_trigger + Δt`의 부호로 정의한다.
- **[필수]** trigger latency가 CSV 시간 또는 보간 pose에 이미 적용되었는지 `trigger_latency_applied_to_pose`로 선언한다.
- **[필수]** shutter 방식(global/rolling), 노출시간, Front/Rear 동기화 방법, 합성 Sphere의 대표시각 선정 방법을 처리 보고서에 기록한다.
- **[필수]** latency 보정값의 산출 방법, 캘리브레이션 일자, 적용 버전 및 불확도를 기록한다.
- **[조건부]** latency가 적용되지 않았다면 원 trigger 시각, 센서별 노출 중앙 시각, 보정식을 함께 제공해야 하며 발주자의 사전 승인을 받아야 한다.

**[확인 필요]** 카메라 간 동기 오차와 trigger-to-exposure 불확도의 계약 한계는 16절의 제안값을 기준으로 계약 전에 확정한다.

## 9. 카메라 EO 기준점과 lever arm

영상 pose의 XYZ가 무엇의 위치인지 명확하지 않으면 영상 ray와 점군을 올바르게 교차시킬 수 없다. 장비 이름이나 관행만으로 기준점을 추정하지 않는다.

### 9.1 권장 납품 방식

**[필수]** 최종 Sphere 납품의 권장 방식은 다음과 같다.

```text
eo_reference_point = stitched_sphere_center
camera_lever_arm_applied = true
R = sphere_local_to_world
```

이 경우 CSV의 `C_world=[x,y,z]`는 최종 equirectangular Sphere의 가상 투영 중심이며, GNSS 안테나·IMU/body 원점에서 카메라 중심까지의 lever arm과 고정 회전은 이미 적용되어 있어야 한다.

### 9.2 다른 기준점을 사용할 경우

**[조건부]** CSV XYZ가 IMU 원점, GNSS 안테나 기준점 또는 다른 body 원점을 나타내면 다음 정보를 모두 제공한다.

- `eo_reference_point`의 정확한 frame 이름과 물리적 위치
- `camera_lever_arm_applied=false`
- Sphere frame과 body/IMU frame 사이의 4 × 4 rigid transform
- transform source/target 방향, 회전 행렬 저장순서, translation 표현 frame 및 단위
- trajectory 자세가 body→world인지 world→body인지
- 보간 및 lever-arm 적용식
- 해당 transform의 calibration ID와 유효기간

본 명세의 표준 transform 이름은 `T_target_from_source`이며 다음 식을 사용한다.

```text
p_target = R_target_from_source · p_source + t_target_from_source
```

예를 들어 trajectory가 body 원점과 `R_world_from_body`를 제공하고 `T_body_from_sphere`가 제공되면 다음과 같다.

```text
C_world_sphere = C_world_body
               + R_world_from_body · t_body_from_sphere
R_world_from_sphere = R_world_from_body · R_body_from_sphere
```

**[필수]** lever arm 적용 여부가 누락되거나 `unknown`이면 해당 track은 검수 실패이다.

## 10. 원시 카메라 영상 제공 시 내부표정과 boresight

### 10.1 최종 Sphere만 제공하는 경우

원시 Front/Rear 영상이 아니라 이미 합성된 Sphere JPEG만 제공하는 경우, 원시 카메라의 EUCM 내부표정과 개별 카메라 boresight는 기하 계산 입력으로 요구하지 않는다. 다만 어떤 calibration set이 합성에 적용되었는지 식별할 수 있도록 calibration ID, 버전, 센서 일련번호 및 유효기간은 제공해야 한다.

**[필수]** 소비자는 Sphere에 원시 카메라 IO/boresight를 중복 적용하지 않는다.

### 10.2 원시 Front/Rear 영상을 함께 제공하는 경우

**[조건부]** 원시 영상을 제공하거나 발주자가 Sphere를 다시 생성해야 하는 경우 `calibration/raw_cameras.json`에 각 카메라별 다음 항목을 제공한다.

- 카메라 ID, 이름, 일련번호, 영상 폭·높이
- calibration ID, 버전, 수행일, 유효 시작·종료일, 상태(pass/fail)
- EUCM 모델명과 정확한 모델 식
- `fx`, `fy`, `cx`, `cy`, `s`, `alpha`, `beta` 및 단위(pixel 또는 dimensionless)
- distortion 모델명과 `p1`, `p2`, `k1`, `k2`, `k3`
- 픽셀 원점, pixel-center 규약, X/Y 증가 방향
- raw camera frame의 축 정의
- 각 카메라와 body/IMU 또는 Sphere frame 사이의 boresight/lever-arm 4 × 4 transform
- translation 단위와 표현 frame
- Euler 값도 제공할 경우 각도 단위, 축, 순서, 방향
- 노출시각, shutter 방식 및 센서 간 동기 offset
- 적용한 stitching 소프트웨어와 버전, seam/blending 설정

`r1/r2/r3`, `t1/t2/t3`처럼 의미가 문서화되지 않은 이름만 제공하는 것은 허용하지 않는다. 반드시 `T_target_from_source` 형태의 명시적 행렬과 frame 정의를 함께 제공한다.

## 11. 점군 LAS/LAZ/COPC 요구사항

### 11.1 허용 형식

- **[필수]** LAS 1.4 또는 LAS 1.4 기반 LAZ/COPC를 사용한다.
- **[권장]** 대용량 데이터는 공간 index를 내장한 COPC 1.0 LAZ를 사용한다.
- **[필수]** 기본 point format은 7이다. 다른 point format은 필요한 차원과 의미를 모두 보존하며 발주자의 사전 승인을 받은 경우만 허용한다.
- **[필수]** 압축 여부와 무관하게 표준 LAS reader로 열려야 한다.
- **[필수]** 좌표 scale은 축별 0.001 m 이하를 권장하며, 계약 정확도를 손상시키지 않아야 한다. 현재 TRK500Neo 샘플은 0.001 m이다.

### 11.2 필수 point 차원

| 차원 | 수준 | 요구사항 |
|---|---|---|
| X, Y, Z | 필수 | 선언된 동일 CRS, metre |
| intensity | 필수 | uint16, 센서/정규화 방식 명시 |
| return number / number of returns | 필수 | LAS 표준 의미 준수 |
| classification | 필수 | ASPRS class 코드와 공급자 mapping 제공 |
| synthetic/key-point/withheld/overlap flags | 필수 | 실제 의미대로 설정 |
| scanner channel | 필수 | 다중 채널이면 원 센서와 연결 가능해야 함 |
| scan angle | 필수 | LAS 1.4 표준 단위/scale 준수 |
| point source ID | 필수 | flight line/track/source mapping 문서화 |
| GPS time | 필수 | encoding과 time scale 명시 |
| Red, Green, Blue | 필수 | uint16, 색상화 방법과 원본 영상 기록 |
| NIR | 조건부 | 제공 가능한 경우 point format 8 사용 및 의미 명시 |

RGB가 8-bit 영상에서 생성되었다면 `rgb16 = rgb8 × 257`과 같이 0과 65535 끝점이 보존되는 변환식을 기록한다. 8-bit 값을 단순히 uint16 하위 범위 0~255에 넣어서는 안 된다. 색상이 없는 점에 임의의 회색값을 넣고 RGB가 실제 측정값인 것처럼 표시할 수 없다. 색상이 없는 점은 별도 flag/Extra Byte 또는 명시된 sentinel 정책으로 식별한다.

### 11.3 공식 classification map

classification은 사용한 ASPRS LAS 버전의 표준 분류를 따라야 한다. 모든 점이 0 또는 1인 경우에도 "미분류"임을 manifest에 명시해야 하며, 별도의 지면/비지면 분류를 수행했다고 오인하게 해서는 안 된다.

**[필수]** `pointcloud/classification_map.csv`를 제공하고 manifest의 각 pointcloud 항목이 이 파일을 참조해야 한다. 본 사업에서 사용하는 최소 의미는 다음과 같이 고정한다.

| semantic role | 공식 LAS class | 본 사업의 의미와 처리 |
|---|---:|---|
| `unclassified` | 0 또는 1 | 생성 후 미분류 또는 unclassified. 지면이나 지주로 추정해서 사용할 수 없음 |
| `ground` | 2 | ASPRS `Ground`. 자연지반·성토면 등 지형면. 도로 포장면, 연석, 구조물 포함 여부를 별도로 명시 |
| `low_vegetation` | 3 | ASPRS `Low Vegetation`. 지주 후보에서 제외할 식생 |
| `medium_vegetation` | 4 | ASPRS `Medium Vegetation`. 지주 후보에서 제외할 식생 |
| `high_vegetation` | 5 | ASPRS `High Vegetation`. 지주 후보에서 제외할 식생 |
| `road_surface` | 11 | ASPRS `Road Surface`. 차량 통행 포장면. 보도·중앙분리대·교통섬·연석 포함 여부를 별도로 명시 |
| `sign_support_pole` | 발주자 승인 custom ID | 교통표지 판을 직접 지지하는 기둥 또는 지지축. LAS 1.4 사용자 정의 범위에서 공급자가 코드를 제안하고 발주자가 승인한 뒤 사용 |

현재 샘플에서 관찰된 class `84`는 지주로 보이는 일부 점에 나타난 **경험적 관찰값**일 뿐이며 공식 class map이 아니다. 다음 조건을 모두 만족하기 전에는 class `84`를 `sign_support_pole`로 확정하거나 프로그램의 `pole_class_ids`에 고정할 수 없다.

1. 공급자가 class `84`의 공식 명칭, 포함·제외 기준과 생성 공정을 서면 회신한다.
2. 납품 전 구간에서 같은 의미로 사용되며 파일별·track별 의미 변경이 없음을 보증한다.
3. 독립 판독 표본으로 precision/recall과 주요 혼동 대상(수목, 가로등, 신호등, 방호울타리, 건물 모서리)을 보고한다.
4. 발주자가 해당 매핑을 승인하고 `classification_map.csv`와 manifest revision에 반영한다.

`classification_map.csv`는 최소 다음 열을 가진다.

```text
class_id,class_name,class_authority,las_spec_version,semantic_role,description,inclusion_rules,exclusion_rules,source_method,software_version,model_version,qa_sample_count,precision,recall,revision
```

- `class_authority`는 `ASPRS_STANDARD` 또는 `SUPPLIER_CUSTOM` 중 하나이다.
- custom class는 코드, 명칭, 객체 범위, 포함·제외 규칙, 수동/자동 생성 방식, 소프트웨어·모델 버전과 QA 결과를 모두 기록한다.
- 교통표지 지주, 가로등주, 전신주, 신호등주, 가드레일과 수목을 하나의 `pole` class로 합친 경우 각 하위 유형의 식별 가능 여부와 혼동률을 별도로 기록한다. 교통표지 지주만 분리할 수 없다면 `semantic_role=generic_vertical_pole`로 선언하고 `sign_support_pole`이라고 표시할 수 없다.
- 동일 `delivery_id` 안에서 같은 `class_id`의 의미가 달라질 수 없다. 의미가 바뀌면 새 delivery revision과 새 map revision을 발행한다.
- class 2와 11을 모두 사용한다면 지면과 노면의 경계를 설명하고, 보도·연석 상단·교통섬·중앙분리대가 어느 class에 포함되는지 명시한다.

#### 처리 프로그램의 미분류 데이터 대응

일부 공급자는 분류가 완료된 LAS를 만들 수 없고, LAS 표준 필드만 존재한 채 값이 전부 `0/1`일 수 있다. 처리 프로그램은 이 경우에도 지주 축의 수직 연속성과 로컬 저점 평면을 이용한 형상 기반 계산을 지원한다.

- `pole_classification_mode: auto`: 승인·설정한 의미 class가 실제 선택 LAS에 있을 때만 `HYBRID`, 없으면 `GEOMETRY`로 자동 전환한다.
- `pole_classification_mode: off`: 원본 class를 파생 LAS에는 보존하지만 지주·지면 계산에서는 강제로 무시한다.
- `pole_classification_mode: require`: 선택된 모든 LAS에 설정한 의미 class가 없으면 처리 전에 실패한다. 분류 포함 납품의 인수검사에 사용한다.

이 fallback은 입력 누락으로 전체 처리가 중단되는 것을 막기 위한 것이며, 본 명세의 분류 납품 요구를 자동으로 면제하지 않는다. 계약에서 미분류 납품을 별도 승인했다면 공급자는 manifest와 `classification_map.csv`에 `0/1=unclassified`를 명시하고, 발주자는 `GEOMETRY` 결과의 별도 정확도·누락률 검증 기준을 정해야 한다. 매핑되지 않은 custom ID는 번호가 존재한다는 이유만으로 지면·식생·지주로 추정하지 않는다.

### 11.4 LAS GPS time

- **[필수]** LAS Global Encoding bit 0과 manifest의 `gps_time_encoding`이 일치해야 한다.
- bit 0이 0이면 GPS Week Time이며, 해당 파일의 `gps_week`를 manifest/index에 기록한다.
- bit 0이 1이면 Adjusted Standard GPS Time이며 LAS 규격의 `GPS time - 1,000,000,000` 정의를 따른다.
- **[필수]** point GPS time에서 UTC로의 변환에 필요한 leap-second 정보가 8절과 동일해야 한다.
- **[필수]** 영상 pose와 점군 time은 같은 time solution 및 동기 보정을 사용해야 한다.

### 11.5 CRS VLR

- **[필수]** 각 LAS/LAZ/COPC 파일은 WKT CRS VLR을 포함한다.
- **[필수]** LAS 1.4에서 WKT를 사용하는 경우 Global Encoding의 WKT bit를 규격에 맞게 설정한다.
- **[필수]** 모든 authoritative pointcloud 파일의 WKT는 의미상 동일해야 하며 manifest와 일치해야 한다.
- **[필수]** 잘못된 EPSG:4326 또는 수직 datum이 모순된 WKT를 좌표값과 함께 납품할 수 없다.

### 11.6 분할, 중복 및 공간 index

동일 track의 전체 LAS와 `_1`, `_2`, … 분할 LAS를 함께 납품하면 단순 파일 검색 시 점이 이중 집계될 수 있다. 따라서 다음을 준수한다.

- **[필수]** `partition_group`별로 정확히 하나의 authoritative 파일 집합을 지정한다.
- **[필수]** 전체본과 분할본이 같은 점을 담으면 한쪽은 `role=duplicate_full_copy` 또는 `role=alternate_non_authoritative`로 표시한다.
- **[필수]** 검수 및 처리 대상은 `role=authoritative`인 파일만이다.
- **[필수]** 분할 파일마다 순번, bounding box, point count, GPS time 범위, SHA-256, overlap 정책을 `pointcloud_index.json`에 기록한다.
- **[필수]** 분할 경계 중복이 있으면 중복 폭, 중복 생성 이유, 안정적인 deduplication key 또는 규칙을 기록한다.
- **[필수]** 중복이 없다고 선언한 경우 authoritative 파일 간 동일 `(X,Y,Z,gps_time,point_source_id)` 레코드가 없어야 한다.
- **[필수]** COPC는 자체 hierarchy가 정상이어야 하며, 별도 index에도 파일 단위 extent와 point count를 제공한다.
- **[필수]** 파일 bounding box 합집합이 해당 track의 declared coverage와 일치해야 한다.

`pointcloud_index.json`의 파일 항목은 최소 다음 필드를 가진다.

```json
{
  "schema_version": "1.0.0",
  "crs_id": "EPSG32652_PLUS_DECLARED_VERTICAL",
  "partition_groups": [
    {
      "partition_group": "JOB20250311_TRACK01_PC_V1",
      "authoritative_set": "splits",
      "overlap_policy": "none",
      "total_unique_point_count": 81215853,
      "files": [
        {
          "path": "pointcloud/Job_20250311_1043/Track01/Job_20250311_1043_Track01_001.las",
          "role": "authoritative",
          "order": 1,
          "point_count": 7703377,
          "bounds": {
            "min": [329485.497, 4153227.461, 32.586],
            "max": [330356.476, 4153962.381, 118.039]
          },
          "gps_time_min": 180000.000000,
          "gps_time_max": 181000.000000,
          "gps_week": 2357,
          "sha256": "1111111111111111111111111111111111111111111111111111111111111111"
        }
      ]
    }
  ]
}
```

위 수치와 hash는 구조 예시이며 실제 index에는 모든 파일의 실측값을 기록한다.

### 11.7 지주·표지 주변 점밀도와 지주 수직 연속성

전체 track의 평균 점밀도만으로는 가는 지주와 지면 교점을 보장할 수 없다. 공급자는 `qa/point_density_report.csv`에 전체 구간 통계와 객체별 QA 통계를 함께 제공한다. 객체별 통계는 `object_id`, `support_id`로 16.3절 ground-truth와 연결한다.

지주 통계는 다음 공통 측정창을 사용한다.

- 기준축: ground-truth 지주축. ground-truth가 없는 전수 통계에서는 공급자 추정축을 사용하되 `axis_source=SUPPLIER_ESTIMATE`로 표시한다.
- 축 지지영역: 기준축으로부터 수평거리 0.30 m 이내이며, 즉시 지지면에서 0.15 m 위부터 실제 관측 상단까지의 점이다.
- 수직 bin: 높이 0.15 m의 고정 bin을 사용한다. `occupied_bin`은 지주 후보점이 1개 이상 있는 bin이다.
- 내부 공백: 첫 occupied bin과 마지막 occupied bin 사이의 연속 empty bin 길이이다.
- 하단 가림 간격: 즉시 지지면 높이와 가장 낮은 유효 지주점 사이의 수직거리이다. 0.35 m를 초과하면 `base_visibility=OCCLUDED`로 기록한다.
- 지면 지지영역: 기준축에서 수평거리 0.24 m 초과 1.50 m 이하인 환형 영역을 0.25 m × 0.25 m 격자로 나눈다. 해당 객체의 실제 지지면을 표현하는 ground/road/보도/교통섬 점만 사용한다.

**[필수]** 객체별 행에는 최소 다음 값을 기록한다.

```text
object_id,support_id,job_id,track_id,range_to_sensor_m,axis_source,
pole_point_count,vertical_span_m,bin_height_m,total_bins,occupied_bins,
occupied_bin_ratio,max_internal_gap_m,ground_point_count,ground_grid_cells,
ground_angular_sectors,sign_face_area_m2,sign_face_point_count,
sign_face_density_ppm2,base_visibility,measurement_status
```

초기 수집·가공 하한은 아래와 같다. 이는 알고리즘이 계산을 시도할 수 있는 최소 조건이며 정확도 합격을 자동 보장하지 않는다. 계약 전 실증 표본으로 확정한 값은 16.2절의 필수 허용기준이 된다.

| 객체별 항목 | 초기 제안 하한 | 미달 시 처리 |
|---|---:|---|
| 지주 후보점 수 | 20점 이상 | `INSUFFICIENT_POLE_POINTS` |
| 관측 수직 span | 0.80 m 이상 | `INSUFFICIENT_VERTICAL_SPAN` |
| occupied 0.15 m bin | 5개 이상 | `INSUFFICIENT_VERTICAL_BINS` |
| 관측 span 내 occupied bin 비율 | 60% 이상 | `VERTICAL_DISCONTINUITY` |
| 최대 내부 공백 | 0.45 m 이하 | 초과 원인을 가림/저밀도로 구분 |
| 지면 0.25 m 격자 | 6 cell 이상 | `INSUFFICIENT_GROUND_SUPPORT` |
| 지면 방위 커버리지 | 8개 방위 sector 중 3개 이상 | 편측 관측으로 표시하고 자동 확정 금지 |
| 표지판 면 점밀도 | **[확인 필요]** 거리구간별 points/m² | 미달 표지는 영상 기반 탐지만 허용하고 3차원 면 추정 금지 |

공급자는 0–10 m, 10–20 m, 20–30 m, 30 m 초과의 센서 거리구간과 단주·복주·문형·캔틸레버·부착표지 유형별로 `count`, `min`, `p05`, `median`, `p95`를 보고한다. 평균만 제시할 수 없다. 비·안개·야간, 터널, 고속 주행 등 수집 조건별 저밀도 구간도 분리한다.

### 11.8 지면·노면·연석의 표현

지주 하단점은 지주축을 **그 지주가 실제로 설치된 즉시 지지면**과 교차시킨 점이다. 보도 또는 교통섬 위 지주를 인접 차도 높이까지 내리거나, 연석 아래의 가장 낮은 점을 지면으로 선택해서는 안 된다.

- **[필수]** 지주 반경 1.50 m 안의 자연지반, 차도, 보도, 중앙분리대, 교통섬 및 연석 상단은 원시 관측 밀도를 보존한다. 지면 분류나 thinning 때문에 지주 주변에 인위적인 hole을 만들 수 없다.
- **[필수]** 각 QA 지주의 `support_surface`를 `NATURAL_GROUND`, `ROAD_SURFACE`, `SIDEWALK`, `MEDIAN`, `TRAFFIC_ISLAND`, `CURB_TOP`, `STRUCTURE`, `UNKNOWN` 중 하나로 기록한다.
- **[필수]** class 2/11만으로 구분되지 않는 보도·교통섬·연석은 custom class 또는 QA 속성으로 의미를 보존한다. 모든 지표면을 일괄 class 2로 재분류했다면 원 분류와 변경 이력을 함께 제공한다.
- **[필수]** 연석이나 단차가 축 반경 1.50 m 안에 있고 높이차가 0.10 m 이상이면, 하단·상단 breakline과 해당 면의 종류를 `qa/ground_breaklines.gpkg`에 제공한다. 레이어 CRS는 납품 CRS와 같아야 한다.
- **[필수]** 식생, 주차 차량, 배수구, 맨홀, 지주 자체 점을 지면 plane에 포함하지 않는다. 제외 class와 필터 규칙을 기록한다.
- **[필수]** 한쪽만 관측된 연석, 차량에 가린 지면 또는 물고임처럼 지지면을 직접 확인할 수 없는 경우 `ground_visibility=PARTIAL/FULL_OCCLUSION`과 영향범위를 기록하고 확정 ground-truth로 사용하지 않는다.

`ground_breaklines.gpkg`는 최소 `object_id`, `feature_id`, `feature_type`, `elevation_role`, `source_method`, `survey_time`, `quality_status` 속성을 가진다. 단차가 없는 QA 객체는 파일에서 생략할 수 있지만 `pole_ground_truth.csv`에 `nearby_breakline=NONE`을 명시한다.

### 11.9 가림 구간과 멀티프레임 관측 연결

가림은 단순 누락으로 숨기지 않고 관측 상태로 납품한다. track 전체의 센서 차폐·데이터 중단 구간은 전수 기록하고, 객체 단위 가림은 모든 ground-truth/QA 표본과 공급자가 납품하는 표지 인벤토리 전체에 기록한다.

`qa/occlusion_intervals.csv`는 최소 다음 열을 가진다.

```text
interval_id,job_id,track_id,start_gps_week,start_gps_sow,end_gps_week,end_gps_sow,
affected_sensor,scope,object_id,visibility,reason,pointcloud_available,
alternate_observation_count,usable_for_pole_base,notes
```

- `visibility`는 `PARTIAL` 또는 `FULL`, `reason`은 `VEHICLE`, `VEGETATION`, `STRUCTURE`, `SIGN_PANEL`, `WEATHER`, `SENSOR_BLOCKAGE`, `DATA_GAP`, `OTHER` 중 하나를 사용한다.
- `FULL`은 후보 프레임 전체에서 지주축 또는 즉시 지지면을 직접 관측할 수 없는 상태이다. 이 구간에서 추정값을 실측 ground-truth처럼 제공할 수 없으며 결과를 `NOT_OBSERVABLE` 또는 `MANUAL_REVIEW`로 처리한다.
- 시작·종료시각은 8절과 같은 GPS week/SOW를 사용하며 영상명과 point GPS time으로 양방향 추적 가능해야 한다.

`qa/pole_observations.csv`는 동일 객체의 멀티프레임 연결표이며 최소 다음 열을 가진다.

```text
object_id,observation_id,support_id,job_id,track_id,image_path,gps_week,gps_sow,
sensor_id,distance_m,bearing_deg,sign_visibility,pole_visibility,base_visibility,
occlusion_reason,detection_usable,pole_base_usable,time_match_error_ms
```

- 같은 실물은 track이나 프레임이 달라도 안정적인 `object_id`를 사용하고, 각 영상 관측만 `observation_id`로 구분한다.
- 한 표지판이 둘 이상의 지주를 갖는 경우 각 축에 고유 `support_id`를 부여한다. 한 지주에 여러 표지판이 부착된 경우 표지별 `object_id`와 공통 `support_id`의 관계를 보존한다.
- 사용 가능한 각 관측은 영상 노출 중앙시각, pose, trajectory와 점군의 시간 일치 오차를 기록하고 16.2절 동기 기준을 만족해야 한다.
- **[권장]** 자동 위치 산출 대상으로 선언한 객체는 서로 다른 시점 또는 관측방향의 `pole_base_usable=true` 관측을 2개 이상 제공한다. 하나뿐이면 `SINGLE_VIEW_ONLY`, 하나도 없으면 `NOT_OBSERVABLE`로 표시한다.
- 완전가림 표본을 정확도 통계에서 조용히 제외할 수 없다. 정확도 분모에서는 별도 계층으로 분리하되, 전체 객체 대비 `observable`, `partial`, `full`, `not_processed` 비율을 함께 보고한다.

## 12. 수평·수직 좌표참조체계

### 12.1 수평 CRS

- **[필수]** 현재 사업 구간의 수평 CRS는 `WGS 84 / UTM zone 52N`, EPSG:32652이다.
- **[필수]** 축은 easting, northing 순서이고 단위는 metre이다.
- **[필수]** 영상 pose XYZ, trajectory XYZ, pointcloud XYZ, control point 및 결과 벡터가 동일 수평 CRS를 사용해야 한다.
- **[필수]** `crs/horizontal.wkt2`에 WKT2:2019 표현을 제공하고 manifest의 EPSG 코드와 일치시킨다.
- **[확인 필요]** WGS 84 realization과 coordinate epoch를 실제 GNSS 처리 결과에 맞춰 명시한다.

EPSG:32652는 수평 projected CRS만 식별하며 Z의 높이 종류를 결정하지 않는다. EPSG:32652만 기록하고 Z를 설명하지 않는 납품은 허용하지 않는다.

### 12.2 수직 CRS

공급자는 아래 둘 중 실제 처리와 일치하는 하나만 선택한다.

#### 선택 A: 타원체고

- `height_type=ellipsoidal`
- 필드 기호 `h`
- metre 단위
- 사용한 geodetic datum realization과 coordinate epoch 명시
- `geoid_model=null`
- WKT와 파일명에 EGM2008 height 또는 정표고라고 쓰지 않음

#### 선택 B: EGM2008 정표고

- `height_type=orthometric`
- 필드 기호 `H`
- vertical CRS `EGM2008 height`, EPSG:3855
- metre 단위
- 사용한 EGM2008 grid의 정확한 파일명·버전·해상도·checksum 명시
- geoid undulation `N`의 보간법과 적용 방향 명시
- `H = h - N` 관계로 변환 이력 제공

**[필수]** 단순히 `UTM52N_타원체고_EGM2008 height`처럼 타원체고와 EGM2008 정표고를 한 이름에 혼합할 수 없다.

**[필수]** LAS WKT, manifest, pose CSV의 Z, trajectory의 Z, control point의 Z가 모두 같은 높이 체계여야 한다. 다른 높이 체계 자료를 함께 제공할 경우 별도 파일로 분리하고 원본/변환본 역할 및 변환식을 명시한다.

### 12.3 현재 자료의 필수 확인 항목

공급자는 현재와 같은 TRK500Neo export를 납품하기 전에 다음 질문에 서면 답변해야 한다.

1. Sphere CSV 5열 Z는 타원체고 `h`인가, EGM2008 정표고 `H`인가?
2. LAS Z는 어느 높이이며, Sphere CSV/trajectory Z와 동일한가?
3. trajectory QA의 `H-Ell`은 실제 최종 solution에 사용된 높이인가?
4. 장비 로그의 `height`와 `displayheight`는 각각 어떤 datum인가?
5. EGM2008 변환을 적용했다면 어느 단계에서 어떤 grid와 보간법으로 적용했는가?
6. LAS VLR의 수직 WKT가 실제 좌표와 다르면 수정 export를 제공할 수 있는가?
7. 독립 기준점에서 타원체고와 정표고 중 어느 쪽이 일치하는가?

답변만으로 끝내지 않고, 최소 3개 이상의 분산된 검증점에 대해 원 GNSS 타원체고, geoid undulation, 변환 정표고 및 납품 Z를 비교한 `qa/control_points.csv`를 제공한다.

## 13. 최종 trajectory solution과 품질

### 13.1 최종 solution 식별

- **[필수]** 영상 pose와 점군 생성에 실제 사용한 최종 trajectory 하나를 `solution_id`로 식별한다.
- **[필수]** raw, forward, backward, optimized, adjusted, PCC-adjusted 등 여러 solution이 존재하면 처리 순서와 각 ID를 기록하고 `final=true`인 하나를 지정한다.
- **[필수]** 영상 pose, LAS/COPC, calibration snapshot 및 품질 보고서에 같은 최종 `solution_id`를 기록한다.
- **[필수]** trajectory 처리 소프트웨어·버전, 처리일, tightly/loosely coupled 여부, forward/backward 또는 multi-pass 여부를 기록한다.
- **[필수]** GNSS 보정원(base station/network RTK/PPP), 기준국 ID·좌표·안테나 정보, 사용 위성군, IMU profile, lever arm, smoothing 및 point-cloud adjustment 정보를 기록한다.

현재 자료에서 `opt1`, `PCCAdj` 계열이 함께 확인되므로, 이름이 가장 최신처럼 보인다는 이유로 발주자가 임의 선택하지 않도록 공급자가 최종본을 명시해야 한다.

### 13.2 표준 trajectory CSV

**[필수]** `trajectory.csv`에는 최소 다음 열을 제공한다.

```text
gps_week,gps_sow,x,y,z,qx,qy,qz,qw,
sigma_x_m,sigma_y_m,sigma_z_m,
sigma_roll_deg,sigma_pitch_deg,sigma_heading_deg,
solution_status,ambiguity_status,num_satellites,pdop,quality_class,solution_id
```

- 위치는 본 명세의 공통 CRS와 수직 datum을 사용한다.
- quaternion은 `q=[qx,qy,qz,qw]` 순서와 unit norm을 사용한다.
- orientation 방향은 manifest에 명시하며 권장값은 `body_to_world`이다.
- `solution_status`, `ambiguity_status`, `quality_class`의 enum과 의미를 문서화한다.
- 공분산을 제공할 수 있으면 표준편차 대신 또는 함께 covariance 열/별도 파일로 제공한다.
- 샘플링 주기, 최대 time gap, 보간법을 manifest에 기록한다.

### 13.3 영상별 quality 연결

**[필수]** 각 Sphere pose 시각에 해당하는 품질을 직접 제공하거나 trajectory 품질로부터 재현 가능하게 연결한다. `trajectory_quality.csv`에는 최소 다음이 있어야 한다.

- `gps_week`, `gps_sow_start`, `gps_sow_end`
- `quality_class`
- `position_sigma_horizontal_m`, `position_sigma_vertical_m`
- `attitude_sigma_roll_deg`, `attitude_sigma_pitch_deg`, `attitude_sigma_heading_deg`
- GNSS fix/float/single 또는 equivalent status
- 위성 수, PDOP
- outage 여부와 outage 지속시간
- 사용 가능 여부와 제외 사유

High/Medium/Low 같은 공급자 품질 등급만 제공해서는 안 된다. 각 등급의 수치 기준과 정확도 의미를 함께 제공해야 한다.

현재 샘플 보고서에는 High 9.29%, Medium 58.36%, Low 32.34%가 기록되어 있다. 이 비율은 자동 승인 기준이 아니며, Low 구간이 실제 표지 탐지 대상 구간에 포함되는지와 그 구간의 절대·상대 정확도를 공급자가 증명해야 한다.

## 14. 캘리브레이션 납품 및 이력

### 14.1 공통 필드

**[필수]** 최종 산출물 생성에 사용한 모든 calibration set을 `calibration/calibration.json`에 기록한다.

각 calibration record는 최소 다음 필드를 포함한다.

- `calibration_id`, `version`, `revision`
- 센서 종류, 제조사, 모델, 일련번호
- `calibrated_at_utc`, `valid_from_utc`, `valid_to_utc`
- calibration 수행기관과 방법
- pass/fail 상태 및 residual/RMSE
- 온도 또는 설치 조건이 유효성에 영향을 주는 경우 그 범위
- 값별 단위
- source frame, target frame, transform 방향
- 회전 표현과 저장 순서
- translation이 어느 frame에 표현되는지
- 적용 여부와 적용한 processing `solution_id`
- 원 보고서 경로와 SHA-256

### 14.2 transform 표현

모든 rigid transform은 4 × 4 homogeneous matrix를 row-major로 제공한다. 이름은 `T_target_from_source` 규약을 따른다.

```json
{
  "transform_id": "T_body_from_sphere",
  "source_frame": "sphere",
  "target_frame": "body_imu",
  "direction": "source_to_target",
  "storage": "row_major",
  "translation_unit": "m",
  "translation_expressed_in": "body_imu",
  "matrix": [
    1.0, 0.0, 0.0, 0.10,
    0.0, 1.0, 0.0, 0.20,
    0.0, 0.0, 1.0, 0.30,
    0.0, 0.0, 0.0, 1.0
  ]
}
```

위 수치는 형식 예시다. `Angles`, `Distance`, `Mounting`과 같은 공급자 고유 필드를 추가할 수 있지만 표준 행렬을 대신할 수 없다. degree, gon, radian, metre, millimetre를 혼용하지 않으며 각 값의 단위를 명시한다.

### 14.3 유효성

- **[필수]** 수집일은 모든 적용 calibration의 유효기간 안에 있어야 한다.
- **[필수]** 센서 탈부착, 충격, 수리, 펌웨어 변경 또는 재장착이 있었으면 재검정 여부와 영향평가를 기록한다.
- **[필수]** calibration 이후 현장 boresight 검증 결과를 제공한다.
- **[조건부]** 유효기간이 만료되었거나 사후 calibration을 소급 적용한 경우 발주자 승인 및 정량 검증 보고서가 필요하다.

## 15. 파일 무결성과 전송

- **[필수]** `checksums.sha256`에 자신을 제외한 모든 납품 파일의 SHA-256을 기록한다.
- **[필수]** 형식은 소문자 64자리 hex, 두 칸, 상대경로 순서로 한다.
- **[필수]** 상대경로는 `/`를 사용하고 중복 경로가 없어야 한다.
- **[필수]** checksum 계산은 압축 해제 후 실제 납품 파일 바이트를 대상으로 한다.
- **[필수]** 전송용 ZIP/TAR를 사용하는 경우 archive 자체의 SHA-256도 별도 전달한다.
- **[필수]** `checksums.sha256` 자체의 SHA-256은 이메일, 공문 또는 공급자 전자서명 등 납품 패키지 외부 채널로 전달한다.
- **[필수]** 재납품 시 `revision`을 증가시키고 변경 파일뿐 아니라 전체 manifest와 checksum을 다시 생성한다.

예시:

```text
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  manifest.json
bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  imagery/Job_20250311_1043/Track01/sphere/Job_20250311_1043_Track01_Sphere.csv
cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  imagery/Job_20250311_1043/Track01/sphere/Job_20250311_1043_Track01_Sphere_00001.jpg
```

## 16. 허용오차와 정량 검수 기준

### 16.1 즉시 적용할 형식·일관성 기준

| 검사항목 | 허용오차/합격 기준 | 수준 |
|---|---:|---|
| SHA-256 | 전 파일 100% 일치 | 필수 |
| manifest 참조 파일 | 누락 0건, 미참조 필수 파일 0건 | 필수 |
| JPEG 디코딩 | 100% 성공 | 필수 |
| JPEG 크기 | sidecar/manifest와 정확히 일치 | 필수 |
| pose-영상 대응 | 양방향 1:1, 누락·중복 0건 | 필수 |
| pose CSV 열 수 | 모든 데이터 행 정확히 17열 | 필수 |
| GPS SOW | `[0,604800)` 및 track 내 시간 순서 정상 | 필수 |
| GPS→UTC 교차검증 | 기록 정밀도 이내 일치, 권장 1 μs | 필수 |
| 행렬 직교성 | `max(abs(RᵀR-I)) ≤ 1e-5` | 필수 |
| 행렬 determinant | `abs(det(R)-1) ≤ 1e-5` | 필수 |
| 축 관계 | `max(abs(cross(forward,up)-right)) ≤ 1e-6` | 필수 |
| OPK 재구성 | 자세 각 차이 ≤ 0.001 gon | 필수 |
| CRS | pose, trajectory, 점군, QA 자료가 의미상 동일 | 필수 |
| LAS/COPC header | 버전, point format, WKT, global encoding이 manifest와 일치 | 필수 |
| LAS 좌표 scale | 축별 ≤ 0.001 m 또는 승인된 정확도 이내 | 필수 |
| point count | header/index/실제 reader count 정확히 일치 | 필수 |
| authoritative 분할 | 이중 집계 0건 또는 선언된 overlap 규칙과 정확히 일치 | 필수 |
| calibration 유효기간 | 수집시각을 포함 | 필수 |
| solution ID | 영상·궤적·점군·QA 전부 동일 | 필수 |

### 16.2 계약 전에 확정할 성능 기준

아래 수치는 표지 위치 산출용 초기 제안값이다. 사업 정확도 등급, 주행속도, 기준점 품질과 표지 크기를 반영해 계약 전에 확정한다. 확정값은 **[필수]** 검수 기준이 된다.

| 검사항목 | 초기 제안값 | 상태 |
|---|---:|---|
| 독립 검사점 수평 RMSE | ≤ 0.05 m | 확인 필요 |
| 독립 검사점 수직 RMSE | ≤ 0.10 m | 확인 필요 |
| 독립 검사점 수평 95% 오차 | ≤ 0.10 m | 확인 필요 |
| 독립 검사점 수직 95% 오차 | ≤ 0.20 m | 확인 필요 |
| 영상 ray-점군 기준표적 재투영 | median ≤ 3 px, 95% ≤ 5 px | 확인 필요 |
| Sphere Front/Rear 동기 오차 | ≤ 2 ms | 확인 필요 |
| trigger-to-exposure 보정 불확도 | ≤ 1 ms | 확인 필요 |
| 영상 pose-최종 trajectory 재계산 위치차 | ≤ 0.02 m | 확인 필요 |
| 영상 pose-최종 trajectory 재계산 자세차 | ≤ 0.01° | 확인 필요 |
| 사용구간 trajectory time gap | ≤ 0.10 s | 확인 필요 |
| 사용구간 Low/invalid 품질 비율 | ≤ 5%, 대상 구간 0% 권장 | 확인 필요 |
| 지주 점 수·수직 연속성·지면 격자 | 11.7절 초기 하한 이상 | 확인 필요 |
| 표지판 면 유효 점밀도 | 거리구간별 points/m² 및 최소 점 수로 확정 | 확인 필요 |
| 지주축-지면 교점 수평 RMSE / 95% | ≤ 0.10 m / ≤ 0.20 m | 확인 필요 |
| 지주축-지면 교점 수직 RMSE / 95% | ≤ 0.10 m / ≤ 0.20 m | 확인 필요 |
| 지주축-지면 교점 3D 최대오차 | ≤ 0.50 m, 초과 0건 | 확인 필요 |
| 지주축 방향 95% 각도오차 | ≤ 2.0° | 확인 필요 |
| 교점 위치의 지지면 높이 95% 오차 | ≤ 0.10 m | 확인 필요 |
| 관측 가능 QA 객체 자동 산출 성공률 | ≥ 95% | 확인 필요 |
| 분할 경계 의도치 않은 중복률 | 0% | 확인 필요 |

정확도 검증점은 수집 궤적과 독립된 측량으로 취득하고, 도로 전 구간과 고도 범위에 분산한다. 평균값만으로 합격 처리하지 않으며 RMSE, median, 95 percentile, maximum, outlier 제외 규칙과 제외 전후 통계를 함께 보고한다.

### 16.3 독립 검사점과 지주 유형별 ground-truth

`qa/control_points.csv`와 `qa/pole_ground_truth.csv`는 영상 pose·trajectory·점군 생성이나 strip/PCC 조정에 사용하지 않은 독립 측량값이어야 한다. 같은 LAS에서 점을 수동 선택하거나, 납품 MMS 궤적으로 다시 계산한 좌표는 독립 ground-truth가 아니다.

독립 검사는 검교정 이력이 있는 total station, network RTK/PPK GNSS 또는 그보다 높은 정확도의 방법으로 수행한다. 장비, 기준점망, 측량일, 작업자, 원시 관측 파일, 조정 보고서, 좌표계와 수직 datum을 추적할 수 있어야 한다. ground-truth 자체의 95% 불확도 초기 상한은 수평 0.03 m, 수직 0.05 m이며 계약 전 최종 확정한다.

`qa/control_points.csv`는 최소 다음 열을 가진다.

```text
check_id,job_id,track_id,check_type,x_gt,y_gt,z_gt,crs_id,vertical_datum,
survey_method,equipment_id,calibration_certificate_id,survey_utc,
sigma_x_m,sigma_y_m,sigma_z_m,used_in_mms_adjustment,source_report
```

- `used_in_mms_adjustment`는 반드시 `false`여야 한다.
- 초기 표본 수는 전체 `max(20, ceil(노선연장_km))`점 이상이고, track마다 5점 이상이다. 시작·중간·끝, 좌우 도로변, 고도 범위와 GNSS 양호/불량 환경에 분산한다.
- 검사점은 이동 가능 물체, 차선도색의 불명확한 끝점, 식생처럼 재측정이 불안정한 위치를 사용하지 않는다.

`qa/pole_ground_truth.csv`는 **지지축 1개당 1행**이며 최소 다음 열을 가진다.

```text
object_id,support_id,structure_type,support_role,linked_sign_ids,job_id,track_id,
base_x_gt,base_y_gt,base_z_gt,axis_unit_x_gt,axis_unit_y_gt,axis_unit_z_gt,
support_surface,nearby_breakline,ground_plane_a,ground_plane_b,ground_plane_c,ground_plane_d,
survey_method,survey_utc,sigma_xy_m,sigma_z_m,visibility,occlusion_reason,
ground_truth_status,source_report
```

`ground_plane_a`~`d`는 `a*x + b*y + c*z + d = 0`의 계수이며 `(a,b,c)`는 단위벡터, `c >= 0`으로 정규화한다. `linked_sign_ids`에 여러 값이 있으면 세미콜론으로 구분하고 그 규칙을 README에 기록한다.

구조 유형과 하단점 정의는 다음과 같다.

| `structure_type` | ground-truth 작성 규칙 |
|---|---|
| `SINGLE`(단주) | 1개 지주축과 즉시 지지면의 교점 1개 |
| `DOUBLE`(복주) | 좌·우 지주축 각각 1행. 두 하단점의 중점은 별도 파생값이며 개별 하단점을 대체할 수 없음 |
| `GANTRY`(문형) | 도로 양측 또는 다수 column 각각 1행. `support_role=GANTRY_COLUMN_n` 사용 |
| `CANTILEVER`(캔틸레버) | 수직 column의 축-지면 교점을 기록. 수평 arm 끝을 하단점으로 사용하지 않음 |
| `ATTACHED_TO_POLE`(기존 지주 부착표지) | 표지 자체의 새 지주를 만들지 않고 host 지주의 `support_id`에 연결 |
| `ATTACHED_NO_GROUND_SUPPORT`(벽·교량 등 부착표지) | 지면 지지축이 없으므로 좌표를 공란으로 두고 `ground_truth_status=NOT_APPLICABLE`. 가상의 하단점을 생성하지 않음 |

교점은 지주 중심축이 플랜지·기초·보도·교통섬 등 실제 즉시 지지면과 만나는 점이다. 원통 지주는 단면 중심, 사각 지주는 대각선 교점, 복합 지주는 주 구조축을 사용한다. 지중 매입 또는 커버로 물리 교점을 직접 측량하지 못하면 지주의 서로 떨어진 두 높이 이상에서 축을 측량하고 지지면까지 외삽하며 `survey_method=AXIS_EXTRAPOLATION`과 불확도를 기록한다.

유형별 초기 QA 표본은 다음과 같다. 실제 수량이 최소 표본보다 적으면 해당 유형을 전수 조사하고, 유형이 전혀 없으면 `0건`과 확인 근거를 회신한다.

| 유형 | 초기 최소 객체 수 |
|---|---:|
| 단주 | 20 |
| 복주 | 10 구조물, 모든 지지축 |
| 문형 | 5 구조물, 모든 column |
| 캔틸레버 | 5 구조물 |
| 부착표지 | 10 구조물 |

표본은 11.7절 거리구간, 좌·우측 설치, 평탄면/연석/보도/교통섬, 직립/경사, 부분가림을 층화한다. 각 유형에서 실제로 존재하는 부분가림 사례를 최소 20% 포함하는 것을 원칙으로 한다. 완전가림 객체는 독립 현장측량 ground-truth를 가질 수 있으나 좌표 정확도 통계가 아니라 관측 가능률과 미검출률 평가 계층으로 분리한다.

각 QA 객체의 `qa/pole_samples/<object_id>/`에는 최소 2개 대표 Sphere crop, 축과 지지면이 보이는 pointcloud subset(LAZ), 판독 overlay, 현장사진 또는 측량 스케치를 제공한다. 원본 파일명, GPS 시각과 원본 point index를 잃지 않아야 한다.

### 16.4 축-지면 교점 오차 산식과 판정

추정 교점을 `P=(x,y,z)`, 독립 ground-truth를 `P_gt=(x_gt,y_gt,z_gt)`라 할 때 다음 값을 객체별로 계산한다.

```text
e_xy = sqrt((x-x_gt)^2 + (y-y_gt)^2)
e_z  = abs(z-z_gt)
e_3d = sqrt(e_xy^2 + e_z^2)
```

축 방향 각도오차는 추정 단위축 `u`와 ground-truth 단위축 `u_gt`에 대해 축의 부호를 무시하도록 `acos(abs(dot(u,u_gt)))`로 계산한다. 지지면 높이오차는 ground-truth 교점의 `(x_gt,y_gt)`에서 추정 지면 plane과 독립 지지면의 Z 차이로 계산한다.

- 16.2절의 RMSE와 95% 기준을 **동시에** 만족해야 한다. 3D 최대오차 상한을 넘는 객체가 하나라도 있으면 원인분석과 재처리 전에는 합격 처리하지 않는다.
- 단주·복주·문형·캔틸레버, 거리구간, 지지면 종류와 가림상태별 통계를 각각 보고한다. 전체 통계로 특정 유형의 실패를 숨길 수 없다.
- 복주·문형은 support별 교점을 먼저 판정한다. 필요 시 산출하는 구조물 대표점은 모든 유효 support 하단점의 명시된 규칙(예: XY 중점)으로 계산하고 별도 통계를 낸다.
- `visibility=FULL` 또는 `ground_truth_status=NOT_APPLICABLE`은 좌표오차 분모에서 제외하되, 전체 건수와 제외 사유를 정확히 보고한다. `PARTIAL`은 별도 통계와 전체 통계에 모두 포함한다.
- outlier 제외는 측량 오류가 독립 증거로 확인된 경우만 허용한다. 알고리즘 실패나 저밀도·가림을 이유로 제외할 수 없다.

## 17. 자동검수 체크리스트

공급자는 납품 전에 아래 항목을 자동 실행하고 `qa/validation_report.json`에 항목별 `pass/fail`, 측정값, 기준값, 검사 프로그램 버전 및 실행시각을 저장한다.

### 17.1 패키지와 manifest

- [ ] `manifest.json`이 UTF-8 JSON으로 파싱된다.
- [ ] schema version이 지원 버전이다.
- [ ] 상대경로에 절대경로, `..`, 대소문자 충돌이 없다.
- [ ] 모든 참조 파일이 존재하고 파일 크기가 0보다 크다.
- [ ] `checksums.sha256`의 SHA-256이 전부 일치한다.
- [ ] job/track ID와 파일 경로가 일관된다.
- [ ] 재납품 revision과 변경 이력이 기록되어 있다.
- [ ] classification map, 점밀도 보고서, 가림 목록, 관측 연결표와 지주 ground-truth 경로가 manifest에 등록되어 있다.

### 17.2 영상, sidecar 및 pose

- [ ] 모든 JPEG가 끝까지 디코딩된다.
- [ ] JPEG 실제 크기, manifest 크기, `ImageSize`가 일치한다.
- [ ] sidecar의 전체 Sphere 범위가 `[-180,180]`, `[-90,90]`이다.
- [ ] `PanoramaHotSpot=0,0`이거나 승인된 명시 규약이 있다.
- [ ] CSV 각 행이 정확히 17열이고 모든 숫자가 유한수이다.
- [ ] 영상과 pose가 1:1이며 순번 누락/중복이 `known_gaps`와 일치한다.
- [ ] GPS SOW 범위, week, UTC 변환 및 시간 단조성이 정상이다.
- [ ] trigger latency와 적용 여부가 선언되어 있다.
- [ ] 모든 R의 직교성, determinant와 축 관계가 허용오차를 만족한다.
- [ ] OPK와 R의 교차검증이 선언된 규약에서 통과한다.
- [ ] 영상 중앙 ray가 `-R[:,2]`, 영상 상단이 `R[:,1]` 방향인지 기준 영상으로 확인한다.
- [ ] EO 기준점과 lever arm 적용 여부가 확정값이다.
- [ ] `pole_observations.csv`의 영상명·GPS week/SOW가 pose CSV 및 point GPS time과 일치한다.
- [ ] 같은 실물의 `object_id`와 지지축의 `support_id`가 프레임·track 사이에서 유지된다.
- [ ] track 전체 센서 차폐/데이터 gap 및 QA 객체의 부분·완전가림이 `occlusion_intervals.csv`에 기록되어 있다.

### 17.3 점군과 공간 index

- [ ] LAS/LAZ/COPC 모든 파일이 표준 reader로 열린다.
- [ ] LAS version, point format, dimensions, scale, offset이 manifest와 일치한다.
- [ ] XYZ, RGB, intensity, GPS time, classification 값 범위가 유효하다.
- [ ] WKT VLR과 LAS Global Encoding bit가 규격에 맞다.
- [ ] point GPS time encoding과 GPS week가 명시되어 있다.
- [ ] RGB 16-bit scaling 및 무색점 정책이 선언과 일치한다.
- [ ] LAS에 실제 등장하는 모든 classification 코드가 `classification_map.csv`에 있고 파일·track별 의미가 동일하다.
- [ ] class 2/11/3/4/5의 의미와 보도·연석·교통섬의 포함·제외 기준이 명시되어 있다.
- [ ] custom 지주 class의 승인 ID, 생성 버전, 포함·제외 규칙과 QA precision/recall이 기록되어 있다.
- [ ] class `84`는 공식 승인 map이 없으면 지주 class로 사용되지 않는다.
- [ ] header, 공간 index, 실제 읽기 point count가 일치한다.
- [ ] 각 파일의 실제 min/max가 index bounds 안에 있다.
- [ ] authoritative set만 합산한 unique point count가 manifest와 일치한다.
- [ ] 전체본·분할본 중복 역할이 명시되어 이중 집계되지 않는다.
- [ ] COPC hierarchy 또는 외부 spatial index가 정상 검색된다.
- [ ] 지주 후보점 수, 0.15 m bin 연속성, 내부 공백과 하단 가림 간격이 객체별로 계산되어 있다.
- [ ] 지주 주변 지면의 0.25 m 격자 수와 방위 sector가 하한을 만족하거나 명시적 실패 상태이다.
- [ ] 표지판 면 점밀도가 거리·구조유형별로 보고되어 있으며 평균 외 `min/p05/median/p95`가 있다.
- [ ] 지주 반경 1.50 m의 지면·노면·연석 점이 보존되고, 단차 대상에는 `ground_breaklines.gpkg`가 있다.

### 17.4 CRS, trajectory 및 calibration

- [ ] 수평 CRS가 EPSG:32652이며 축과 단위가 올바르다.
- [ ] 수직 datum이 타원체고 또는 EGM2008 정표고 중 하나로 확정되어 있다.
- [ ] WKT에 타원체고/EGM2008 모순이 없다.
- [ ] pose, trajectory, pointcloud, control point 좌표 범위가 상호 겹친다.
- [ ] GPS week와 leap-second 정보가 모든 센서에서 동일하다.
- [ ] 최종 `solution_id`가 모든 산출물에서 동일하다.
- [ ] 각 영상 시각에 trajectory와 quality가 존재하거나 승인된 보간 범위 안에 있다.
- [ ] trajectory의 quaternion norm, 시간 순서와 gap이 정상이다.
- [ ] calibration sensor serial이 실제 수집 장비 serial과 일치한다.
- [ ] 수집일이 calibration 유효기간에 포함된다.
- [ ] 모든 transform의 source/target, 단위, 방향 및 역행렬 검사가 정상이다.
- [ ] 독립 기준점 정확도와 영상-점군 재투영 오차가 확정 기준을 만족한다.
- [ ] 독립 검사점의 `used_in_mms_adjustment=false`, 측량 불확도, 장비 검교정과 원시 관측 추적성이 확인된다.

### 17.5 지주 하단점 QA와 납품 완결성

- [ ] `pole_ground_truth.csv`가 지지축 1개당 1행이며 좌표계·수직 datum이 점군과 일치한다.
- [ ] 단주·복주·문형·캔틸레버·부착표지의 실제 수량과 QA 표본 수가 보고되어 있다.
- [ ] 복주와 문형의 모든 지지축에 서로 다른 `support_id`가 있다.
- [ ] 부착표지는 host 지주 연결 또는 `NOT_APPLICABLE`로 처리되며 가상 하단점이 없다.
- [ ] 하단점이 차도 최저점이 아니라 실제 즉시 지지면과 축의 교점으로 측량되어 있다.
- [ ] 평탄면·보도·교통섬·연석/단차·부분가림 표본이 포함되어 있다.
- [ ] 각 QA 객체의 Sphere crop, pointcloud subset, overlay와 측량 증빙이 원본 시각·파일로 추적된다.
- [ ] `FULL`, `PARTIAL`, `NOT_OBSERVABLE`, `NOT_APPLICABLE` 건수가 전체 분모와 함께 보고되어 있다.
- [ ] 축-지면 교점의 `e_xy`, `e_z`, `e_3d`, 축 각도 및 지지면 높이오차가 유형별로 계산되어 있다.
- [ ] RMSE, median, 95 percentile, maximum과 제외 전후 통계가 16.2절 확정 허용오차를 만족한다.
- [ ] 저밀도·가림·알고리즘 실패를 outlier로 제외하지 않았으며 제외 건마다 독립 증빙이 있다.

## 18. 검수 결과와 재납품

다음은 중대 오류로 분류하며 해당 track 또는 전체 납품을 반려한다.

- checksum 불일치 또는 파일 손상
- 영상과 pose의 대응 누락
- GPS week/time scale/leap second/latency를 확정할 수 없음
- `R`의 방향 또는 축 정의가 본 명세와 다름
- EO 기준점 또는 lever arm 적용 여부가 불명확함
- 수직 datum이 타원체고와 EGM2008 사이에서 모순됨
- 잘못된 CRS WKT 또는 좌표 단위
- 영상과 점군이 서로 다른 trajectory solution으로 생성됨
- authoritative 점군 분할을 식별할 수 없어 중복 집계 가능성이 있음
- calibration 일련번호·버전·유효기간 또는 transform 방향이 누락됨
- LAS에 존재하는 class가 공식 map에 없거나 같은 class ID의 의미가 파일·track별로 다름
- 공식 승인과 QA 없이 class `84` 또는 다른 custom ID를 교통표지 지주로 표시함
- ground/road/vegetation/pole의 포함·제외 기준이 없거나 연석·보도·교통섬 의미를 확인할 수 없음
- 지주 점 수·수직 연속성·지면 격자 커버리지가 확정 하한에 미달하면서 가림/미수집 상태로 선언되지 않음
- 지면 분류 또는 thinning으로 지주 주변 지지면·연석이 소실됨
- 완전가림·데이터 gap이 누락되거나 멀티프레임 객체 연결 및 시각이 원본과 맞지 않음
- 단주·복주·문형·캔틸레버·부착표지의 실제 수량 또는 필수 QA 표본과 독립 ground-truth가 누락됨
- 동일 MMS 자료에서 만든 좌표를 독립 검사점 또는 ground-truth라고 제출함
- 지주축-지면 교점을 실제 즉시 지지면이 아닌 인접 차도·최저점 또는 가상 위치로 정의함
- 필수 정확도 검사가 확정 허용오차를 초과함

재납품 시 공급자는 다음을 제공한다.

1. 증가된 `revision`의 전체 manifest
2. 수정 사유와 영향 범위
3. 전체 파일에 대한 새 checksum
4. 재실행한 validation report
5. 이전 납품과의 파일 단위 변경 목록

## 19. 공급업체 회신이 필요한 확인사항

계약 체결 전에 아래 값을 표로 회신하고 최종 manifest schema에 반영한다. 회신표에는 각 항목별 `응답값`, `근거 파일/보고서`, `담당자`, `확정 여부`, `예외 track`을 포함한다. 단순히 "제조사 기본값" 또는 "LAS 표준"이라고만 답할 수 없다.

1. Sphere CSV XYZ의 정확한 EO 기준점과 lever arm 적용 여부
2. Sphere CSV R의 local/world 방향 및 축 정의 확인
3. OPK의 축, 순서, 부호, intrinsic/extrinsic 규약
4. GPS week, time scale, GPS-UTC 차이 및 leap-second source
5. trigger timestamp와 노출 중앙시각의 차이, 적용 여부 및 불확도
6. Front/Rear 동기 방식과 최대 동기 오차
7. 최종 trajectory solution 이름, 처리 단계와 `solution_id`
8. trajectory quality 등급의 수치 정의 및 Low 구간 처리방안
9. 수평 CRS의 WGS 84 realization과 coordinate epoch
10. Sphere CSV, trajectory, LAS 각각의 Z가 타원체고인지 EGM2008 정표고인지
11. EGM2008 적용 시 grid 버전, checksum, 보간법 및 변환 방향
12. LAS Global Encoding의 WKT/GPS time bit와 실제 값의 일치 여부
13. 점군 RGB 생성 방식과 GPS time encoding
14. 적용 LAS/ASPRS 버전과 실제 등장하는 모든 classification ID의 공식 map
15. class 2 `Ground`, class 11 `Road Surface`, class 3/4/5 vegetation의 사용 여부와 보도·연석·교통섬·중앙분리대 포함·제외 기준
16. 현재 샘플에서 관찰된 class `84`의 공식 명칭·정의·생성 방법·적용 범위. 지주 코드가 아니라면 실제 의미를 명시
17. 교통표지 지주 custom class ID, 가로등·전신주·신호등주·수목·가드레일과의 구분 규칙 및 precision/recall QA 결과
18. 전체 LAS와 번호 분할 LAS의 중복 관계 및 authoritative 집합
19. 거리구간별 지주점 수, 수직 span, 0.15 m bin 연속성, 최대 내부 공백과 표지판 면 points/m²의 보장값 및 실측 `p05/median/p95`
20. 지주 반경 1.50 m 지면점의 0.25 m 격자/방위 커버리지 보장값과 분류·thinning 전후 보존 방법
21. 지주가 보도·교통섬·중앙분리대·연석에 있을 때 즉시 지지면 결정 규칙, 연석 breakline 제공 방식
22. 부분가림·완전가림·센서 차폐·데이터 gap의 판정 규칙, 구간 수와 `occlusion_intervals.csv` 생성 방식
23. 동일 객체의 멀티프레임 `object_id`와 다중 지주의 `support_id` 부여 규칙, 영상-pose-pointcloud 시간 연결 오차
24. 수집 구간의 단주·복주·문형·캔틸레버·부착표지 실제 수량과 유형별 QA/ground-truth 제공 수량
25. 독립 검사점 및 축-지면 교점 ground-truth의 측량 방법, 장비 검교정, 예상 95% 불확도, MMS 조정 미사용 증빙
26. 복주·문형의 개별 지지축 및 대표점 정의, 부착표지의 host 연결/`NOT_APPLICABLE` 처리 방식
27. 완전가림과 저밀도 객체의 자동 산출 여부, `NOT_OBSERVABLE`/`MANUAL_REVIEW` 처리 및 정확도·관측 가능률 분모 규칙
28. 카메라·IMU·LiDAR calibration ID, 버전, 유효기간, 단위와 transform 방향
29. 16.2절의 사업별 정확도·동기·점밀도·수직 연속성·축-지면 교점 허용오차 최종값

위 항목은 제조사 내부 DB 사본만 전달하는 것으로 갈음할 수 없다. 공급자는 납품자가 아닌 제3자도 본 명세의 표준 파일만으로 같은 좌표, 시각, 방향 및 품질 판단을 재현할 수 있도록 명시적으로 export해야 한다.
