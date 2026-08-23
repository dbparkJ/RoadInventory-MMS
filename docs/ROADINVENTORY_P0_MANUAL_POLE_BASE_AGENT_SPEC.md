# RoadInventory-MMS P0 구현 지시서
## 클릭 시드 기반 지주 바닥점 자동 산출 + AI 미검출 수동 구축 워크플로

> 대상 저장소: `dbparkJ/RoadInventory-MMS`  
> 기준 브랜치: `main`  
> 문서 목적: AI 에이전트가 추가 질의 없이 바로 구현할 수 있는 단일 실행 명세  
> 최우선 목표: 작업자가 LAS 점군에서 **지주의 어느 한 점이든 클릭**하면, 서버가 해당 지주의 축과 설치 지면을 추정하여 **지주 맨 아래 바닥점(PointZ)** 을 계산하고 신규 또는 기존 SHP 피처에 저장한다.

---

# 0. AI 에이전트 실행 규칙 — 토큰·테스트 비용 제한

이 절은 구현보다 우선한다. 에이전트는 아래 규칙을 지켜야 한다.

## 0.1 작업 범위 제한

1. 이번 작업은 **P0 지주 바닥점 도구**만 구현한다.
2. 차선 추출, 전체 UI 재설계, 데이터베이스 교체, 신규 AI 모델 학습, 전체 QA 플랫폼 구축은 하지 않는다.
3. 관련 없는 파일 정리, 대규모 리팩터링, 포매팅 전면 적용, 의존성 업그레이드는 하지 않는다.
4. 기존 자동 검출 파이프라인의 결과가 바뀌지 않도록 한다.
5. 기존 `find_pole_bases()`에 클릭점을 가짜 표지판 위치로 넣는 식의 우회 구현은 금지한다. 클릭 시드 전용 진입점을 만든다.

## 0.2 저장소 탐색 제한

처음부터 저장소 전체를 반복해서 읽지 않는다. 다음 파일만 우선 확인한다.

1. `mms_shp_detection/pole.py`
2. `mms_shp_detection/pointcloud.py`
3. `mms_shp_detection/webapp/media.py`
4. `mms_shp_detection/webapp/overlays.py`
5. `mms_shp_detection/webapp/app.py`
6. `webui/src/views/PointCloudView.tsx`
7. `webui/src/components/OverlayContext.tsx`
8. `webui/src/components/OverlayPanel.tsx`
9. `webui/src/lib/api.ts`
10. `webui/src/types.ts`
11. 관련 테스트 파일만 확인한다.

심볼 검색 후 필요한 줄 범위만 읽는다. 큰 파일 전체를 여러 번 출력하지 않는다.

## 0.3 테스트 토큰 예산

### 금지

- 작업 시작 직후 전체 `pytest` 실행 금지
- 작업 시작 직후 전체 `npm test` 실행 금지
- 동일한 성공 테스트 반복 실행 금지
- 수백 MB 이상의 LAS 테스트 데이터 생성 금지
- 테스트 로그 전체를 대화에 붙여 넣는 행위 금지
- 경고와 스택트레이스를 무제한 출력하는 행위 금지

### 허용되는 테스트 라운드

최대 **3개 그룹**으로 실행한다. 실패 시 해당 실패 파일 또는 실패 테스트만 다시 실행하고, 성공한 그룹은 반복하지 않는다.

#### 그룹 A — 순수 알고리즘과 API

```bash
python -m pytest -q \
  tests/test_manual_pole_base.py \
  tests/test_webapp_pole_tools.py \
  --tb=short --maxfail=1
```

#### 그룹 B — 프런트엔드 관련 테스트

```bash
cd webui
npm test -- \
  src/views/PointCloudView.test.ts \
  src/components/OverlayContext.test.tsx \
  src/components/OverlayPanel.test.tsx \
  --reporter=dot
```

#### 그룹 C — 최종 영향 범위 회귀와 빌드

백엔드 공유 로직을 수정한 경우에만 다음을 실행한다.

```bash
python -m pytest -q \
  tests/test_pole.py \
  tests/test_pole_accuracy_regressions.py \
  tests/test_pointcloud.py \
  tests/test_webapp_overlays.py \
  -k "pole or ground or point or overlay" \
  --tb=short --maxfail=1
```

프런트엔드는 빌드를 한 번만 실행한다.

```bash
cd webui
npm run build
```

### 테스트 작성 원칙

- 합성 점군을 이용한 table-driven 테스트를 사용한다.
- 파라미터 하나마다 테스트 하나씩 만들지 않는다.
- 정상, 경사, 가림, 모호성, 지면 없음 등 핵심 시나리오만 묶어서 검증한다.
- API 테스트에서는 실제 대형 LAS 대신 mock catalog와 mock `PointCloudReaderCache`를 사용한다.
- UI 테스트는 네트워크 응답을 mock하고 상태 전이만 검증한다.
- 테스트 실패 출력은 마지막 핵심 오류만 요약한다.

## 0.4 구현 종료 조건

아래가 충족되면 작업을 종료한다.

- 순수 알고리즘 테스트 통과
- API 테스트 통과
- 신규 생성과 기존 피처 재산출 UI 흐름 통과
- 프런트엔드 빌드 통과
- 기존 일반 포인트 선택 기능 `P`가 깨지지 않음
- `git diff --check` 통과

전체 저장소 테스트는 사용자가 별도로 요청하지 않는 한 실행하지 않는다.

## 0.5 최종 보고 형식

에이전트의 최종 응답은 아래 네 항목만 짧게 남긴다.

1. 변경 파일
2. 구현된 사용자 흐름
3. 실제 실행한 테스트 명령과 성공 여부
4. 남은 제약사항

전체 테스트 로그나 코드 전체를 응답에 복사하지 않는다.

---

# 1. 제품 방향

## 1.1 기존 제품과 본 프로젝트의 차이

본 프로젝트는 작업자가 처음부터 모든 도로 객체를 직접 도화하는 제품이 아니다.

```text
AI 자동 검출·3D 위치화
        ↓
중복 제거·SHP 생성
        ↓
작업자가 누락·오류만 빠르게 후처리
        ↓
검수 및 납품
```

따라서 수동 기능의 목적은 “전체 구축 도구”가 아니라 다음 두 가지다.

1. AI가 놓친 지주를 빠르게 신규 생성
2. AI가 생성했지만 하단점이 부정확한 지주를 빠르게 재산출

작업자에게 요구할 입력은 최소화한다.

> 작업자는 “이 점이 해당 지주의 몸체에 속한다”는 의미만 제공한다.  
> 지주의 중심축, 지면 높이, 하단점은 서버가 계산한다.

## 1.2 P0 핵심 사용자 가치

- 지주의 밑부분이 차량·수풀·가드레일에 가려져 있어도 중간 또는 상단의 몸체를 클릭할 수 있어야 한다.
- 작업자는 바닥점을 직접 찾거나 정확하게 클릭할 필요가 없다.
- 계산 결과를 저장 전에 확인할 수 있어야 한다.
- 실패하거나 모호할 때 잘못된 점을 자동 저장하면 안 된다.
- 미검출 지주를 연속으로 추가할 때 클릭 수를 최소화해야 한다.

---

# 2. 현재 저장소에서 재사용할 기반

현재 저장소에는 필요한 기반이 상당 부분 존재한다. 새 시스템을 별도로 만들지 말고 아래 요소를 재사용한다.

## 2.1 점군 읽기

`mms_shp_detection/pointcloud.py`

- `build_pointcloud_catalog()`
- `match_nearest_pointcloud_files()`
- `PointCloudReaderCache.read_block_records()`
- LAS/PCDB 공통 블록 접근
- `classification`, `gps_time`, `return_number`, `source_index` 등 LAS 속성
- 디코딩 블록 LRU 캐시

중요: 지주 바닥점 계산은 브라우저의 MMSP 미리보기 샘플이 아니라 **서버의 원본 블록 레코드**를 사용해야 한다.

## 2.2 기존 지주·지면 알고리즘

`mms_shp_detection/pole.py`

이미 다음 개념이 구현되어 있다.

- 높이 구간별 median을 이용한 축 fitting
- 축 기울기 및 RMSE 검증
- 수직 연속성 검증
- 거의 수직인 축의 plumb 안정화
- LAS 지면 class와 geometry 기반 지면 추정
- 국지 지면 plane fitting
- 지주 축과 지면의 교차점 산출

이번 기능은 이 수학적 primitive를 재사용하되, 기존 함수의 “표지판 검출 위치에서 지주를 찾는” 진입 로직과 분리한다.

## 2.3 기존 웹 편집 기능

- `PointCloudView.tsx`에는 실제 표시 점을 선택하는 picking 로직이 있다.
- `OverlayContext.tsx`에는 신규 Point 생성과 기존 Point 이동 상태가 있다.
- `OverlayPanel.tsx`에는 Point 레이어 신규 포인트와 속성 편집 UI가 있다.
- `overlays.py`에는 SHP 편집본의 생성·수정·삭제, revision, optimistic concurrency가 있다.

따라서 신규 기능은 다음처럼 연결한다.

```text
3D 점군에서 지주 몸체 클릭
        ↓
클릭한 실제 점의 dataset XYZ
        ↓
신규 read-only 추론 API
        ↓
바닥점·축·지면·품질 결과 반환
        ↓
3D 미리보기
        ↓
사용자 확인
        ↓
기존 overlay create/patch API로 저장
```

추론 API 자체는 SHP를 수정하지 않는다.

---

# 3. P0 범위

## 3.1 반드시 구현

### A. 미검출 지주 신규 추가

1. 작업자가 Point 레이어를 선택한다.
2. `미검출 지주 추가` 버튼 또는 단축키 `B`를 누른다.
3. 3D 점군에서 지주 몸체의 임의 지점을 클릭한다.
4. 서버가 지주 바닥점을 계산한다.
5. 시드점, 축, 계산된 바닥점을 3D 화면에 미리 표시한다.
6. 작업자가 `Enter` 또는 `저장`을 누른다.
7. 계산된 바닥점으로 신규 PointZ 피처를 생성한다.
8. 연속 추가가 켜져 있으면 다음 지주 클릭 상태를 유지한다.

### B. 기존 피처의 지주 하단 재산출

1. 작업자가 기존 Point 피처를 선택한다.
2. `지주 하단 재산출` 버튼을 누른다.
3. 3D 점군에서 해당 지주 몸체를 클릭한다.
4. 서버가 새 바닥점을 계산하고 미리 표시한다.
5. 확인 시 기존 피처의 geometry를 새 PointZ로 수정한다.
6. 기존 속성은 보존하고, 바닥점 전용 매핑 필드만 갱신한다.

### C. 실패·모호성 처리

- 추론 실패 시 기존 피처 또는 신규 피처를 저장하지 않는다.
- 모호한 결과는 `검토 필요`로 표시한다.
- 사용자는 다시 클릭하거나 일반 실제 포인트 선택 도구 `P`로 전환할 수 있다.
- 일반 포인트 선택은 “자동 하단 산출 실패 시 명시적으로 선택하는 별도 도구”로 남겨 둔다.

## 3.2 이번 작업에서 제외

- 파노라마 영상에서 클릭한 픽셀만으로 지주 바닥점 산출
- 여러 지주 일괄 자동 도화
- 전체 도로 시설물 종류 지원
- 차선·가드레일·경계석 도화
- 작업자 배정과 승인 워크플로
- 신규 딥러닝 모델 학습
- 사용자 정의 파라미터 UI
- 포인트클라우드 전체 타일링 엔진 교체

단, 향후 파노라마에서도 동일 API를 호출할 수 있도록 API는 dataset 좌표 시드를 받는다.

---

# 4. UX 상세 명세

## 4.1 도구 구분

기존 `P` 기능과 신규 `B` 기능의 의미를 섞지 않는다.

| 도구 | 의미 | 결과 |
|---|---|---|
| `P` 실제 포인트 선택 | 클릭한 점 자체를 geometry로 사용 | 클릭 XYZ 저장 |
| `B` 지주 하단 자동 산출 | 클릭점은 지주 식별용 시드 | 추정한 바닥 XYZ 저장 |
| 미검출 지주 추가 | 새 피처 생성 모드 | 계산 결과 확인 후 create |
| 지주 하단 재산출 | 선택 피처 수정 모드 | 계산 결과 확인 후 patch |

## 4.2 작업 상태

프런트엔드 상태는 최소한 다음을 구분한다.

```ts
type PoleBaseTarget =
  | {
      kind: 'pole-base-create'
      layerId: string
      continuous: boolean
    }
  | {
      kind: 'pole-base-move'
      layerId: string
      featureId: string | number
    }

type PoleBaseProposalState =
  | { status: 'idle' }
  | { status: 'picking'; target: PoleBaseTarget }
  | {
      status: 'loading'
      target: PoleBaseTarget
      frameId: string
      seed: [number, number, number]
    }
  | {
      status: 'ready'
      target: PoleBaseTarget
      frameId: string
      seed: [number, number, number]
      result: PoleBaseInferResponse
    }
  | {
      status: 'error'
      target: PoleBaseTarget
      frameId?: string
      seed?: [number, number, number]
      message: string
      reasonCodes: string[]
    }
```

기존 `OverlayPickTarget`에 신규 종류를 추가하거나, 충돌 방지를 위해 `poleBaseProposal`을 별도 상태로 둔다. 어느 방식을 사용해도 되지만 아래 원칙은 지킨다.

- 일반 create/move와 지주 하단 추론을 구분한다.
- 서버 응답이 오기 전까지 geometry를 변경하지 않는다.
- `ready`에서도 저장하지 않는다.
- 확인 동작에서만 overlay create/patch를 호출한다.

## 4.3 키보드와 버튼

- `B`: 미검출 지주 추가 모드 시작
- 선택된 기존 Point가 있고 속성 패널의 재산출 버튼을 누르면 move 모드 시작
- `Enter`: 준비된 결과 저장
- `R`: 같은 대상에서 다시 지주점 선택
- `Esc`:
  1. 미리보기가 있으면 미리보기 취소
  2. 미리보기가 없으면 도구 모드 종료
- 기존 `P`, `N`, 프레임 이동 키와 충돌하지 않게 한다.
- input/textarea/select에 포커스가 있을 때 단축키를 실행하지 않는다.

## 4.4 3D 미리보기

PointCloudView에 전용 THREE group을 둔다.

필수 표시:

- 클릭 시드점 marker
- 추정 지주 축 line
- 최종 바닥점 marker
- 시드점에서 바닥점까지의 보조 line

선택 표시:

- 지면 plane의 작은 patch
- 지면 support point 일부

디버그 포인트는 응답당 최대 256개만 반환한다. 원본 국지 점군을 API JSON으로 반환하지 않는다.

## 4.5 결과 카드

다음 정보만 간결하게 표시한다.

- 상태: `자동 산출 가능`, `검토 필요`, `산출 실패`
- 최종 X/Y/Z
- 품질 점수
- 축 RMSE
- 지면 RMSE
- 바닥까지 외삽한 높이
- 주요 경고 한 줄
- `저장`, `다시 선택`, `취소`

`review` 결과도 사용자가 저장할 수 있으나 경고를 명확히 표시한다. `failed` 결과는 저장 버튼을 비활성화한다.

## 4.6 연속 미검출 추가

미검출 지주 추가 모드는 `연속 추가`를 기본 활성화한다.

확인 저장 후:

- 새 피처를 선택 상태로 만든다.
- 짧은 성공 알림을 표시한다.
- `continuous=true`이면 즉시 다음 지주 시드 클릭 상태로 돌아간다.
- 프레임을 이동해도 모드는 유지할 수 있으나, 진행 중 요청과 미리보기는 취소한다.

---

# 5. 백엔드 구조

## 5.1 권장 신규 파일

```text
mms_shp_detection/
  manual_pole_base.py              # 순수 NumPy/SciPy 알고리즘
  webapp/
    pole_tools.py                  # 국지 원본 점군 수집 + FastAPI route

tests/
  test_manual_pole_base.py
  test_webapp_pole_tools.py
```

필요한 최소 수정:

```text
mms_shp_detection/pole.py
mms_shp_detection/webapp/app.py
mms_shp_detection/webapp/overlays.py
webui/src/types.ts
webui/src/lib/api.ts
webui/src/components/OverlayContext.tsx
webui/src/components/OverlayPanel.tsx
webui/src/views/PointCloudView.tsx
관련 CSS 및 기존 테스트
```

`point_queries.py` 같은 공용 모듈은 두 번째 기능에서 재사용 요구가 생길 때 분리한다. 이번 P0에서는 `pole_tools.py` 안에 작은 국지 조회 helper를 두어 파일 수를 늘리지 않아도 된다.

## 5.2 기존 `pole.py` 재사용 방식

기존 private 함수를 신규 웹 모듈에서 직접 import하지 않는다.

다음 둘 중 하나를 선택한다.

### 권장안: 동작을 바꾸지 않는 public primitive 추출

`pole.py`에서 기존 구현을 그대로 사용하도록 public wrapper 또는 public dataclass를 만든다.

예시 이름:

```python
fit_pole_axis(...)
estimate_local_ground(...)
intersect_pole_axis_with_ground(...)
```

요구사항:

- 기존 `find_pole_bases()`도 같은 구현을 계속 사용한다.
- 함수 본문을 복제하지 않는다.
- 기존 자동 파이프라인의 파라미터와 결과를 변경하지 않는다.
- 기존 pole 회귀 테스트를 통과시킨다.

### 대안: 독립 순수 구현

public 추출이 기존 코드에 과도한 영향을 줄 경우 `manual_pole_base.py`에 필요한 축 fitting과 지면 fitting만 독립 구현한다.

단, 기존 로직을 그대로 복사해 두 개의 서로 다른 버전으로 방치하지 않는다. 공통 수학 helper는 가능한 한 한 위치에 둔다.

---

# 6. API 계약

## 6.1 Endpoint

```http
POST /api/datasets/{dataset_id}/frames/{frame_id}/pole-base/infer
```

이 API는 read-only다. overlay 또는 SHP를 수정하지 않는다.

## 6.2 Request

```json
{
  "coordinate_space": "dataset",
  "seed_position": [209123.456, 412345.678, 35.912],
  "profile": "balanced",
  "debug": false
}
```

P0에서는 `profile`은 `balanced`만 허용해도 된다. 사용자가 반경과 포인트 제한을 임의로 크게 설정하는 API는 만들지 않는다.

## 6.3 Response

```json
{
  "status": "auto",
  "algorithm": "manual_seed_axis_ground_intersection",
  "algorithm_version": "1",
  "coordinate_space": "dataset",
  "seed_position": [209123.456, 412345.678, 35.912],
  "snapped_seed_position": [209123.451, 412345.681, 35.908],
  "base_position": [209123.487, 412345.702, 31.204],
  "axis": {
    "point": [209123.472, 412345.694, 33.500],
    "direction": [0.005, 0.004, 0.999979],
    "point_count": 138,
    "observed_z_min": 31.482,
    "observed_z_max": 37.113,
    "vertical_span_m": 5.631,
    "vertical_bin_count": 31,
    "longest_consecutive_bin_count": 27,
    "occupancy_ratio": 0.86,
    "rmse_m": 0.046,
    "tilt_deg": 0.37,
    "seed_distance_m": 0.031
  },
  "ground": {
    "method": "classified_or_geometry_anchored_plane",
    "z_at_base": 31.204,
    "rmse_m": 0.058,
    "cell_count": 13,
    "candidate_cell_count": 19,
    "nearest_support_distance_m": 0.29,
    "plane_coefficients": [0.012, -0.006, 31.201],
    "reference_xy": [209123.487, 412345.702]
  },
  "quality": {
    "score": 0.91,
    "candidate_count": 2,
    "ambiguous": false,
    "bottom_gap_m": 0.278,
    "components": {
      "seed": 0.90,
      "axis": 0.94,
      "span": 1.0,
      "continuity": 0.86,
      "ground": 0.89,
      "bottom_gap": 1.0
    }
  },
  "reason_codes": [],
  "warnings": [],
  "debug": null
}
```

## 6.4 `status`

- `auto`: 모든 자동 저장 품질 gate를 통과했지만 UI 확인은 여전히 필요
- `review`: 바닥점은 존재하지만 외삽·모호성·지면 품질 경고가 있음
- `failed`: 신뢰할 수 있는 바닥점을 만들지 못함. `base_position=null`

알고리즘상의 실패는 HTTP 200과 `status=failed`로 반환한다. 사용자가 다시 클릭할 수 있는 정상적인 업무 결과이기 때문이다.

## 6.5 HTTP 상태

| 상태 | 의미 |
|---|---|
| 200 | 추론 완료. `auto`, `review`, `failed` 중 하나 |
| 202 | point-cloud catalog 준비 중. `Retry-After` 제공 |
| 404 | dataset 또는 frame 없음 |
| 422 | 잘못된 좌표, 비미터 CRS, 프레임에서 너무 먼 시드 |
| 503 | point reader 사용 불가 또는 catalog 오류 |
| 500 | 예상하지 못한 서버 오류만 사용 |

## 6.6 Reason codes

아래 코드를 문자열 상수로 관리한다.

```text
INVALID_SEED
METRIC_CRS_REQUIRED
SEED_OUTSIDE_FRAME_WINDOW
SEED_NOT_ON_SOURCE_POINT
NO_LOCAL_POINTS
LOCAL_POINT_LIMIT_EXCEEDED
TOO_MANY_CANDIDATE_BLOCKS
NO_VERTICAL_AXIS
AXIS_TOO_SHORT
AXIS_DISCONTINUOUS
AXIS_RMSE_HIGH
AXIS_TILT_EXCESS
AMBIGUOUS_AXES
NO_GROUND_SUPPORT
GROUND_RMSE_HIGH
GROUND_HYPOTHESES_CONFLICT
GROUND_TOO_FAR
GROUND_PENETRATION
BOTTOM_EXTRAPOLATED
BASE_OUTSIDE_LOCAL_WINDOW
```

한글 메시지는 프런트엔드에서 reason code를 매핑한다. 서버는 디버깅을 위한 짧은 영문 `detail`을 함께 제공할 수 있다.

---

# 7. 원본 국지 점군 조회

## 7.1 브라우저 미리보기 점을 그대로 계산에 사용하지 않는 이유

MMSP는 화면 표시를 위해 포인트 예산에 맞춰 샘플링된다. 샘플만으로 축과 지면을 계산하면 다음 문제가 생긴다.

- 지주 하단 점이 샘플에서 누락될 수 있음
- 지면 cell 수가 부족할 수 있음
- 축 연속성 지표가 왜곡됨
- point budget 설정에 따라 결과가 변함

브라우저 클릭점은 시드 좌표로만 사용하고, 서버는 그 주변의 **원본 LAS/PCDB 블록**을 다시 읽는다.

## 7.2 좌표 검증

1. `seed_position`은 finite한 3개 숫자여야 한다.
2. dataset CRS의 수평 단위가 metre 계열인지 확인한다.
3. 시드 XY가 현재 frame origin에서 30m 이내인지 확인한다.
4. 시드 Z가 frame origin Z에서 비정상적으로 멀지 않은지 확인한다.
5. 원본 점군에서 시드와 0.20m 이내의 가장 가까운 점을 찾고 `snapped_seed_position`으로 사용한다.
6. 가까운 원본 점이 없으면 `SEED_NOT_ON_SOURCE_POINT`로 실패한다.

브라우저의 local float32 좌표를 dataset double 좌표에 더한 값에는 작은 오차가 있을 수 있으므로 0.20m snap 허용치를 둔다.

## 7.3 조회 범위 기본값

```python
seed_snap_radius_m = 0.20
local_xy_radius_m = 2.0
local_z_below_seed_m = 12.0
local_z_above_seed_m = 4.0
max_candidate_blocks = 128
max_local_points = 1_000_000
```

시드가 지주 상단에 있어도 지면까지 읽을 수 있게 아래 방향 범위를 충분히 둔다.

## 7.4 블록 선택

1. `match_nearest_pointcloud_files(frame_task, catalog, neighbor_count=8)`을 사용한다.
2. 정확한 job/track match가 여러 LAS split을 반환하면 모두 후보로 유지한다.
3. 각 block bbox가 다음 범위와 교차하는지 검사한다.
   - XY: 시드 중심 반경 2.0m
   - Z: `seed_z - 12m` ~ `seed_z + 4m`
4. 교차 block만 bbox 거리순으로 읽는다.
5. 128개를 넘으면 조용히 잘라내지 말고 `TOO_MANY_CANDIDATE_BLOCKS`로 실패한다.
6. `catalog.data_root`가 있으면 기존 safe path resolver를 사용한다.
7. `read_block_records()`로 원본 XYZ와 classification을 읽는다.
8. 읽은 뒤 정확한 cylinder/Z 조건으로 다시 crop한다.
9. 1,000,000점을 넘으면 무작위 샘플링하지 않는다.
   - 우선 반경을 늘리지 않았는지 확인한다.
   - P0에서는 `LOCAL_POINT_LIMIT_EXCEEDED`로 실패해 결과의 결정성을 보장한다.

조회는 `asyncio.to_thread()`에서 수행하고 별도 semaphore로 동시 실행 수를 2 이하로 제한한다.

```python
app.state.pole_tool_semaphore = asyncio.Semaphore(2)
```

추론 중 overlay lock을 잡지 않는다.

---

# 8. 클릭 시드 기반 지주 바닥점 알고리즘

## 8.1 입력의 의미

클릭 시드는 정확한 축 중심 또는 하단점일 필요가 없다.

단, 다음 조건은 작업자 안내에 명시한다.

- 지주 몸체의 실제 LiDAR return을 클릭한다.
- 표지판 면, 신호등 헤드, 가로 암, 전선, 나뭇가지는 클릭하지 않는다.
- 상단을 클릭할 수 있지만 지주 몸체의 일부여야 한다.

## 8.2 파라미터

`ManualPoleBaseParameters` dataclass를 만든다.

```python
@dataclass(frozen=True)
class ManualPoleBaseParameters:
    seed_snap_radius_m: float = 0.20

    axis_search_radius_m: float = 0.75
    axis_seed_gate_m: float = 0.30
    axis_cluster_radius_m: float = 0.24
    axis_inlier_radius_m: float = 0.18
    xy_voxel_m: float = 0.10
    z_bin_m: float = 0.15
    min_axis_points: int = 18
    min_vertical_span_m: float = 0.90
    min_vertical_bins: int = 5
    min_consecutive_vertical_bins: int = 4
    min_vertical_occupancy_ratio: float = 0.35
    max_observed_z_gap_m: float = 1.0
    max_axis_rmse_m: float = 0.12
    max_axis_tilt_deg: float = 15.0
    candidate_merge_radius_m: float = 0.18
    max_axis_hypotheses: int = 24
    ambiguity_score_margin: float = 0.08

    ground_search_radius_m: float = 1.50
    ground_core_radius_m: float = 0.75
    ground_exclusion_radius_m: float = 0.24
    ground_cell_size_m: float = 0.25
    ground_cell_quantile: float = 0.10
    ground_min_cells: int = 6
    ground_max_rmse_m: float = 0.20
    ground_surface_step_m: float = 0.22
    max_ground_support_distance_auto_m: float = 0.35
    max_ground_support_distance_review_m: float = 0.75

    max_ground_penetration_m: float = 0.10
    max_bottom_gap_auto_m: float = 0.35
    max_bottom_gap_review_m: float = 1.50
    max_base_seed_xy_distance_m: float = 1.0

    ground_class_ids: tuple[int, ...] = (2, 11)
    excluded_axis_class_ids: tuple[int, ...] = (2, 3, 4, 5, 11)
```

P0에서는 UI에서 이 값을 편집하지 않는다. 변경이 필요하면 코드 또는 config의 단일 section에서 관리한다.

## 8.3 전체 처리 단계

```text
입력 시드 검증 및 원본 점 snap
            ↓
시드 주변 국지 원본 점군 수집
            ↓
수직 구조 후보 중심 생성
            ↓
각 후보에 robust x(z), y(z) 축 fitting
            ↓
시드와 가장 일치하는 축 선택
            ↓
축 주변 지면 cell 생성
            ↓
시드 근처와 연결된 지면 surface 선택
            ↓
robust ground plane fitting
            ↓
지주 축과 ground plane 교차
            ↓
품질 gate 및 auto/review/failed 결정
```

## 8.4 Step 1 — 시드 snap

- 국지 점군 내에서 시드와 가장 가까운 3D 점을 찾는다.
- 0.20m 이내이면 해당 점을 snapped seed로 사용한다.
- 거리 자체를 결과 품질에 포함한다.
- browser preview point가 실제 원본 sample이면 보통 수 cm 이내여야 한다.

## 8.5 Step 2 — 축 후보용 점 필터

기본 mask:

```python
finite_xyz
xy_distance_to_seed <= axis_search_radius_m
seed_z - 12m <= z <= seed_z + 4m
```

classification이 존재하고 의미 있는 경우:

- 2, 11: 지면 계열이므로 축 후보에서 제외
- 3, 4, 5: 식생 계열이므로 축 후보에서 제외
- class 0 또는 unknown은 geometry 기반으로 허용

intensity threshold는 P0의 필수 조건으로 사용하지 않는다. 장비·거리·입사각에 따라 값이 달라질 수 있기 때문이다.

## 8.6 Step 3 — 초기 축 가설 생성

점 하나마다 fitting하지 않는다.

1. 후보 점의 XY를 `xy_voxel_m`으로 voxelize한다.
2. 각 XY cell에서 다음을 계산한다.
   - point count
   - Z span
   - 점유 Z bin 수
   - 최장 연속 Z bin 수
3. 최소한의 수직 연속성이 있는 cell만 중심 후보로 사용한다.
4. snapped seed XY 자체도 반드시 하나의 가설 중심으로 넣는다.
5. 시드와 가까운 순, vertical span이 큰 순으로 정렬한다.
6. 최대 24개만 fitting한다.

지주 단면이 여러 XY cell에 걸칠 수 있으므로 cell 하나의 점만 사용하지 않는다. 각 중심에서 `axis_cluster_radius_m` cylinder로 점을 다시 모은다.

## 8.7 Step 4 — robust 축 fitting

지주 축은 다음과 같이 표현한다.

```text
x(z) = sx × (z - z_ref) + ix
y(z) = sy × (z - z_ref) + iy
```

권장 fitting 절차:

1. cylinder 점을 0.15m Z bin으로 나눈다.
2. 각 bin의 XYZ median을 계산한다.
3. bin median에 대해 robust 선형 회귀를 수행한다.
4. residual median/MAD를 이용해 outlier bin을 반복 제거한다.
5. 모든 원본 점을 축에 재투영하여 radial inlier를 구한다.
6. 필요하면 inlier만으로 한 번 재 fitting한다.
7. 거의 수직인 축은 기존 `pole.py`의 endpoint-centre plumb 안정화 로직을 재사용한다.

후보 gate:

- inlier point 수 ≥ 18
- vertical span ≥ 0.90m
- vertical bin 수 ≥ 5
- 최장 연속 bin ≥ 4
- occupancy ≥ 0.35
- max Z gap ≤ 1.0m
- tilt ≤ 15°
- axis RMSE ≤ 0.12m
- seed 높이에서 seed-to-axis 거리 ≤ 0.30m

클릭점이 중간 또는 상단이어도 모든 국지 shaft point를 사용해 아래쪽으로 축을 fitting한다.

## 8.8 Step 5 — 축 후보 ranking과 모호성

각 후보를 0~1 점수로 정규화한다.

```text
axis_candidate_score =
    0.30 × seed_proximity
  + 0.20 × axis_rmse_score
  + 0.15 × vertical_span_score
  + 0.15 × occupancy_score
  + 0.10 × consecutive_bin_score
  + 0.10 × tilt_score
```

시드 proximity를 가장 크게 둔다. 사용자가 “이 지주”를 지정했다는 의미를 우선한다.

다음 조건이면 모호성 경고를 만든다.

- 1위와 2위 점수 차이가 0.08 미만
- 두 축의 seed 높이 XY가 0.18m 이상 떨어짐
- 두 후보 모두 기본 gate 통과

모호하지만 바닥점을 계산할 수 있으면 `review + AMBIGUOUS_AXES`, 차이가 지나치게 작고 선택 근거가 없으면 `failed`로 처리한다.

## 8.9 Step 6 — 지면 후보 cell 생성

선택 축의 예상 XY 주변에서 지면을 찾는다.

1. 축으로부터 0.24m 이내 점은 지주 몸체·기초 오염 가능성이 있으므로 제외한다.
2. 1.50m 이내 점을 0.25m XY cell로 나눈다.
3. LAS classification 지면점이 충분하면 cell median Z를 생성한다.
4. classification이 없거나 부족하면 각 cell의 낮은 10% quantile Z를 생성한다.
5. 단순히 전체에서 가장 낮은 지면을 선택하지 않는다.

## 8.10 Step 7 — curb·단차 대응 지면 surface 선택

지주가 보도 위에 있고 바로 옆 차도가 낮은 경우, 전체 low quantile만 쓰면 차도면을 잘못 선택할 수 있다.

따라서 지면 cell을 하나의 평면으로 바로 fitting하지 않고, 축 주변 surface를 먼저 선택한다.

1. 지주 exclusion ring 바깥에서 축 XY와 가장 가까운 cell들을 seed cell로 정한다.
2. 인접 cell 사이의 높이 차가 다음을 만족할 때만 같은 surface로 연결한다.

```text
abs(dz) <= ground_surface_step_m + 허용경사 × xy_distance
```

3. 가장 가까운 seed cell에서 region growing을 수행한다.
4. 최소 6개 cell이 연결된 component를 우선 사용한다.
5. classified ground component와 geometry component를 각각 만들 수 있다.
6. 두 component의 예측 높이가 0.15m 이상 다르면 다음을 비교한다.
   - 축과 가장 가까운 support 거리
   - 연결 cell 수
   - plane RMSE
   - 최하단 축 inlier와의 높이 일관성
7. 우열이 명확하지 않으면 `GROUND_HYPOTHESES_CONFLICT`와 `review`를 반환한다.

이 방식은 지주가 서 있는 보도면을 선택하고, 인접한 낮은 차도면으로 내려가는 오류를 줄이기 위한 것이다.

## 8.11 Step 8 — robust ground plane fitting

지면 plane은 기준 XY를 중심으로 표현한다.

```text
z = a × (x - x0) + b × (y - y0) + c
```

절차:

1. 선택된 cell representative를 입력으로 사용한다.
2. 최소 6개 cell을 요구한다.
3. least-squares fitting 후 residual median/MAD로 outlier를 제거한다.
4. 최대 5회 반복한다.
5. 최종 RMSE가 0.20m를 넘으면 실패 또는 review 처리한다.
6. 지주 축 주변에서 가장 가까운 지면 support distance를 계산한다.

## 8.12 Step 9 — 축과 지면의 교차점

축을 다음처럼 parametric line으로 쓴다.

```text
P(t) = P0 + tD
```

```text
x = xr + t·dx
y = yr + t·dy
z = zr + t·dz
```

지면식에 대입하면:

```text
t = [a(xr-x0) + b(yr-y0) + c - zr]
    / [dz - a·dx - b·dy]
```

분모가 0에 가까우면 교차점을 만들지 않는다.

최종 base는:

```text
base = P0 + tD
```

최저 shaft return을 그대로 바닥점으로 사용하지 않는다. 축·지면 교차점을 사용해야 밑부분 가림과 지주 기울기에 대응할 수 있다.

## 8.13 Step 10 — 물리적 검증

다음을 확인한다.

- base 좌표가 finite
- ground plane residual이 수치 오차 범위 내
- base XY와 seed XY 거리 ≤ 1.0m
- base가 조회 cylinder 밖으로 벗어나지 않음
- 최저 축 inlier가 ground 아래 0.10m 이상 들어가지 않음
- 지면 support가 너무 멀지 않음

`bottom_gap_m`:

```text
bottom_gap_m = observed_axis_z_min - base_z
```

판정:

| 조건 | 판정 |
|---|---|
| `-0.10m <= gap <= 0.35m` | auto 가능 |
| `0.35m < gap <= 1.50m` | review, `BOTTOM_EXTRAPOLATED` |
| `gap > 1.50m` | 일반적으로 failed |
| `gap < -0.10m` | review 또는 failed, `GROUND_PENETRATION` |

상단 클릭 자체는 문제없지만, 실제 점군에 축의 아래쪽 증거가 거의 없고 1.5m 이상 외삽해야 한다면 자동 저장 품질로 인정하지 않는다.

## 8.14 최종 품질 점수

```text
quality_score =
    0.20 × seed_score
  + 0.20 × axis_rmse_score
  + 0.15 × vertical_span_score
  + 0.10 × continuity_score
  + 0.25 × ground_score
  + 0.10 × bottom_gap_score
```

예시 정규화:

```python
seed_score = clip(1 - seed_axis_distance / 0.30, 0, 1)
axis_rmse_score = clip(1 - axis_rmse / 0.12, 0, 1)
vertical_span_score = clip((span - 0.9) / 3.1, 0, 1)
continuity_score = occupancy_ratio
ground_score = clip(1 - ground_rmse / 0.20, 0, 1) * support_score
bottom_gap_score = 1.0 if gap <= 0.35 else clip((1.50-gap)/1.15, 0, 1)
```

상태 결정:

```text
auto:
  score >= 0.80
  hard warning 없음
  ground support distance <= 0.35m
  bottom gap <= 0.35m
  ambiguous=false

review:
  base가 존재하고 score >= 0.55
  또는 외삽·모호성·지면 충돌 경고가 있음

failed:
  base 없음
  또는 score < 0.55
  또는 hard failure gate 위반
```

## 8.15 순수 함수 의사 코드

```python
def infer_pole_base_from_seed(
    points_xyz: np.ndarray,
    seed_xyz: np.ndarray,
    *,
    classifications: np.ndarray | None = None,
    parameters: ManualPoleBaseParameters = ManualPoleBaseParameters(),
) -> ManualPoleBaseResult:
    validate_arrays(points_xyz, classifications, seed_xyz)

    snapped_seed, snap_distance = snap_seed_to_source_point(
        points_xyz,
        seed_xyz,
        parameters.seed_snap_radius_m,
    )
    if snapped_seed is None:
        return failed("SEED_NOT_ON_SOURCE_POINT")

    axis_pool = select_axis_pool(
        points_xyz,
        snapped_seed,
        classifications,
        parameters,
    )
    hypothesis_centres = build_axis_hypothesis_centres(
        axis_pool,
        snapped_seed,
        parameters,
    )

    axis_candidates = []
    for centre_xy in hypothesis_centres:
        candidate_points = crop_axis_cylinder(
            axis_pool,
            centre_xy,
            parameters.axis_cluster_radius_m,
        )
        fit = fit_pole_axis(candidate_points, parameters)
        if fit is None:
            continue
        metrics = validate_axis_against_seed(fit, snapped_seed, parameters)
        if metrics.passes_basic_gates:
            axis_candidates.append(score_axis_candidate(fit, metrics, parameters))

    selected_axis, ambiguity = choose_seeded_axis(axis_candidates, parameters)
    if selected_axis is None:
        return failed("NO_VERTICAL_AXIS")

    ground_hypotheses = build_ground_hypotheses(
        points_xyz,
        selected_axis,
        classifications,
        parameters,
    )
    ground = choose_anchored_ground_surface(
        ground_hypotheses,
        selected_axis,
        parameters,
    )
    if ground is None:
        return failed("NO_GROUND_SUPPORT", axis=selected_axis)

    base = intersect_axis_and_ground(selected_axis, ground)
    if base is None:
        return failed("NO_GROUND_SUPPORT", axis=selected_axis, ground=ground)

    validation = validate_base(
        base,
        selected_axis,
        ground,
        snapped_seed,
        ambiguity,
        parameters,
    )
    return build_result(
        seed_xyz=seed_xyz,
        snapped_seed=snapped_seed,
        snap_distance=snap_distance,
        axis=selected_axis,
        ground=ground,
        base=base,
        validation=validation,
    )
```

모든 sampling, 정렬, tie-break는 deterministic하게 구현한다.

---

# 9. 저장과 속성 처리

## 9.1 권위 좌표

지주 바닥점의 권위 데이터는 SHP 편집본의 **PointZ geometry**다.

```json
{
  "type": "Point",
  "coordinates": [base_x, base_y, base_z]
}
```

속성에만 좌표를 넣고 geometry를 클릭점에 남겨 두는 구현은 금지한다.

## 9.2 속성 동기화

레이어에 아래와 같은 명시적 바닥점 필드가 이미 있을 때만 값을 동기화한다.

```text
X 후보: BASE_X, BAS_X, POLE_X
Y 후보: BASE_Y, BAS_Y, POLE_Y
Z 후보: BASE_Z, BAS_Z, POLE_Z, ELEV
방법: BASE_MTH, BAS_MTH
품질: BASE_Q, BAS_Q
상태: BASE_ST, BAS_ST, QA_STATUS
원본 프레임: SRC_FRAME, FRAME_ID
```

규칙:

1. 대소문자와 공백을 정규화해 exact alias만 매칭한다.
2. 일반 `X`, `Y`, `Z` 필드는 의미가 불분명하므로 자동 수정하지 않는다.
3. 존재하지 않는 DBF 열을 P0에서 자동 생성하지 않는다.
4. 기존 속성은 보존한다.
5. 필드 type에 맞게 숫자 또는 문자열을 변환한다.
6. method 값은 짧게 `MAN_SEED`를 사용한다.
7. status는 `AUTO` 또는 `REVIEW`를 사용한다.
8. quality는 숫자 필드이면 0~1 float, 정수 필드이면 0~100 integer로 저장한다.

## 9.3 신규 피처 원자적 생성

현재 신규 Point create request에 properties가 없다면 다음처럼 optional properties를 추가한다.

```ts
interface OverlayFeatureCreateRequest {
  geometry?: { type: 'Point'; coordinates: [number, number, number?] }
  coordinate_space?: OverlayCoordinateSpace
  copy_geometry_from?: string | number
  expected_revision?: number
  properties?: Record<string, unknown>
}
```

서버는 existing field schema에 맞춰 검증한 뒤 geometry와 properties를 한 revision에서 생성한다.

“먼저 빈 피처 생성 후 두 번째 PATCH”는 중간 실패 시 불완전 피처가 남을 수 있으므로 피한다.

## 9.4 기존 피처 수정

기존 `PATCH feature`를 사용한다.

- `expected_revision`을 반드시 보낸다.
- geometry와 매핑된 properties를 한 번에 보낸다.
- 409 conflict가 발생하면 proposal을 지우지 않는다.
- 레이어를 새로 불러온 뒤 사용자가 다시 저장할 수 있게 한다.

## 9.5 추론과 저장 분리

다음 흐름을 유지한다.

```text
infer API: read-only
        ↓
client proposal
        ↓
user confirm
        ↓
overlay create/patch
```

추론 endpoint에서 overlay DB를 직접 수정하지 않는다.

---

# 10. FastAPI 구현 명세

## 10.1 `pole_tools.py`

책임:

1. request/response Pydantic model
2. dataset/frame/catalog 검증
3. metric CRS 검증
4. 원본 국지 레코드 수집
5. `infer_pole_base_from_seed()` 호출
6. 결과 직렬화
7. bounded debug payload 생성

권장 내부 함수:

```python
_validate_metric_dataset_crs(...)
_validate_seed_against_frame(...)
_block_intersects_local_window(...)
_collect_local_point_records(...)
_public_manual_pole_base_result(...)
```

## 10.2 App 연결

`app.py`:

```python
from .pole_tools import router as pole_tools_router
```

```python
app.state.pole_tool_semaphore = asyncio.Semaphore(2)
app.include_router(pole_tools_router)
```

bootstrap capability:

```json
{
  "pole_base_inference": true
}
```

point reader가 없으면 false로 반환한다.

## 10.3 Catalog 준비 중 처리

`media.py`의 point preview 처리 방식과 동일한 사용자 경험을 제공한다.

- catalog가 아직 없고 오류 상태가 아니면 schedule 후 202
- `Retry-After: 2`
- client는 자동 무한 재시도하지 않는다.
- 사용자가 한 번 다시 시도하거나 짧은 bounded retry를 최대 2회만 수행한다.

## 10.4 보안·리소스 제한

- seed 좌표로 임의의 전체 dataset scan을 할 수 없게 frame origin 거리 제한
- path는 catalog와 safe resolver를 통해서만 접근
- 요청당 block/point hard limit
- JSON debug point hard limit
- query parameter로 반경과 point limit을 노출하지 않음
- API 로그에 전체 포인트 또는 전체 응답을 기록하지 않음

---

# 11. 프런트엔드 구현 명세

## 11.1 `types.ts`

추가 타입:

```ts
export type PoleBaseInferStatus = 'auto' | 'review' | 'failed'

export interface PoleBaseInferRequest {
  coordinate_space: 'dataset'
  seed_position: [number, number, number]
  profile: 'balanced'
  debug?: boolean
}

export interface PoleBaseInferResponse {
  status: PoleBaseInferStatus
  algorithm: string
  algorithm_version: string
  coordinate_space: 'dataset'
  seed_position: [number, number, number]
  snapped_seed_position?: [number, number, number]
  base_position: [number, number, number] | null
  axis?: PoleBaseAxisResult
  ground?: PoleBaseGroundResult
  quality: PoleBaseQualityResult
  reason_codes: string[]
  warnings: string[]
}
```

bootstrap capabilities에도 `pole_base_inference?: boolean`을 추가한다.

## 11.2 `api.ts`

```ts
inferPoleBase(
  datasetId: string,
  frameId: string,
  payload: PoleBaseInferRequest,
  signal?: AbortSignal,
) {
  return json<PoleBaseInferResponse>(
    `/api/datasets/${encodeURIComponent(datasetId)}/frames/${encodeURIComponent(frameId)}/pole-base/infer`,
    {
      method: 'POST',
      ...jsonBody(payload),
      signal,
      timeout: 30_000,
      retries: 0,
    },
  )
}
```

POST 요청을 자동 재시도하지 않는다. 이 endpoint는 read-only이지만 사용자가 프레임을 바꾼 뒤 오래된 결과가 들어오는 것을 방지해야 한다.

## 11.3 `OverlayContext.tsx`

추가 책임:

- 지주 create/move target 관리
- 요청 AbortController 관리
- dataset/frame 변경 시 오래된 응답 무시
- proposal 저장
- confirm 시 create/patch
- continuous mode 복귀
- reason code 사용자 메시지 매핑

추가 함수 예시:

```ts
beginCreatePoleBase(layerId: string, continuous?: boolean): void
beginRecomputeSelectedPoleBase(): void
applyPoleSeed(
  frameId: string,
  coordinates: [number, number, number],
): Promise<void>
confirmPoleBaseProposal(): Promise<void>
retryPoleBasePick(): void
cancelPoleBaseProposal(): void
```

일반 `applyPickedCoordinate()`는 기존 동작을 유지한다.

## 11.4 `PointCloudView.tsx`

현재 picking 흐름에서 실제 visible point index를 찾은 뒤 target에 따라 분기한다.

```ts
if (target.kind === 'move' || target.kind === 'create') {
  await overlay.applyPickedCoordinate(datasetXYZ, 'dataset')
} else if (
  target.kind === 'pole-base-create' ||
  target.kind === 'pole-base-move'
) {
  await overlay.applyPoleSeed(frame.id, datasetXYZ)
}
```

주의:

- 화면 local 좌표가 아니라 dataset 좌표를 API에 보낸다.
- 추론은 렌더링용 sample point 자체가 아니라 서버 원본 점군을 사용한다.
- frame/dataset 변경 시 proposal THREE objects를 정리한다.
- detached popup에서도 단축키와 상태가 동일하게 작동해야 한다.

## 11.5 `OverlayPanel.tsx`

Point 레이어일 때만 다음 버튼을 활성화한다.

- `미검출 지주 추가`
- 선택된 Point 피처가 있을 때 `지주 하단 재산출`
- `연속 추가` toggle

`미검출 지주 추가` 클릭 시 패널을 닫고 3D 점군을 작업 대상으로 만든다. 3D 창이 닫혀 있으면 기존 popup open 방식을 사용해 연다.

## 11.6 속성 patch utility

프런트엔드 또는 작은 공용 utility에 다음을 둔다.

```ts
buildPoleBasePropertyPatch(
  fields: OverlayField[],
  currentProperties: Record<string, unknown>,
  result: PoleBaseInferResponse,
  frameId: string,
): Record<string, unknown>
```

매칭되는 필드가 없으면 빈 patch를 반환한다. geometry 저장은 항상 수행한다.

---

# 12. 구현 순서

아래 순서를 바꾸지 않는다. 백엔드 계약이 완성되기 전에 UI부터 만들지 않는다.

## Phase 1 — 순수 알고리즘

1. `ManualPoleBaseParameters`, result dataclass 정의
2. synthetic point cloud helper를 테스트 안에 작성
3. seed snap 구현
4. 축 후보 생성과 robust fitting 구현
5. anchored ground surface와 plane fitting 구현
6. axis-ground intersection 구현
7. status/reason/quality 구현
8. `tests/test_manual_pole_base.py` 통과

## Phase 2 — 원본 점군 조회와 API

1. local block bbox filter 구현
2. `read_block_records()` 기반 full-resolution crop 구현
3. request validation 구현
4. endpoint와 response serialization 구현
5. router/semaphore/capability 연결
6. `tests/test_webapp_pole_tools.py` 통과

## Phase 3 — 저장 API 보완

1. create request에 optional properties 추가
2. 기존 field validation 재사용
3. atomic geometry+properties create 테스트
4. 기존 overlay create 동작 회귀 확인

## Phase 4 — 프런트엔드 상태와 API

1. 타입 추가
2. api client 추가
3. context state machine 추가
4. stale response/abort 처리
5. confirm create/patch 처리

## Phase 5 — 작업 UI

1. OverlayPanel 버튼
2. PointCloudView target 분기
3. 축·시드·바닥 preview
4. 결과 카드와 키보드
5. 연속 추가
6. 관련 Vitest 통과

## Phase 6 — 제한된 회귀 검증

1. 공유 `pole.py`를 수정했다면 관련 pole 테스트만 실행
2. overlay 관련 테스트 실행
3. `npm run build` 한 번
4. `git diff --check`

---

# 13. 테스트 시나리오

## 13.1 순수 알고리즘 필수 테스트

하나의 table-driven 테스트 또는 소수의 테스트로 구성한다.

### Case 1 — 평평한 지면, 수직 지주

- ground Z = 10.0
- pole axis XY = (2.0, 3.0)
- pole height = 6m
- seed 높이: 20%, 50%, 95%
- 각 seed에서 base XY/Z 오차 ≤ 0.05m
- status `auto`

### Case 2 — 경사진 지면

- ground plane에 X/Y 경사 부여
- 지주가 5~8° 기울어짐
- base 3D 오차 ≤ 0.08m

### Case 3 — 하단 가림

- 지면부터 0.8m 구간의 shaft point 제거
- base 오차 ≤ 0.10m
- status `review`
- `BOTTOM_EXTRAPOLATED` 포함

### Case 4 — 보도와 차도 단차

- 지주가 높은 보도면에 설치
- 0.5~1.0m 옆에 더 낮은 차도면 배치
- 알고리즘은 낮은 차도면이 아니라 지주 주변 보도면을 선택

### Case 5 — 인접한 두 지주

- 두 축 간격 0.6m
- 각 지주를 클릭했을 때 seed와 가까운 축을 선택
- 점수가 비슷한 경계 클릭은 `review` 또는 `AMBIGUOUS_AXES`

### Case 6 — 식생·벽·가드레일 오염

- 비수직 또는 넓은 면 구조를 함께 배치
- 클릭한 지주 축을 유지
- 축 RMSE gate가 넓은 면을 지주로 선택하지 않게 함

### Case 7 — 지면 없음

- shaft만 있고 주변 ground point 없음
- `failed`
- `base_position=null`
- `NO_GROUND_SUPPORT`

### Case 8 — 결정성

- 동일 입력을 3회 호출
- base, score, reason order가 동일

## 13.2 API 필수 테스트

- dataset/frame 404
- invalid seed 422
- frame에서 너무 먼 seed 422
- catalog 없음 → 202
- point reader 없음 → 503
- mock blocks 중 bbox 교차 block만 읽음
- sampled preview가 아니라 `read_block_records()`를 호출함
- point hard limit 동작
- algorithmic failed가 HTTP 200으로 반환됨
- infer endpoint가 overlay revision을 변경하지 않음

## 13.3 프런트엔드 필수 테스트

- create mode에서 클릭 → loading → ready → confirm → create 호출
- move mode에서 confirm → expected revision 포함 patch 호출
- `failed` 응답에서 저장 비활성화
- `review` 응답에서 경고 표시
- Esc로 취소
- frame 변경 시 이전 요청 abort 및 stale response 무시
- continuous create 저장 후 picking 상태 복귀
- 기존 일반 `P` 실제 포인트 이동 기능 유지
- alias field가 있을 때만 property patch 생성

---

# 14. 인수 기준

## 14.1 기능

- [ ] 지주 중간을 클릭해도 바닥점이 계산된다.
- [ ] 지주 상단 shaft return을 클릭해도 축 전체를 찾아 아래로 계산한다.
- [ ] 지면이 일부 가려져도 축·지면 교차점으로 처리한다.
- [ ] 신규 미검출 지주를 PointZ로 생성한다.
- [ ] 기존 지주 피처의 geometry를 재산출할 수 있다.
- [ ] 저장 전 3D 미리보기가 표시된다.
- [ ] 실패 시 자동 저장하지 않는다.
- [ ] 연속 추가가 가능하다.

## 14.2 정확성

- [ ] 합성 평지 수직 지주 base 오차 ≤ 5cm
- [ ] 경사·기울기 case base 오차 ≤ 8cm
- [ ] 0.8m 하단 가림 case base 오차 ≤ 10cm이며 review 처리
- [ ] 보도/차도 단차 case에서 지주 설치면을 선택
- [ ] 동일 입력의 결과가 deterministic

## 14.3 성능과 안전

- [ ] 원본 LAS 전체를 메모리에 읽지 않는다.
- [ ] bbox와 2m local crop을 사용한다.
- [ ] 최대 block/point limit이 있다.
- [ ] 동시 추론 수가 제한된다.
- [ ] preview MMSP sample만으로 추론하지 않는다.
- [ ] infer endpoint는 read-only다.
- [ ] overlay revision conflict를 처리한다.

## 14.4 회귀

- [ ] 기존 AI 자동 검출 pipeline 결과를 변경하지 않는다.
- [ ] 기존 Point 생성·이동·삭제가 동작한다.
- [ ] 기존 `P` 도구가 동작한다.
- [ ] 프런트엔드 build가 통과한다.

---

# 15. AI 미검출 후처리 도구에서 추가로 벤치마킹할 항목

이 절은 P0 이후 우선순위다. 이번 구현 중 무리해서 전부 추가하지 않는다.

## P0.5 — 수동 복구 생산성

1. **연속 구축 모드**
   - 저장 후 다음 시드 클릭으로 즉시 복귀
   - 이전 피처의 공통 속성을 선택적으로 유지

2. **중복 경고**
   - 새 base 주변 기존 피처가 일정 거리 이내이면 저장 전 경고
   - AI 검출 결과를 작업자가 중복 생성하지 않게 함

3. **Undo last create**
   - 마지막 수동 생성 피처를 한 번에 되돌림

4. **근거 표시**
   - 시드, 축, 지면, base를 함께 표시
   - 작업자가 왜 이 점이 만들어졌는지 판단 가능

## P1 — 미검출 탐색과 검수 큐

1. 프레임별 `검토 완료 / 미검토` 상태
2. AI confidence가 낮은 객체 우선 큐
3. 감지 결과가 드문 구간 또는 coverage gap 표시
4. 지도·파노라마·점군·속성표의 선택 동기화
5. `다음 미검토 프레임` 단축키
6. 작업자 수정 이력과 생성 방식 기록
7. 동일 지주가 여러 프레임에 나타날 때 중복 후보 경고
8. 품질 점수에 따른 색상 또는 필터

## P2 — 반자동 도화 플랫폼 확장

1. 표지판 면 클릭 → 연결 지주 자동 선택
2. 표지판/신호등과 support_id 연결
3. 지주 높이, 기울기, 직경 자동 측정
4. 선형·면형 시설물 반자동 도화
5. 작업자 승인 단계와 납품 QA rule engine

## 생산성 지표

향후 XD 계열 도구와 비교할 때 모델 정확도만 보지 않는다.

- 미검출 1개 추가 평균 시간
- 객체 1개당 클릭 수
- 첫 클릭 성공률
- `auto/review/failed` 비율
- base 수평·수직 오차
- 재클릭 비율
- 중복 생성률
- 1km당 작업자 후처리 시간
- 자동 처리 후 최종 납품까지 총 작업 시간

본 프로젝트의 차별점은 “작업자가 전부 만드는 도구”가 아니라 “AI가 만든 결과에서 예외만 빠르게 판정·복구하는 도구”여야 한다.

---

# 16. 알려진 제약사항

다음은 오류가 아니라 P0 알고리즘의 명시적 한계다.

1. 클릭점이 지주 몸체가 아니라 가로 암·전선·표지판 면이면 실패하거나 잘못된 후보가 생길 수 있다.
2. 지주와 유사한 수직 구조가 매우 가깝고 seed 자체가 경계에 있으면 review가 필요하다.
3. 지면점이 전혀 없으면 신뢰할 수 있는 설치 바닥점을 만들 수 없다.
4. 1.5m 이상 하단 구간이 완전히 가려진 경우 자동 확정하지 않는다.
5. 교량 상판, 터널, 계단, 큰 기초 pedestal처럼 “바닥”의 업무 정의가 다른 구조는 별도 profile이 필요하다.
6. PointZ geometry와 별도 속성 필드의 의미가 레이어마다 다르면 layer-level mapping 설정이 추가로 필요하다.
7. P0는 파노라마 픽셀만으로 이 기능을 실행하지 않는다. 3D 실제 point seed를 사용한다.

---

# 17. 에이전트 작업 체크리스트

## 구현 전

- [ ] 현재 브랜치와 변경 파일 확인
- [ ] 관련 심볼만 검색
- [ ] 전체 테스트 실행하지 않음
- [ ] 기존 `P`와 신규 `B` 동작 경계 확인

## 백엔드

- [ ] 순수 알고리즘 모듈 작성
- [ ] full-resolution local point query 작성
- [ ] metric CRS와 frame distance 검증
- [ ] read-only endpoint 작성
- [ ] semaphore와 capability 연결
- [ ] reason code와 deterministic ordering 적용

## 저장

- [ ] PointZ geometry를 권위 좌표로 사용
- [ ] optional properties atomic create 지원
- [ ] 기존 속성 보존
- [ ] exact alias field만 동기화
- [ ] expected revision 사용

## 프런트엔드

- [ ] create/move target 분리
- [ ] loading/ready/error proposal 상태
- [ ] abort와 stale response 방지
- [ ] 3D seed/axis/base preview
- [ ] 저장 전 확인
- [ ] continuous mode
- [ ] 키보드 충돌 방지

## 검증

- [ ] 그룹 A 테스트
- [ ] 그룹 B 테스트
- [ ] 필요한 경우에만 그룹 C 테스트
- [ ] 프런트엔드 build 한 번
- [ ] `git diff --check`
- [ ] 불필요한 리팩터링 없음

---

# 18. 최종 구현 판단 원칙

에이전트가 세부 구현 선택에서 고민될 때 아래 순서로 판단한다.

1. 잘못된 바닥점을 자동 저장하지 않는가?
2. 클릭점이 상단이어도 원본 점군에서 축 전체를 찾는가?
3. 인접한 낮은 차도면이 아니라 지주가 서 있는 국지 surface를 선택하는가?
4. 기존 AI 자동 파이프라인을 변경하지 않는가?
5. 기존 overlay revision과 편집본 보존 정책을 따르는가?
6. point preview sample이 아닌 원본 local records를 사용하는가?
7. 작업자가 미검출 객체를 최소 클릭으로 연속 처리할 수 있는가?
8. 테스트와 로그가 작업 범위에 비해 과도하지 않은가?

이 우선순위를 만족하는 가장 작은 변경으로 P0를 완성한다.
