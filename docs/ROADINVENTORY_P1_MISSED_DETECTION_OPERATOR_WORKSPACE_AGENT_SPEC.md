# RoadInventory-MMS P1 구현 지시서
## XDRoadMap 벤치마킹 기반 AI 미검출 보정 작업실

> 대상 저장소: `dbparkJ/RoadInventory-MMS`  
> 기준 브랜치: `main`  
> 기준 커밋: `0218e16566eeaa9191e3a4c0021dbf2e49fc0c1e` (`feat: add manual pole base workflow`)  
> 선행 문서: `docs/ROADINVENTORY_P0_MANUAL_POLE_BASE_AGENT_SPEC.md`  
> 문서 목적: AI 자동 검출이 놓친 객체를 작업자가 빠르고 안전하게 보완하는 **웹 기반 Operator-in-the-loop 작업실**을 작은 단계로 구현한다.  
> 구현 패러다임: `작업자 전체 수동 도화`가 아니라 `AI 자동 구축 → 누락·오류만 작업자가 보정 → 규칙 기반 검수`이다.

---

# 0. AI 에이전트 실행 계약

이 절은 모든 구현 지시보다 우선한다.

## 0.1 선행 조건

1. P0 지주 바닥점 기능은 이미 구현되어 있으므로 다시 만들지 않는다.
2. 작업 시작 시 반드시 다음만 확인한다.

```bash
git status --short
git log -1 --oneline
git diff --stat
```

3. 사용자가 디버깅 중인 수정사항이 있으면 덮어쓰지 않는다.
4. P0 관련 실패가 이번 Step을 막는 경우에만 최소 범위로 수정한다.
5. 기준 커밋 이후 구조가 변경되었으면 실제 현재 코드를 기준으로 경로를 조정하되, 제품 계약은 유지한다.

## 0.2 한 번에 한 Step만 구현

- 이 문서 전체를 한 세션에 구현하지 않는다.
- 사용자가 명시한 Step 하나만 구현한다.
- Step의 테스트와 사용자 확인 자료를 제출한 뒤 반드시 중단한다.
- 사용자의 `다음 Step 진행` 승인 전에는 후속 Step을 시작하지 않는다.
- 여러 Step을 묶어 대규모 PR로 만들지 않는다.

## 0.3 범위 제한

이번 P1의 목표는 다음뿐이다.

1. AI 결과 검수 범위를 세션으로 관리
2. 누락 의심 구간과 실패 결과를 작업 큐로 제공
3. 파노라마·점군에서 미검출 객체를 수동 생성
4. 객체 종류별 속성 템플릿과 자동 입력
5. 저장 전 중복·속성·관계 오류 검사
6. 저장 근거와 작업 이력 유지
7. 작업 완료율과 남은 검수 범위 표시

다음은 별도 승인 전 구현하지 않는다.

- 신규 YOLO 학습
- 능동학습 자동 재학습 파이프라인
- 차선·가드레일 등 선형 객체의 완전한 CAD 편집기
- Postgres, Redis, Kafka 등 인프라 교체
- 다기관 동시편집 플랫폼
- 전체 UI 프레임워크 교체
- 전체 LAS를 브라우저 메모리에 올리는 방식
- XDRoadMap 화면을 그대로 복제하는 작업
- XDExpress 도로 기하·강우·물리 시뮬레이션 기능

## 0.4 저장소 탐색 예산

처음부터 전체 저장소를 반복해서 읽지 않는다. 우선 다음 파일만 확인한다.

### 현재 P0 및 편집 기반

- `mms_shp_detection/manual_pole_base.py`
- `mms_shp_detection/webapp/pole_tools.py`
- `mms_shp_detection/webapp/overlays.py`
- `mms_shp_detection/webapp/store.py`
- `mms_shp_detection/webapp/detections.py`
- `mms_shp_detection/webapp/media.py`
- `mms_shp_detection/pointcloud.py`
- `webui/src/components/OverlayContext.tsx`
- `webui/src/components/OverlayPanel.tsx`
- `webui/src/components/Workspace.tsx`
- `webui/src/views/PanoramaView.tsx`
- `webui/src/views/PointCloudView.tsx`
- `webui/src/lib/api.ts`
- `webui/src/types.ts`

### 필요할 때만 확인

- `mms_shp_detection/pipeline.py`
- `mms_shp_detection/shp_writer.py`
- `mms_shp_detection/geometry.py`
- 관련 테스트 파일

큰 파일은 심볼 검색 후 필요한 범위만 읽는다. 한 파일의 전체 내용을 반복 출력하지 않는다.

## 0.5 테스트·토큰 예산

### 금지

- 작업 시작 직후 전체 `pytest`
- 작업 시작 직후 전체 `npm test`
- 성공한 테스트의 반복 실행
- 실제 대형 LAS를 기본 테스트 fixture로 복제
- 테스트 로그 전체를 대화에 붙여넣기
- 관련 없는 lint·format 전면 실행
- 전체 `pipeline.py`를 테스트 목적으로 반복 분석
- 한 Step에서 테스트 파일을 6개 이상 새로 만드는 행위

### Step당 허용

- 백엔드 테스트 그룹 최대 1개
- 프런트엔드 테스트 그룹 최대 1개
- 프런트엔드 빌드 최대 1회
- 실패한 테스트는 실패 항목만 재실행
- 최종 전체 회귀는 Step 9 또는 사용자 요청 시에만 수행

예시:

```bash
python -m pytest -q tests/test_webapp_review_tasks.py --tb=short --maxfail=1
```

```bash
cd webui
npm test -- src/components/ReviewQueue.test.tsx --reporter=dot
npm run build
```

## 0.6 Step 종료 보고

에이전트는 각 Step 종료 시 아래만 보고한다.

1. 변경 파일
2. 구현한 사용자 흐름
3. 실행한 테스트 명령과 결과
4. 사용자가 확인할 화면·행동
5. 알려진 제약
6. 다음 Step은 시작하지 않았다는 사실

전체 코드와 전체 로그를 응답에 복사하지 않는다.

---

# 1. 제품 방향

## 1.1 목표

현재 시스템은 다음 흐름을 가진다.

```text
MMS 원본
  ↓
YOLO 자동 검출
  ↓
점군 기반 3D 위치화·지주 하단점 산출
  ↓
중복 제거·SHP 생성
  ↓
작업자 후처리
```

P1에서는 마지막 `작업자 후처리`를 생산 도구 수준으로 만든다.

```text
AI 결과와 실패 근거 수집
  ↓
검수 세션과 작업 큐 생성
  ↓
작업자가 파노라마·점군에서 누락 확인
  ↓
객체별 스마트 생성 도구 실행
  ↓
속성 자동 입력·중복 검사
  ↓
저장·검수 상태 갱신
  ↓
다음 작업으로 자동 이동
```

## 1.2 XDRoadMap과의 차별점

XDRoadMap의 공개 설명은 MMS 점군·사진·3D GIS를 중첩해 정밀도로지도를 도화하고 속성을 입력하며, 오류·무결성을 검사하는 통합 제작도구에 가깝다.

RoadInventory-MMS는 이를 그대로 복제하지 않는다.

| 구분 | XDRoadMap에서 확인되는 방향 | RoadInventory-MMS 목표 |
|---|---|---|
| 시작점 | 작업자가 구축·도화 | AI가 우선 구축 |
| 작업자 역할 | 전체 객체 제작 가능 | 누락·오류·불확실 결과만 처리 |
| 실행 환경 | 전문 제작용 도구 | 호스트 계산 + 웹 작업실 |
| 데이터 처리 | 통합 클라이언트 도구 중심 | 서버 원본 계산 + 경량 파생 스트리밍 |
| 품질 관리 | 오류·무결성 검사 | AI 근거·수동 이력까지 포함한 QA |
| 확장 | 외부 제작 모듈 | 객체별 inference tool registry |

---

# 2. 공개 자료에서 확인되는 XDRoadMap 벤치마킹 근거

공개 자료로 직접 확인되는 범위와 본 문서의 설계 추론을 구분한다.

## 2.1 직접 확인되는 기능

EGIS 공식 XDBUILD 페이지와 공시 자료에서 다음이 확인된다.

1. MMS 점군, 사진, 3D GIS 데이터 중첩
2. 데이터 구축자가 정밀한 3D 도로 데이터를 분석
3. 속성정보의 효율적 입력
4. 대용량 점군과 사진의 끊김 없는 가시화
5. 차선 자동 추출
6. 정밀도로지도 도화
7. 검수 데이터 EOP·IOP 생성
8. MMS 점군과 사진 정합
9. 정밀도로 공간 데이터 오류 자동 체크
10. 공간·속성 데이터 자동입력과 무결성 체크
11. 도로 시설물 자산관리
12. 시설물 설치 간격과 시거 분석

## 2.2 본 문서에서 제품 기능을 웹 도구로 번역한 항목

다음 항목은 공개된 버튼명이나 상세 매뉴얼을 복제한 것이 아니라, 위 기능을 현재 레포에 맞게 구현 가능한 도구로 해석한 것이다.

- 객체 중심 다중 뷰 동기화
- 연속 도화와 `저장 후 다음`
- 객체별 속성 템플릿
- 오류 목록에서 객체 위치로 이동
- 작업 세션·담당 범위·진행률
- 중복 객체 경고
- 저장 근거와 편집 이력
- 점군 클립·단면·색상 모드
- 객체별 스마트 생성 플러그인

문서와 코드 주석에서 이 항목들을 `XDRoadMap에 정확히 같은 버튼이 존재한다`고 표현하지 않는다.

---

# 3. 현재 레포 기준선

기준 커밋에는 다음 기반이 존재한다.

## 3.1 이미 구현된 기능

- 지도·파노라마·3D 점군 뷰
- 현재 프레임 기반 동기화
- YOLO 원본 bbox 표시
- YOLO 3D 대표점과 SHP 포인트 표시
- SHP 레이어 업로드·표시·다운로드
- Point 피처 생성·수정·삭제·위치 복사
- 속성표와 optimistic revision
- 지도·파노라마·점군에서 실제 포인트 선택
- 클릭 시드 기반 지주 바닥점 자동 산출
- 신규 지주 바닥점 연속 추가
- 기존 피처 지주 바닥점 재산출
- 지주 축·시드·바닥점 미리보기
- 원본 점군 서버 계산과 경량 MMSP/MMSO 스트리밍
- 프레임 이동과 detachable viewer

## 3.2 부족한 부분

| 부족한 부분 | 현재 영향 |
|---|---|
| 무엇을 검수했는지 기록하는 세션 없음 | 작업자가 프레임을 반복 확인하거나 누락 |
| 누락 의심·실패 결과 작업 큐 없음 | 전체 구간을 눈으로 순차 탐색 |
| 객체 종류별 수동 생성 템플릿 없음 | 레이어·속성을 매번 수동 선택 |
| 파노라마에서 수동 bbox를 그려 기존 3D 로직으로 보내는 흐름 없음 | 미검출 표지판 생성이 불편 |
| 작업자 생성 객체의 근거 데이터가 분리 저장되지 않음 | 감사·재검수 어려움 |
| 저장 전 중복·관계 검사 부족 | 같은 객체 중복 추가 가능 |
| 오류 목록과 위치 이동 기능 부족 | QA 결과 수정 시간이 길어짐 |
| 작업 단위 undo/redo 없음 | 잘못 저장한 뒤 수동 복구 |
| 점군 클립·단면·intensity/class 색상 도구 부족 | 복잡한 위치에서 객체 판별이 느림 |
| 검수 완료율·미검수 구간 표시 부족 | 납품 완료 판단이 어려움 |

---

# 4. 우선순위별 도구 목록

## 4.1 P1 핵심 도구

### 1. 검수 세션

작업 시작 시 다음 범위를 고정한다.

- 데이터셋
- 원본 AI 실행
- 대상 SHP 레이어
- 대상 클래스
- 트랙
- 프레임 범위
- 작업자 식별자
- 생성 시각
- 검수 상태

세션을 다시 열면 마지막 작업과 필터를 복원한다.

### 2. 작업 큐

작업 큐는 `확정된 미검출 목록`이 아니라 `작업자가 확인할 후보와 미검수 범위`다.

큐 항목 종류:

- `MANUAL_SCAN`: 작업자가 직접 추가한 검수 위치
- `LOW_CONFIDENCE`: threshold 아래 원본 YOLO 후보
- `PROJECTION_FAILED`: 2D 검출은 있으나 3D 위치화 실패
- `GEOMETRY_REVIEW`: 3D 결과가 REVIEW
- `POLE_BASE_REVIEW`: 지주 하단점 품질이 REVIEW
- `SPACING_ANOMALY`: 주변 객체 간격이 비정상인 약한 후보
- `UNREVIEWED_INTERVAL`: 아직 확인하지 않은 프레임 구간
- `MANUAL_FLAG`: 작업자가 `나중에 확인`으로 표시

### 3. 객체 중심 동기화

작업 큐 항목을 선택하면 다음이 하나의 context로 이동한다.

- 지도: 해당 프레임과 주변 객체
- 파노라마: 같은 프레임, bbox와 포인트
- 점군: 같은 프레임 origin과 주변 점
- 속성표: 선택 객체 또는 신규 객체 template
- 작업 큐: 현재 항목 강조

뷰마다 독립적으로 다른 프레임을 가리키지 않게 한다.

### 4. 파노라마 수동 bbox·포인트 도구

작업자가 미검출 표지판·신호등 등 영상 객체를 다음 방식으로 생성한다.

- 단일 점 클릭
- 사각형 드래그
- 필요 시 bbox 수정
- 객체 클래스 선택
- 서버 3D proposal 요청
- 점군 근거와 품질 확인
- 저장 또는 다시 선택

YOLO를 다시 실행하지 않는다. 작업자가 만든 bbox를 기존 2D→3D 대표점 로직에 넣는 adapter를 만든다.

### 5. 객체 템플릿

객체 종류별로 다음을 preset으로 관리한다.

- 대상 레이어
- geometry type
- 필수 속성
- 기본값
- 코드 domain
- 생성 도구
- 중복 검색 반경
- 관계 규칙
- 저장 후 연속 모드
- QA 규칙

P1에서는 Point 객체부터 시작한다.

권장 첫 template:

1. `TRAFFIC_SIGN`
2. `SIGN_SUPPORT_POLE`
3. `TRAFFIC_SIGNAL`
4. `STREET_LIGHT`
5. `DIRECT_POINT_GENERIC`

### 6. 제안 후 저장 구조

모든 스마트 생성은 직접 저장하지 않는다.

```text
작업자 입력
  ↓
read-only inference
  ↓
GeometryProposal
  ↓
미리보기
  ↓
사용자 확인
  ↓
기존 overlay create/patch
```

P0 지주 바닥점 패턴을 공통 proposal 계약의 첫 구현으로 사용한다.

### 7. 저장 후 다음

버튼과 단축 동작:

- `확인하고 다음`
- `건너뛰고 다음`
- `다시 확인`
- `오검출`
- `미검출 객체 추가`
- `현장조사 필요`
- `이전 항목`

저장 후 화면이 임의로 초기화되지 않고 다음 큐 항목으로 이동해야 한다.

### 8. 중복 검사

저장 전 서버에서 같은 레이어 또는 호환 레이어의 주변 객체를 검사한다.

- class별 반경
- XY 거리
- Z 차이
- 속성 호환성
- `support_id`
- 같은 원본 bbox·frame
- 기존 detection ID

정확히 같은 객체로 판단되면 저장을 막는다. 애매하면 경고만 표시하고 작업자 선택을 받는다.

### 9. 속성 자동 입력

다음은 자동으로 채운다.

- source dataset
- source run
- source frame
- source view
- track
- image name
- creation tool
- origin: `AI`, `MANUAL`, `CORRECTED`
- created/updated time
- quality status
- model 또는 manual template
- class
- base method 등 객체 도구별 품질

내부 provenance를 임의의 DBF 필드에 강제로 넣지 않는다. 내보낼 필드와 내부 메타데이터를 분리한다.

### 10. QA 오류 탐색기

오류 목록을 누르면 객체와 프레임으로 이동한다.

초기 규칙:

- 필수 속성 누락
- domain 밖 속성
- geometry 없음
- 데이터셋 범위 밖
- 비정상 Z
- 주변 중복
- relation 누락
- 근거 frame 없음
- 수동 생성 후 미검수
- REVIEW 상태 미해결

## 4.2 P1 보조 도구

### 점군 클립

- 선택점 중심 XY 반경
- Z 최소·최대
- clipping box
- 현재 proposal 주변만 표시
- 클립 초기화

### 점군 색상 모드

- RGB
- intensity
- classification
- height
- source file
- selected proposal support

브라우저에 원본 LAS 전체 속성을 보내지 않아도 된다. 서버가 선택 모드에 맞게 RGB를 생성해 MMSP derivative로 제공할 수 있다.

### 단면·측정

- 두 점 거리
- 수직 높이
- 지면과의 높이
- 원통 직경 근사
- 로컬 단면
- 객체 간 간격

P1 첫 릴리스에서 측정값을 SHP에 자동 저장하지 않는다.

### 배치 속성 편집

선택된 여러 피처에 공통 속성을 적용한다. geometry는 배치 변경하지 않는다.

### undo/redo

UI local state가 아니라 서버 audit revision을 기준으로 한다. 다른 요청이 수정한 뒤 과거 상태를 조용히 덮어쓰지 않는다.

## 4.3 후속 기능

- 선·면 객체 도화
- 다중 작업자 할당
- 능동학습 샘플 export
- 모델별 miss heatmap
- 자동 재추론
- 규격서별 schema package
- 객체 간 설치 간격·시거 분석
- 차선 반자동 추적

---

# 5. 권장 화면 구조

```text
┌────────────────────────────────────────────────────────────────────┐
│ 검수 세션 / 대상 Run / 트랙 / 클래스 / 진행률 / 저장 상태          │
├───────────────┬──────────────────────────────┬─────────────────────┤
│ 작업 큐       │ 지도·파노라마·점군 작업 영역 │ 객체 템플릿·속성     │
│               │                              │                     │
│ 상태 필터     │ 현재 frame context 고정       │ 필수값               │
│ 후보 유형     │ AI bbox                       │ 자동입력값            │
│ 이전/다음     │ 수동 bbox                      │ QA 경고               │
│               │ proposal 미리보기             │ 확인하고 다음         │
├───────────────┴──────────────────────────────┴─────────────────────┤
│ 근거: run / model / frame / seed / bbox / 품질 / edit revision    │
└────────────────────────────────────────────────────────────────────┘
```

기존 detachable 지도·파노라마·점군 구조를 없애지 않는다. 검수 세션 context만 공유한다.

---

# 6. 도메인 계약

## 6.1 ReviewSession

```json
{
  "id": "rvw_<opaque>",
  "dataset_id": "ds_<opaque>",
  "source_run_ids": ["run_<opaque>"],
  "target_layer_ids": ["ov_<opaque>"],
  "track_ids": ["Track01"],
  "frame_range": [0, 1200],
  "class_filters": ["TRAFFIC_SIGN", "SIGN_SUPPORT_POLE"],
  "status": "active",
  "created_by": "operator-local",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "last_task_id": "rvt_<opaque>"
}
```

상태:

```text
draft -> active -> paused -> completed
                    |
                    -> archived
```

## 6.2 ReviewTask

```json
{
  "id": "rvt_<opaque>",
  "session_id": "rvw_<opaque>",
  "dataset_id": "ds_<opaque>",
  "task_type": "PROJECTION_FAILED",
  "status": "todo",
  "priority": 72,
  "frame_id": "frm_<opaque>",
  "track_id": "Track01",
  "source_run_id": "run_<opaque>",
  "source_detection_id": "det_<opaque>",
  "target_layer_id": "ov_<opaque>",
  "class_hint": "TRAFFIC_SIGN",
  "reason_codes": ["NO_SUPPORTING_POINTS"],
  "location_hint": [123.0, 456.0, 10.0],
  "claimed_by": null,
  "resolved_feature_ids": [],
  "resolution": null,
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

상태:

```text
todo -> in_progress -> confirmed
                    -> corrected
                    -> manual_added
                    -> false_positive
                    -> skipped
                    -> field_survey
```

## 6.3 ManualObservation

```json
{
  "observation_id": "mob_<opaque>",
  "dataset_id": "ds_<opaque>",
  "frame_id": "frm_<opaque>",
  "view_type": "panorama",
  "class_name": "TRAFFIC_SIGN",
  "geometry_2d": {
    "type": "equirectangular_bbox",
    "u_intervals": [[0.94, 1.0], [0.0, 0.03]],
    "v_min": 0.22,
    "v_max": 0.41,
    "image_width": 7040,
    "image_height": 3520
  },
  "created_by": "operator-local"
}
```

파노라마 seam을 지나는 bbox를 단순한 `left < right` 하나로 저장하지 않는다.

## 6.4 GeometryProposal

```json
{
  "proposal_id": "prp_<opaque>",
  "tool_id": "panorama_bbox_point_v1",
  "status": "review",
  "coordinate_space": "dataset",
  "geometry": {
    "type": "Point",
    "coordinates": [123.0, 456.0, 7.8]
  },
  "property_patch": {
    "CLASS_NM": "TRAFFIC_SIGN"
  },
  "quality": {
    "score": 0.78,
    "support_point_count": 43,
    "depth_spread_m": 0.18,
    "reprojection_error_px": 4.2
  },
  "reason_codes": ["DEPTH_CLUSTER_WEAK"],
  "evidence": {
    "frame_id": "frm_<opaque>",
    "observation_id": "mob_<opaque>",
    "seed_position": [123.1, 456.0, 8.2]
  }
}
```

## 6.5 FeatureProvenance

내부 메타데이터는 DBF 속성과 분리한다.

```json
{
  "layer_id": "ov_<opaque>",
  "feature_id": "f_000000123",
  "origin": "MANUAL",
  "source_run_id": "run_<opaque>",
  "source_frame_ids": ["frm_<opaque>"],
  "source_detection_ids": [],
  "manual_observation_ids": ["mob_<opaque>"],
  "creation_tool": "panorama_bbox_point_v1",
  "proposal_quality": 0.78,
  "review_status": "confirmed",
  "created_by": "operator-local",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

## 6.6 QaIssue

```json
{
  "id": "qai_<opaque>",
  "session_id": "rvw_<opaque>",
  "layer_id": "ov_<opaque>",
  "feature_id": "f_000000123",
  "rule_id": "DUPLICATE_NEARBY",
  "severity": "warning",
  "message": "0.31m 안에 같은 클래스 객체가 있습니다.",
  "related_feature_ids": ["f_000000087"],
  "status": "open"
}
```

---

# 7. 객체 생성 도구 Registry

P0의 지주 바닥점 기능을 특수 UI로 계속 확장하지 말고 공통 interface를 도입한다.

```python
class ManualObjectTool(Protocol):
    tool_id: str
    supported_templates: tuple[str, ...]

    def infer(self, request: ManualObjectInferRequest) -> GeometryProposal:
        ...
```

초기 tool:

| tool_id | 입력 | 출력 |
|---|---|---|
| `direct_point_v1` | 실제 점군 점 | PointZ |
| `ground_snap_point_v1` | 주변 시드점 | 지면 PointZ |
| `manual_pole_base_v1` | 지주 몸체 시드 | 지주 바닥 PointZ |
| `panorama_point_depth_v1` | 파노라마 클릭 + depth sample | PointZ |
| `panorama_bbox_point_v1` | 파노라마 bbox | 대표 PointZ |
| `existing_geometry_copy_v1` | 기존 feature | geometry 복사 |

원칙:

- tool은 overlay를 직접 수정하지 않는다.
- proposal은 짧은 TTL을 가진 서버 관리 객체 또는 완전한 재현 입력을 가진다.
- 저장 시 현재 layer revision을 다시 확인한다.
- 결과가 `failed`이면 저장할 수 없다.
- `review`는 사용자의 명시적 확인이 필요하다.
- `auto`도 자동 저장하지 않는다.

---

# 8. 핵심 알고리즘

## 8.1 파노라마 bbox를 3D로 변환

### 입력

- frame
- calibrated panorama axes
- 원본 image width/height
- equirectangular bbox
- object template
- 주변 MMSO point samples
- 필요 시 서버 원본 point blocks

### 단계

1. 브라우저 drag 영역을 normalized UV로 변환한다.
2. seam 교차 여부를 판정해 하나 또는 두 개의 U interval로 표현한다.
3. bbox 내부 MMSO 포인트를 찾는다.
4. bbox 중심에서 너무 먼 포인트에 낮은 가중치를 준다.
5. depth를 1차원 bin 또는 연결 군집으로 나눈다.
6. 가장 가까운 한 점이 아니라 **가까우면서 일정 수 이상 지지되는 첫 coherent depth cluster**를 선택한다.
7. 선택 cluster의 중앙 depth와 UV를 산출한다.
8. `origin + ray(u,v) * depth`로 초기 dataset XYZ를 만든다.
9. template이 요구하면 원본 점군의 작은 로컬 window에서 full-resolution refinement를 수행한다.
10. geometry·품질·reason code를 proposal로 반환한다.

### 실패·검토 조건

- bbox 내부 포인트 없음
- depth cluster가 둘 이상 유사
- support point 부족
- depth spread 과다
- frame 최대 범위 초과
- dataset CRS 부적합
- refinement 실패
- 기존 객체와 중복 가능성

### 기존 코드 재사용

가능하면 다음을 재사용한다.

- `panorama-pick`
- `_panorama_axes`
- MMSO cache
- `pixel_to_world_ray`
- 기존 detection bbox의 2D→3D 대표점 처리
- `PointCloudReaderCache`
- P0 proposal preview·confirm 패턴

작업자가 만든 bbox를 YOLO detection과 동일한 중간 계약으로 정규화한 뒤 기존 geometry path에 넣는 방식을 우선한다.

## 8.2 누락 후보 우선순위

미검출은 정답 데이터가 없으면 자동으로 확정할 수 없다. 따라서 `누락 검출기`가 아니라 `검수 후보 생성기`로 구현한다.

예시 점수:

```text
priority =
    source_weight
  + failure_severity
  + low_confidence_proximity
  + spatial_gap_score
  + class_business_priority
  + unreviewed_age
  - duplicate_probability
```

초기 source weight 권장:

| source | weight |
|---|---:|
| projection failed | 35 |
| 3D geometry review | 30 |
| pole base review | 25 |
| low confidence raw detection | 20 |
| manual flag | 40 |
| unreviewed interval | 10 |
| spacing anomaly | 8 |

점수는 정확도 확률로 표시하지 않는다. `검수 우선순위`라고 표시한다.

## 8.3 중복 판정

단계:

1. 같은 또는 호환 template의 기존 feature를 spatial query한다.
2. class별 XY 반경을 적용한다.
3. Z가 있으면 Z 차이를 적용한다.
4. source detection ID·manual observation ID가 같으면 exact duplicate로 본다.
5. `support_id` 관계 객체는 같은 위치여도 별개 record일 수 있으므로 template 규칙을 우선한다.
6. exact duplicate는 저장 차단.
7. near duplicate는 비교 카드 표시.
8. 작업자가 `기존 선택`, `새로 저장`, `기존 위치 수정` 중 선택한다.

## 8.4 속성 자동 입력

입력 우선순위:

```text
template fixed value
  > inference result
  > selected source feature
  > frame metadata
  > dataset metadata
  > user last-used value
  > null
```

자동값과 작업자 입력값을 UI에서 구분한다.

## 8.5 QA 규칙

규칙은 pure function으로 구현한다.

```python
def evaluate(feature, template, context) -> list[QaIssue]:
    ...
```

초기 rule:

- `REQUIRED_FIELD`
- `DOMAIN_VALUE`
- `GEOMETRY_REQUIRED`
- `OUTSIDE_DATASET_BOUNDS`
- `Z_OUTLIER`
- `DUPLICATE_NEARBY`
- `MISSING_SOURCE_FRAME`
- `UNREVIEWED_MANUAL_FEATURE`
- `SUPPORT_RELATION_REQUIRED`
- `REVIEW_PROPOSAL_UNRESOLVED`

규칙 하나마다 파일을 만들지 않는다. registry와 table-driven tests를 사용한다.

---

# 9. 백엔드 구조

## 9.1 권장 모듈

```text
mms_shp_detection/webapp/
  review_tasks.py          # session/task API와 application logic
  manual_objects.py        # tool registry, proposal API
  qa.py                    # QA rule registry와 issue API
  overlays.py              # 기존 최종 create/patch 계약 유지
  store.py                 # review session/task schema
```

필요 시 domain pure logic:

```text
mms_shp_detection/
  manual_object_tools.py
  review_candidates.py
  qa_rules.py
```

P0 `manual_pole_base.py`는 유지하고 adapter로 연결한다.

## 9.2 저장 위치

### `registry.sqlite3`

- `review_sessions`
- `review_tasks`
- `review_task_events`
- `qa_issues`

### overlay별 `features.sqlite3`

- 기존 `features`
- 기존 `audit`
- 신규 `feature_provenance`
- 신규 `manual_observations`
- 후속 `edit_transactions`

DBF schema를 내부 상태 때문에 임의 변경하지 않는다.

## 9.3 API 초안

### Session

```http
POST /api/datasets/{dataset_id}/review-sessions
GET  /api/datasets/{dataset_id}/review-sessions
GET  /api/review-sessions/{session_id}
PATCH /api/review-sessions/{session_id}
```

### Task

```http
POST /api/review-sessions/{session_id}/tasks/generate
GET  /api/review-sessions/{session_id}/tasks
GET  /api/review-tasks/{task_id}
PATCH /api/review-tasks/{task_id}
POST /api/review-tasks/{task_id}/resolve
```

### Manual observation and proposal

```http
POST /api/datasets/{dataset_id}/frames/{frame_id}/manual-observations
POST /api/datasets/{dataset_id}/frames/{frame_id}/manual-object-proposals
GET  /api/manual-object-proposals/{proposal_id}
DELETE /api/manual-object-proposals/{proposal_id}
```

### QA

```http
POST /api/review-sessions/{session_id}/qa/run
GET  /api/review-sessions/{session_id}/qa/issues
PATCH /api/qa/issues/{issue_id}
```

### Commit

최종 저장은 기존 overlay create/patch API를 우선 사용한다. 필요한 provenance는 같은 server transaction 경계에서 기록되도록 application service를 둔다.

새로운 `commit-proposal` endpoint가 필요하면 내부에서 다음을 하나의 lock과 transaction으로 수행한다.

1. proposal 확인
2. layer revision CAS
3. feature create/patch
4. provenance insert
5. review task resolve
6. QA enqueue
7. audit insert

중간 일부만 성공하지 않게 한다.

## 9.4 보안·운영

- 서버 절대경로 반환 금지
- dataset/layer/frame opaque ID 사용
- 원본 파일은 read-only
- proposal 최대 point count와 실행 semaphore
- bbox·task API body 크기 제한
- review task pagination
- SQLite transaction과 current revision 검증
- 다른 dataset의 frame/layer 결합 금지
- archive된 overlay에 저장 금지
- API 요청 취소 후 worker drain은 P0 패턴 재사용

---

# 10. 프런트엔드 구조

## 10.1 권장 컴포넌트

```text
webui/src/components/
  ReviewSessionBar.tsx
  ReviewQueue.tsx
  ReviewTaskCard.tsx
  ObjectTemplatePanel.tsx
  QaIssuePanel.tsx
  ProposalInspector.tsx
```

필요한 상태는 context를 무한 확장하지 말고 분리한다.

```text
ReviewContext
  - session
  - task queue
  - current task
  - filters
  - progress
  - task resolution

OverlayContext
  - layers
  - selected feature
  - final feature create/patch
  - existing P0 pole proposal

ManualObjectContext 또는 hook
  - current template
  - manual observation
  - geometry proposal
  - confirm/cancel
```

P0 API를 깨지 않기 위해 단계적으로 분리한다.

## 10.2 파노라마 도구

toolbar mode:

- navigate
- inspect
- manual point
- manual bbox
- existing point move
- pole base seed

mode는 상호 배타적이다. 드래그가 카메라 회전인지 bbox 작성인지 명확해야 한다.

bbox 상태:

```text
idle -> drawing -> adjusting -> proposing -> ready | error
```

## 10.3 점군 도구

toolbar:

- orbit
- select
- local clip
- Z slice
- measure
- proposal seed
- reset view

클립과 색상 모드는 local UI setting으로 보존한다.

## 10.4 단축키

기존 키와 충돌하지 않는다.

현재 계약:

- `A`, `D`: 프레임 이동
- `P`: 실제 Point 선택
- `B`: 지주 바닥점
- `N`: 신규 포인트
- `Esc`: 취소
- `Enter`: proposal 확인

P1 권장:

- `J`: 다음 검수 항목
- `K`: 이전 검수 항목
- `Shift+Enter`: 확인하고 다음
- `X`: 오검출 처리
- `F`: 나중에 확인
- `M`: 수동 bbox mode
- `Q`: QA 패널

텍스트 입력 중에는 단축키를 실행하지 않는다. detachable window에서도 canonical handler로 relay한다.

---

# 11. 단계별 구현 계획

## Step 0 — 기준선 동결과 계약 확정

### 목표

- 현재 P0 동작과 public API를 문서화
- P1 feature flag 정의
- 신규 데이터 계약만 추가
- 사용자 화면 변화 없음

### 변경

- `docs/current_architecture.md`에 P0 완료 상태 반영
- P1 API/type 초안
- `capabilities.review_workspace=false`
- 필요한 migration 전략 문서화

### 금지

- 실제 UI 구현
- 기존 P0 리팩터링
- DB migration 실행

### 사용자 확인 Gate 0

사용자에게 다음을 보여주고 중단한다.

- 현재 기능/부족 기능 표
- P1 화면 wireframe
- 첫 지원 객체 template 후보
- DBF와 내부 provenance 분리 방식

사용자가 승인할 항목:

1. 첫 지원 객체: 표지판 + 지주 권장
2. 검수 단위: 프레임 또는 거리 구간
3. 작업자 식별 방식
4. 내부 상태를 SHP에 내보낼지 여부

---

## Step 1 — 검수 세션·작업 큐 백엔드

### 목표

UI 없이 session/task를 저장하고 다시 읽을 수 있게 한다.

### 구현

- idempotent SQLite migration
- session CRUD
- task CRUD
- 상태 전이 검증
- pagination
- dataset/frame/run/layer ownership 검증
- 수동 `MANUAL_SCAN` task 생성

### 초기 상태 전이

```text
todo -> in_progress
in_progress -> confirmed | corrected | manual_added | false_positive | skipped | field_survey
terminal -> terminal 변경 금지
```

명시적인 reopen API만 terminal을 todo로 되돌릴 수 있다.

### 테스트

- session 생성/조회
- 잘못된 dataset relation 거부
- 상태 전이
- pagination
- server path 비노출

### 사용자 확인 Gate 1

API payload와 재시작 후 persistence를 확인하고 중단한다.

---

## Step 2 — 최소 Review Queue UI

### 목표

작업자가 직접 만든 검수 항목을 순서대로 처리한다.

### 구현

- Review session bar
- Review queue panel
- 현재 task 선택
- frame 이동
- `J/K`
- `건너뛰기`
- `현장조사`
- `완료`
- 진행률

아직 자동 후보 생성은 하지 않는다.

### 테스트

- queue load
- task selection → frame callback
- status update
- session restore
- keyboard and text input guard

### 사용자 확인 Gate 2

사용자가 10개 임시 task를 순서대로 처리해 본다.

확인할 질문:

- 큐 위치가 화면을 과도하게 차지하는가
- 지도/파노라마/점군 중 어떤 뷰를 기본으로 열 것인가
- 완료 후 자동 이동 속도가 적절한가

---

## Step 3 — 파노라마 수동 bbox Proposal

### 목표

미검출 표지판 하나를 파노라마 bbox로 생성할 수 있게 한다.

### 범위

- `TRAFFIC_SIGN` Point template 한 개
- bbox draw/edit
- seam 처리
- MMSO depth cluster
- PointZ proposal
- 미리보기
- 저장 전 confirm
- 아직 자동 속성 template은 최소값만 사용

### 재사용

- 현재 PanoramaView
- MMSO
- `panorama-pick`
- 기존 geometry helper
- overlay create
- proposal state pattern

### 테스트

백엔드:

- 단일 depth cluster
- 복수 depth cluster → REVIEW
- point 없음 → failed
- seam bbox
- max range

프런트:

- navigate와 bbox mode 충돌 없음
- drag proposal
- error/retry/cancel
- confirm

### 사용자 확인 Gate 3

실제 데이터에서 다음 5개를 확인한다.

1. 정면 표지판
2. 측면 표지판
3. 멀리 있는 표지판
4. 앞에 차량이 있는 표지판
5. 파노라마 seam 부근 객체

정확도보다 작업 흐름을 먼저 평가한다.

---

## Step 4 — 객체 Template·자동 속성·연속 추가

### 목표

미검출 표지판과 지주를 반복 생성한다.

### 구현

- template registry
- last-used template
- target layer mapping
- required/default/domain fields
- 자동 provenance
- 연속 추가
- `확인하고 다음`
- 지주 template은 기존 P0 tool adapter 사용
- 표지판과 지주의 `support_id` 연결 제안

### 테스트

- template validation
- incompatible layer 거부
- required field
- continuous create
- current layer revision conflict
- property mapping

### 사용자 확인 Gate 4

사용자가 한 구간에서 표지판 10개와 지주 10개를 추가하고 클릭 수·소요시간을 기록한다.

---

## Step 5 — 점군 판독 보조도구

### 목표

복잡한 장면에서 작업자가 객체를 쉽게 구분한다.

### 구현 순서

1. local clip
2. Z slice
3. RGB/intensity/classification/height 색상
4. proposal isolate
5. reset

server derivative에 `color_mode`를 추가할 경우 기존 MMSP contract를 깨지 않는다.

### 사용자 확인 Gate 5

다음 장면에서 확인한다.

- 수목과 지주가 겹침
- 가드레일 뒤 지주
- 여러 지주가 가까움
- 경사로
- 높은 점밀도

---

## Step 6 — 검수 후보 생성기

### 목표

작업자가 모든 프레임을 무조건 순회하지 않게 한다.

### 첫 source

- raw low-confidence detection
- projection failed
- geometry REVIEW
- pole base REVIEW
- unreviewed interval

`SPACING_ANOMALY`는 마지막에 추가한다.

### 구현 원칙

- deterministic
- source artifact fingerprint
- 동일 입력 재생성 시 중복 task 없음
- priority는 확률이 아님
- task 생성 근거 표시
- 사용자가 source별 on/off 가능

### 사용자 확인 Gate 6

한 트랙에서 후보 100개를 생성해 다음을 기록한다.

- 실제 확인 가치가 있는 후보 비율
- 불필요한 후보 비율
- source별 처리시간
- 전체 프레임 순회 대비 시간 절감

---

## Step 7 — 중복·관계·속성 QA

### 목표

수동 추가로 데이터 품질이 떨어지지 않게 한다.

### 구현

- duplicate preflight
- relation rule
- required/domain rule
- QA issue panel
- issue → object/frame navigation
- warning override reason

### 사용자 확인 Gate 7

의도적으로 잘못된 데이터 fixture를 넣어 오류를 정확히 찾는지 확인한다.

---

## Step 8 — Undo·작업 이력

### 목표

작업자가 최근 편집을 안전하게 되돌린다.

### 구현

- audit 기반 inverse patch
- 현재 revision CAS
- create → soft delete
- update → previous geometry/properties
- delete → restore
- task resolution과 feature edit를 하나의 edit transaction으로 연결

### 금지

- 다른 사용자의 최신 수정 덮어쓰기
- 원본 SHP bundle 수정
- audit row 삭제

### 사용자 확인 Gate 8

생성·이동·속성 수정·삭제를 각각 undo/redo한다.

---

## Step 9 — 완료율·검수 리포트·내보내기

### 목표

납품 전 `무엇을 어디까지 검수했는가`를 설명할 수 있게 한다.

### 지표

- 전체 범위
- 검수 완료 프레임/거리
- source별 task 수
- manual added
- corrected
- false positive
- field survey
- unresolved REVIEW
- open QA issue
- 작업자·시간
- 모델·run·dataset fingerprint

### 출력

- JSON
- CSV
- 간단한 HTML 또는 Markdown summary
- 기존 edited SHP ZIP
- provenance sidecar JSON

### 최종 제한 회귀

사용자 승인 후 관련 전체 테스트와 build를 한 번 실행한다.

---

## Step 10 — 선택형 능동학습 Export

별도 승인 후 진행한다.

- 작업자가 추가한 bbox
- false positive
- corrected class
- source image reference
- model version
- export manifest

자동 재학습과 자동 배포는 포함하지 않는다.

---

# 12. 테스트 전략

## 12.1 백엔드

신규 파일 권장:

- `tests/test_webapp_review_tasks.py`
- `tests/test_manual_object_tools.py`
- `tests/test_webapp_manual_objects.py`
- `tests/test_webapp_qa.py`

각 Step에서 필요한 파일만 만든다.

fixture:

- 작은 in-memory SQLite
- 20~200개 합성 점
- mock MMSO
- mock catalog
- mock frame
- mock overlay store
- seam bbox
- duplicate points

## 12.2 프런트엔드

신규 파일 권장:

- `ReviewQueue.test.tsx`
- `ObjectTemplatePanel.test.tsx`
- `QaIssuePanel.test.tsx`

기존 테스트 확장:

- `PanoramaView.test.tsx`
- `PointCloudView.test.ts`
- `OverlayContext.test.tsx`
- `Workspace.test.tsx`

테스트 목적:

- mode state
- API payload
- confirm/cancel
- frame synchronization
- keyboard
- revision conflict
- next-task transition

Three.js 실제 렌더링 품질을 jsdom에서 과도하게 검증하지 않는다. geometry helper를 pure function으로 분리한다.

---

# 13. 성능 목표

초기 목표이며 실제 데이터로 조정한다.

| 동작 | 목표 |
|---|---:|
| 작업 큐 첫 페이지 | 500ms 이하 |
| task 상태 저장 | 300ms 이하 |
| cached frame 전환 | 1초 이하 체감 |
| manual bbox proposal | 보통 2초 이하 |
| local pole base proposal | 기존 P0 수준 유지 |
| duplicate preflight | 300ms 이하 |
| QA 1,000 features | 3초 이하 |
| 저장 후 다음 항목 이동 | 500ms 이하 |

대형 원본 접근은 호스트에서 수행하고 브라우저에는 필요한 파생 데이터만 전달한다.

---

# 14. 실패 처리

| 실패 | 사용자 동작 |
|---|---|
| 점군 catalog 준비 중 | 자동 재시도 안내 |
| bbox 내부 depth 없음 | 점군 직접 선택으로 전환 |
| 복수 depth 후보 | 후보 비교 또는 다시 bbox |
| 지주 축 모호 | 다른 몸체 점 선택 |
| duplicate 경고 | 기존 선택/수정/강제 생성 |
| revision conflict | 최신 feature 새로고침 후 재적용 |
| frame source 없음 | 현장조사 또는 skip |
| required attribute 누락 | 저장 차단 |
| QA warning | 사유 입력 후 override 가능 |
| QA error | 해결 전 session 완료 차단 |

---

# 15. 완료 기준

P1 전체 완료는 다음 조건을 만족할 때다.

1. 검수 session이 재시작 후 복원된다.
2. 작업자가 task를 순서대로 처리할 수 있다.
3. 파노라마 bbox로 미검출 표지판 PointZ를 생성할 수 있다.
4. 기존 P0 지주 바닥점이 공통 template 흐름에서 동작한다.
5. 저장 전 duplicate와 required field를 검사한다.
6. 수동 객체의 근거 frame·bbox·tool·작업 이력이 보존된다.
7. 오류 목록에서 객체로 이동할 수 있다.
8. undo가 revision을 지키며 동작한다.
9. 검수 범위와 완료율을 리포트로 출력한다.
10. 기존 AI 파이프라인 결과와 P0 수동 지주 기능이 회귀하지 않는다.

---

# 16. 구현 순서 권고

가장 먼저 구현할 제품 단위는 다음이다.

```text
Review Session
  + 수동 작업 큐
  + 파노라마 bbox proposal
  + TRAFFIC_SIGN template
  + 확인하고 다음
  + duplicate warning
```

이 단위가 실제 작업자의 미검출 보정 시간을 줄이는지 먼저 확인한다.

점군 단면, 다중 template, 자동 후보 생성, undo, 리포트는 그 다음이다.

---

# 17. 참고 자료

- [EGIS XDBUILD 공식 제품 페이지](https://www.egiskorea.com/product/xd-build.html)
- [EGIS 시설관리 XDRoadMap 소개](https://www.egiskorea.com/solution/facility-management.html)
- [EGIS 2026 사업보고서의 XDRoadMap 설명](https://kind.krx.co.kr/external/2026/03/20/000003/20260320000021/11011.htm)
- [RoadInventory-MMS 기준 커밋](https://github.com/dbparkJ/RoadInventory-MMS/commit/0218e16566eeaa9191e3a4c0021dbf2e49fc0c1e)

공개 자료에는 XDRoadMap의 모든 상세 메뉴·버튼·내부 알고리즘이 공개되어 있지 않다. 이 문서의 UI와 데이터 계약은 확인된 제품 기능을 RoadInventory-MMS의 AI-first 웹 구조에 맞게 재설계한 것이다.
