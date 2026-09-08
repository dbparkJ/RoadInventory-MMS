# RoadInventory-MMS 점진적 구조 개선 — AI-agent 실행 명세

> **결정: 기존 저장소를 유지한다. 검증된 MMS 처리·좌표 계산·지주 추정·SHP 출력은 보존하고, 배포·검증·실행기·작업자 UI를 작은 변경 단위로 개선한다. 새 저장소 생성과 전면 재작성은 하지 않는다.**

| 항목 | 내용 |
|---|---|
| 대상 저장소 | `dbparkJ/RoadInventory-MMS` |
| 문서 작성일 | 2026-09-08, Asia/Seoul |
| 문서 버전 | 1.0 |
| 선행 검토 기준 | `main@aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf` |
| 선행 검토 범위 | 저장소 소스·문서·커밋·PR·Actions 이력 확인 |
| 선행 검토에서 실행하지 않은 것 | 전체 테스트, 실제 MMS/GPU 전체 처리, 운영 브라우저 성능 측정 |
| 이번 문서의 역할 | 실제 개발 환경에서 에이전트가 재확인·구현·검증할 실행 지침. 구현 완료 보고서가 아니다. |
| 기본 작업 방식 | 격리된 기능 브랜치에서 작은 변경 → 검증 → 체크포인트. `main` 직접 수정·자동 병합 금지. |
| 사용 환경 원칙 | PC 작업자 중심. Windows/WSL/Linux의 실제 지원 범위를 A0에서 확인한다. 파노라마·점군 분리 창은 유지한다. |

**주의:** 위 SHA는 선행 검토 스냅샷이지, 에이전트 실행 시점의 최신 HEAD라고 보증하는 값이 아니다. 작성일·조회일·테스트 실행일을 혼동하지 않는다. 현재 상태를 A0에서 다시 읽고, 이미 해결된 항목은 재구현하지 않는다. 선행 보고서의 과거 테스트 통과 수치를 이번 작업의 결과로 복사하지 않는다.

---

## 0. 에이전트에게 내리는 작업 지시

이 문서를 실제 개발 지시로 받은 에이전트는 **조사 보고서만 작성하고 종료하지 말고**, A0 이후 선행 조건을 충족한 구현을 진행한다. 단, 실제 데이터·GPU·권한이 없는 작업을 완료로 가장하지 않는다.

목표는 다음 네 가지다.

1. 같은 버전의 소스·프런트엔드 빌드·백엔드가 함께 배포되게 한다.
2. 실제 MMS 결과의 정확성과 기존 파일/API 계약을 보호하는 검증 장치를 만든다.
3. 웹 실행기와 처리 단계의 책임을 분리하고 장애 복구 경계를 명확히 한다.
4. 작업자가 객체 선택 → 근거 확인 → 보정 → QA → 완료를 일관되게 수행하게 한다.

기능을 추가하는 것보다 **현재 동작을 유지하면서 수정 범위를 좁히고, 오류를 재현·검증할 수 있게 만드는 것**을 우선한다.

### 0.1 이 문서의 해석

- 실제 저장소의 `AGENTS.md`, 사용자 최신 지시, 적용되는 도구·보안 제약을 먼저 확인한다. 이 문서를 이유로 승인·권한 경계를 우회하지 않는다.
- 아래의 신규 파일명·인터페이스명은 **권장안**이다. 같은 책임의 구현이 있으면 확장·재사용하며 중복 계층을 만들지 않는다.
- `P0/P1/P2/P3`는 우선순위 그룹이다. 모든 그룹을 한 번에 수정하는 거대한 PR을 만들라는 뜻이 아니다.
- 단계 완료 때마다 불필요하게 질문하지 않는다. 조건이 충족되면 다음 독립 작업으로 진행한다. 운영 데이터 변경·파괴적 조치·권한 확대는 별도 승인 없이 하지 않는다.
- 실제 검증이 막힌 경우 해당 작업과 의존 작업을 차단하되, 다른 안전한 독립 작업까지 모두 중단하지 않는다.
- 장시간 작업이 중단되거나 실행 한계에 도달하면 현재 세션에서 체크포인트를 남긴다. 나중에 자동으로 계속하거나 백그라운드에서 처리한다고 약속하지 않는다.

### 0.2 이번 개발의 비목표

- 새 GitHub 저장소 생성, 기존 저장소 폐기, 전체 코드 재작성.
- YOLO 모델·가중치·임계값 변경, 지주 계산 정확도 개선과 구조 변경의 동시 수행.
- 즉시 PostgreSQL/Redis/Kubernetes/분산 GPU 시스템으로 이전.
- 모바일 전용 화면 개발 또는 기존 PC/분리 창 기능 축소.
- 모든 의존성 최신화, 무관한 포맷 일괄 변경, 전체 디렉터리 이름 변경.
- 검수 UI가 꺼져 있다는 이유만으로 강제 활성화.
- 원본 MMS·납품 SHP를 테스트 출력으로 덮어쓰기.

---

## 1. 절대 보존해야 할 계약

### 1.1 계산·데이터 계약

| 보호 대상 | 반드시 지킬 내용 |
|---|---|
| MMS 입력 | 기존 Job/Track 탐색·필터·pose 연결·캘리브레이션 매칭 의미를 유지한다. |
| 캘리브레이션 | 누락·모호한 매칭·잘못된 schema·CRS 문제를 무시하거나 임의 기본값으로 통과시키지 않는다. |
| 좌표 | dataset CRS, 축 순서, 단위, 수직 기준을 구분한다. 수평 CRS만 바꾸고 Z도 변환됐다고 간주하지 않는다. |
| 수치 정밀도 | 저장·계산의 권위 좌표는 기존 정밀도를 유지한다. 뷰어용 local-origin/Float32 데이터를 납품 좌표로 승격하지 않는다. |
| 표지·지주 | 객체 대표점, 지주 축, 지면 교차점, `support_id`, 중복 제거 의미를 유지한다. |
| 지주 바닥점 | 단순 최저 관측점과 추정 지면 접점을 혼동하지 않는다. 기존 허용 범위·실패·검토 정책을 임의 변경하지 않는다. |
| 실패 표현 | 저장 불가 결과를 `(0,0,0)`, 카메라 위치, 최근 성공 좌표 등으로 대체하지 않는다. 실패·검토 사유를 유지한다. |
| SHP | geometry type/PointZ, 필드명·형식·폭·소수점·인코딩·CRS sidecar·파일명 계약을 유지한다. |
| JSON | 프레임별 TXT 확장자 JSON을 포함한 기존 schema와 필수 필드를 유지한다. 새 정보는 호환 가능한 별도/추가 필드로 다룬다. |
| 산출물 구분 | `point_crops`, `pole_crops`, `image_crops`, `point_previews`, `shp`의 기존 의미를 유지한다. |
| 학습용 원본 | QA용 파노라마 RGB crop을 원본 LAS record 보존 데이터로 간주하지 않는다. source file/index·원본 속성의 추적성을 손상시키지 않는다. |

구조 변경 PR에서 계산 결과가 달라지면 기본 판단은 **회귀 또는 미해명 변경**이다. 자동으로 허용오차를 넓히거나 golden 정답을 바꾸지 않는다. 의도적 알고리즘 변경은 별도 작업·근거·검증으로 분리한다.

### 1.2 실행·편집 계약

- CLI와 웹의 기존 진입점, YAML/CLI 기본값·override 의미, 실행별 설정 snapshot을 유지한다.
- manifest의 상태 전이, attempt, 설정·모델·캘리브레이션 fingerprint를 유지한다.
- 자식 프로세스 종료 코드만 보고 성공 처리하지 않는다. 유효한 manifest와 현재 실행의 완전한 산출물 검증을 유지한다.
- SHP staged publish·rollback, 원자적 파일 교체, 경로 제한, stale 산출물 배제 장치를 제거하지 않는다.
- layer revision CAS, feature mutation 멱등성, provenance, undo/redo, task-resolution outbox를 유지한다.
- proposal 생성은 읽기/계산 단계이며, 사용자가 확정하기 전 geometry를 저장하지 않는다.
- 여러 SQLite 파일을 별도 보장 없이 하나의 원자적 transaction으로 설명하지 않는다.
- 현재 단일 worker 가정을 검증 없이 해제하거나 ASGI worker 수를 늘리지 않는다.

### 1.3 사용자 경험 계약

- PC 중심으로 개선한다. 파노라마와 점군의 분리 창 기능을 제거하지 않는다.
- 지도·파노라마·점군이 같은 dataset/frame/feature 작업 문맥을 공유하게 한다.
- `N`, `B → B`, `M`, `Esc`, 프레임 이동 등 기존 단축키 의미는 실제 코드·테스트로 확인 후 보존한다.
- 입력 필드·대화상자에서 타이핑하는 키가 전역 편집 명령으로 중복 실행되지 않아야 한다.
- 임시 제안, 저장 중, 저장 완료, 충돌, 검토 필요, 저장 실패를 구분한다.
- 팝업 사용은 유지하되, 팝업 차단 때문에 핵심 편집 자체가 불가능하지 않도록 대체 경로를 만든다.
- 비활성 검수 기능은 실제 검증과 명시적인 활성화 조건을 충족하기 전까지 비활성 상태로 유지한다.

---

## 2. 선행 검토에서 발견한 항목 — 실행 시 재확인 필요

아래는 해당 스냅샷의 관찰 결과다. 현재 운영 서버의 상태나 실제 장애 발생 사실로 확대 해석하지 않는다. 근거 링크는 문서 끝의 부록 A를 참조한다.

| ID | 관찰 내용 | 올바른 해석 | 근거 |
|---|---|---|---|
| F01 | `aac6a30`에 점 선택·파노라마 성능 개선 PR #2가 병합됨 | 미병합 개선으로 취급하거나 다시 cherry-pick하지 않는다. | S01, S02 |
| F02 | 포함된 `webui/dist`의 마지막 변경은 `c8d562b`, 프런트 소스는 그 이후 변경됨 | 저장소의 소스/포함 빌드가 불일치. 운영 서버가 재빌드했는지는 별도 확인 필요. | S01, S03 |
| F03 | 기본 서버는 `webui/dist`를 제공하고, setup은 npm이 없어도 기존 index가 있으면 성공 가능 | 오래된 빌드를 최신으로 오인할 배포 경로가 있음. Node 없는 설치 지원 자체가 잘못은 아님. | S04, S05 |
| F04 | pipeline 약 405KB, overlays 약 169KB, store 약 103KB, OverlayContext 약 96KB, 전역 CSS 약 90KB | 크기만으로 판단하지 말고 책임·변경 결합도를 확인해야 함. | S06, S07, S08, S09, S10 |
| F05 | 선행 조회의 Actions에는 의존성 그래프 작업만 확인됨 | 현재 CI/조직 ruleset/외부 CI를 재조회해야 함. 테스트 자체가 없다는 뜻은 아님. | S11, S12 |
| F06 | 실제 MMS 전체 golden 비교·장시간 GPU·장애 주입 검증 공백이 문서에 기록됨 | 합성/단위 테스트 통과를 납품 정확도 증명으로 사용할 수 없음. | S12 |
| F07 | `REVIEW_WORKSPACE_UI_ENABLED = false`, 체크리스트에는 검수 UI 절차가 있음 | 의도된 기능 보류와 문서 적용 범위의 불일치를 구분해야 함. | S13, S14 |
| F08 | `PipelineContext/StageResult`는 있으나 계산은 큰 단계로 결합됨 | 계층이 전혀 없는 상태가 아님. 기존 경계를 실제 실행에 연결해야 함. | S12, S15 |
| F09 | 프레임 재사용은 있지만 독립 단계 checkpoint/retry 실행기는 미완성 | `skip_existing`과 단계 재개를 구분해야 함. | S12 |
| F10 | 재시작 후 남은 child의 안전한 소유권 증명 한계가 기록됨 | 실제 중복 GPU 작업을 재현했다고 주장하지 않는다. 복구 공백으로 검증한다. | S12 |
| F11 | 선택형 Basic 인증, worker 잠금, `operator-local`, process-local fence가 있음 | 무인증 시스템이라고 단정하지 않는다. 다중 작업자 권한 시스템과는 구분한다. | S04, S12, S16 |
| F12 | `run_history`는 과거 산출물 bytes 전체의 불변 보관을 보장하지 않음 | 특히 CLI의 같은 출력 루트 재사용을 점검한다. 웹의 실행별 출력 구조와 구분한다. | S12 |
| F13 | panorama fastpath를 함수 치환 방식으로 설치함 | 최적화 성과는 보존하되 명시적인 호출 경계를 검토한다. | S17 |
| F14 | 공통 오류 변환에서 일반 `PermissionError`를 출력 오류로 분류함 | 실제 입력 접근 실패 경로와 UI 호환성을 확인하고 분류를 보강한다. | S15 |

각 항목은 A0에서 `CONFIRMED / ALREADY_FIXED / NOT_REPRODUCED / NEEDS_ENVIRONMENT` 중 하나로 기록한다. 의도된 설계는 무조건 결함으로 분류하지 않는다.

---

## 3. 작업 권한과 변경 안전성

### 3.1 기본 허용 범위

이 문서를 구현하라는 지시를 받은 환경에서 다음을 기본 작업 범위로 삼는다.

- 저장소·테스트·설정·실행 환경을 읽고 조사한다.
- 별도 브랜치/필요한 경우 별도 worktree에서 소스·테스트·문서를 수정한다.
- 프로젝트 전용 환경에서 테스트·빌드를 실행한다.
- 합의된 개발 작업 디렉터리에 작은 합성 데이터와 테스트 산출물을 만든다.
- 검증 가능한 작은 변경을 선택적으로 stage하고 로컬 commit한다.
- push/PR은 사용자 지시와 연결 권한이 허용한 범위에서만 수행한다. 게시 권한이 명확하지 않으면 로컬 결과와 게시 준비 상태를 남긴다.

**허용되지 않는 것:** main 병합, force push, 브랜치 삭제, 운영 서비스 재시작, 불명확한 PID 종료, 원본 데이터 쓰기, DB 초기화, 운영 인증·방화벽 변경, 외부 유료 서비스 생성. 필요하면 해당 작업을 차단하고 이유를 보고한다.

### 3.2 사용자 변경 보호

- 작업 시작과 각 commit 전에 `git status`를 확인한다.
- 사용자 변경을 자동 stash/reset/clean/revert하지 않는다.
- 같은 파일에 사용자 변경이 있으면 별도 worktree로 분리하거나 충돌 없는 작은 부분만 작업한다.
- `git add .` 대신 의도한 파일·hunk만 stage한다. 모델, 원본, 생성된 큰 파일, 비밀정보를 포함하지 않는다.
- 상태 디렉터리·데이터 루트가 symlink/junction일 수 있으므로 경로 문자열만 보고 삭제하지 않는다.
- 실제 source 경로·인증정보·고객 데이터·개인정보를 보고서나 Git에 노출하지 않는다. 필요한 경로는 별칭/상대경로로 기록한다.

### 3.3 승인 없이 가능한 진행과 차단 조건

| 상황 | 행동 |
|---|---|
| 단위 테스트·코드 분리 등 비파괴 작업 | 조건 충족 시 계속 진행 |
| 원본 데이터 없음 | 합성/계약 테스트는 진행, 실제 결과 검증은 `BLOCKED_VALIDATION` |
| GPU 없음 | CPU 가능한 검증은 진행, 실제 추론·GPU 장애 검증은 차단 |
| 최신 main에서 이미 수정됨 | 증거와 테스트를 남기고 중복 구현 생략 |
| 운영 DB/서비스 변경 필요 | 로컬 복제본에서 검증하고 운영 적용은 승인 대기 |
| 기존 데이터 migration의 호환성 불명 | 활성화/배포를 차단, 백업·복구 계획부터 보완 |
| 같은 원인으로 도구 호출/명령이 반복 실패 | 원인·도구 schema를 확인하고 실행 방식을 수정. 무한 재시도 금지 |
| 의존 작업 차단 | 해당 경로만 차단하고 독립 가능한 다음 항목으로 이동 |

---

## 4. A0 — 현재 상태 재확인과 기준선 확보

**이 단계는 모든 구현의 선행 조건이다. 단, 문서 정리만 무기한 반복하지 말고 실행 가능한 작업을 확정한다.**

### 4.1 저장소와 브랜치

읽기 명령 예시이며 현재 환경에 맞게 사용한다.

```bash
git remote -v
git status --short
git branch --show-current
git rev-parse HEAD
git log -5 --oneline
git worktree list
git ls-files
```

네트워크 접근이 허용되면 remote refs를 fetch하여 실제 기본 브랜치와 비교한다. remote 이름이 항상 `origin`이라고 가정하지 않는다. 사용자 변경이 있는 작업 트리를 덮어쓰는 checkout은 하지 않는다.

기록할 내용:

- 조사 시각과 시간대, 원격 기본 브랜치 SHA, 로컬 HEAD, 작업 기준 SHA.
- 사용자 미커밋 변경 여부와 이 작업에서 수정하지 않을 파일.
- 관련 PR #2의 병합 상태 및 기존 최적화의 실제 포함 여부.
- 루트/하위 `AGENTS.md`와 기존 개선 문서의 적용 범위.
- 브랜치 보호와 CI는 권한이 허용하는 읽기 방식으로 확인한다. 조회할 수 없으면 `UNKNOWN`으로 기록한다.

권장 브랜치 접두사는 `refactor/roadinventory-...`다. 기존 성능 브랜치를 재활용하거나 삭제하지 않는다.

### 4.2 환경·데이터

- Python/Node/npm 버전, 선택된 interpreter, 프로젝트 전용 환경, 운영체제를 확인한다.
- `requirements.txt`, `ENV_SETUP.md`, setup 스크립트, 프런트 lockfile을 읽는다.
- Python 직접 의존성은 이미 고정돼 있다는 점을 확인한다. 무조건 “버전 미고정”이라고 진단하지 않는다.
- GPU·CUDA·모델 존재·가용 디스크·허용 데이터 루트는 읽기로 확인한다.
- 운영 DB/실제 서비스 프로세스와 개발 테스트 경로를 구분한다.
- 설치가 필요하면 기존 스크립트를 검토하고 프로젝트 격리 환경을 사용한다. 전역 pip/npm 설치나 OS 전체 업그레이드를 기본 조치로 삼지 않는다.
- 실제 데이터는 테스트용 별도 출력 루트로 처리한다. 데이터 전체 복사나 모델 다운로드가 필요하면 용량·권한을 먼저 확인한다.

### 4.3 현재 호출 경계 조사

최소 다음 파일과 직접 호출자를 확인한다.

```text
scripts/run_pipeline.py
scripts/run_web.py
scripts/setup_web.sh / scripts/setup_web.ps1
mms_shp_detection/pipeline.py
mms_shp_detection/app/pipeline_service.py
mms_shp_detection/infrastructure/manifest_writer.py
mms_shp_detection/webapp/app.py
mms_shp_detection/webapp/runs.py
mms_shp_detection/webapp/media.py
mms_shp_detection/webapp/panorama_fastpath.py
mms_shp_detection/webapp/overlays.py
mms_shp_detection/webapp/store.py
mms_shp_detection/webapp/task_resolution_outbox.py
webui/src/App.tsx
webui/src/components/OverlayContext.tsx
webui/src/components/ManualObjectContext.tsx
webui/src/components/ReviewContext.tsx
webui/src/components/DetachablePanel.tsx
webui/src/lib/acceleratedPointRaycast.ts
webui/src/types.ts / webui/API_CONTRACT.md
관련 Python/프런트 테스트
```

모든 파일을 한 번에 읽어 문맥을 소진하지 말고, 작업별 호출 경로·상태 저장 위치·파일 쓰기 위치를 표로 만든다. 기존 문서를 사실로 단정하지 말고 실제 코드와 대조한다.

### 4.4 기준선 테스트

프로젝트 환경에서 실행할 후보 명령이다. 설치 상태와 실제 스크립트를 확인 후 사용한다.

```bash
python -m pytest -q
npm --prefix webui ci
npm --prefix webui test -- --run
npm --prefix webui run build
```

- baseline 실행은 수정 전 상태 또는 격리된 기준 worktree에서 수행한다.
- `npm ci`/빌드가 작업 트리에 남기는 generated 차이를 기록한다.
- 전체 suite가 실행 불가하면 테스트 단위별로 환경 오류·기존 실패·실제 새 실패를 구분한다.
- 테스트를 삭제하거나 전체 skip으로 바꾸어 초록색 결과를 만들지 않는다.
- GPU 모델을 import하는 CPU 테스트는 무턱대고 mock하지 않는다. 계약 테스트와 실제 통합 테스트의 목적을 분리한다.
- exit code, pass/fail/skip 수, 실제 환경, 로그 위치를 남긴다. 실행하지 않은 테스트에 성공 표시를 하지 않는다.

### 4.5 A0 산출물과 통과 조건

기존에 같은 역할의 문서가 있으면 통합한다. 권장 경로는 `docs/refactor/`다.

| 파일 | 최소 내용 |
|---|---|
| `BASELINE.md` | 기준 SHA·환경·F01~F14 판정·현재 실패·권한 범위 |
| `CONTRACTS.md` | 입력/출력/API/좌표/편집/실행/배포의 보호 계약과 테스트 위치 |
| `WORK_ITEMS.md` | 아래 작업 ID·의존성·상태·검증 증거 |
| `VALIDATION.md` | 테스트 명령·결과·실제 MMS 검증 여부·성능 기준선 |
| `CHECKPOINT.md` | 마지막 완료 작업·남은 차단·다음 구체적인 작업 |

**A0 통과:** 실제 기준 SHA와 사용자 변경 보호가 확인되고, 최소 실행 가능한 테스트가 확보됐으며, 다음 구현 단위가 명확하다. 운영 데이터 부재가 A0 전체를 무기한 막아서는 안 된다.

---

## 5. 작업 순서와 의존성

| ID | 작업 | 선행 조건 | 활성화/완료의 핵심 조건 |
|---|---|---|---|
| A0 | 현재 상태·기준선 | 없음 | 기준 SHA·계약·환경·실패 구분 |
| P0-1 | 빌드·배포 버전 일치 | A0 | 오래된 dist 감지, 동일 소스 패키징 검증 |
| P0-2 | 자동 테스트·빌드 CI | A0 | 실제 테스트·build 실패가 실패로 전파됨 |
| P0-3 | 결과 비교 harness·golden | A0 | 합성 계약 + 실제 MMS 검증 상태 분리 |
| P0-4 | 상태·기능 문서·오류 분류 | A0 | 실제 활성 기능과 문서 일치, 오류 호환 |
| P1-1 | 공통 실행 경계·child 소유권 | P0-2 | 취소·재시작·중복 실행 방어 검증 |
| P1-2 | 처리 단계 점진 추출 | P0-2, P0-3 | 계산 경로 변경 시 실제 결과 동등성 |
| P1-3 | 산출물 불변 보관·백업/복원 | P0-2 | 과거 결과 보존 + 별도 디렉터리 복원 검증 |
| P1-4 | checkpoint·제한적 재시도 | P1-1, 관련 P1-2 | 입력/설정/단계 fingerprint 및 실패 정책 |
| P2-1 | UI 상태·편집 흐름 분리 | P0-2, P0-4 | popup/단축키/빠른 전환/저장 경합 검증 |
| P2-2 | 성능 경계·명시적 preview 구현 | P0-1, P0-2 | 선택 정확도·캐시·cold/warm 측정 |
| P2-3 | 검수 workspace 공개 준비 | P2-1, 실제 데이터 | 기존 false를 자동 true로 바꾸지 않음 |
| P3-1 | 단일 운영 보안·사용자 경계 | P0-2 | 기존 인증·경로 안전성 유지, 사용자 식별 정합 |
| P3-2 | 다중 작업자/배포 확장 설계 | P1/P2의 관련 작업 | 실제 요구·부하·승인 근거가 있을 때만 구현 |
| R0 | 릴리스·롤백·최종 검수 | 적용하는 기능별 gate | 소스/빌드/DB/결과/운영 검증 상태 일치 |

**금지:** 위 표를 “모든 작업이 실제 검증되지 않아도 일단 코드를 모두 연결한다”로 해석하지 않는다. blocked gate를 우회하는 feature flag나 임의 mock 성공값을 만들지 않는다.

## 6. P0 — 배포와 검증의 신뢰성 확보

### P0-1. 소스·프런트 빌드·서버 버전 정합성

**대상 후보:** `scripts/setup_web.*`, `scripts/run_web.py`, `webapp/app.py`, `webui/package.json`, 빌드 설정, CI/릴리스 스크립트.

#### 구현 순서

1. **실제로 어떤 정적 디렉터리를 제공하는지 확인한다.** `static_dir` override, 작업 디렉터리, 기존 dist, 설치 스크립트 분기를 추적한다.
2. 현재 프런트 소스에서 새 빌드를 만들고 포함된 dist와 차이를 확인한다. 오래된 빌드가 제공되는 조건을 테스트로 재현한다.
3. 빌드 출처를 표현하는 manifest를 설계한다. 예시는 아래와 같으며 필드명은 기존 계약과 충돌하지 않게 정한다.
4. 백엔드와 프런트가 같은 릴리스에서 생성됐는지 검사하는 명령을 만든다. 버전 조회는 기존 bootstrap의 추가 필드 또는 작은 전용 endpoint로 제공한다.
5. 설치·실행 경로에 검증을 연결하고 개발 모드와 운영 모드의 정책을 명시한다.
6. CI가 같은 checkout에서 프런트 빌드와 백엔드 패키지를 생성하도록 한다.
7. 검증된 배포 패키지가 생긴 이후에만 `dist`의 Git 추적 제거를 별도 변경으로 검토한다.

#### 권장 build manifest

```json
{
  "schema_version": 1,
  "build_id": "<release-build-identifier>",
  "source_commit": "<full-commit-sha-or-explicitly-unavailable>",
  "source_fingerprint": "<sha256-of-declared-source-inputs>",
  "frontend_lock_hash": "<sha256>",
  "api_contract_version": "<actual-supported-version>",
  "built_at": "<actual-ISO-8601-time>",
  "assets_manifest_hash": "<sha256>"
}
```

- source fingerprint에 포함할 입력을 명시한다. 프런트 소스, public 파일, 빌드 설정, lockfile 등 실제 결과에 영향을 주는 항목을 포함한다.
- generated dist·build manifest 자체를 입력에 넣어 hash가 순환하지 않게 한다.
- dirty source를 clean commit의 결과인 것처럼 표시하지 않는다. Git 없는 릴리스에서도 검증할 수 있는 source fingerprint를 함께 사용한다.
- `source_commit`만 기록하고 실제 수정된 소스를 무시하는 검사로 끝내지 않는다.
- 소스 fingerprint와 artifact checksum은 정합성 확인용이다. 서명 검증을 구현하지 않았다면 “공급망 서명 검증”이라고 표현하지 않는다.
- public 응답에는 서버 절대경로, 사용자명, 비밀번호, 환경 변수 전체를 포함하지 않는다.

#### 설치·배포 정책

| 상태 | 요구 동작 |
|---|---|
| npm 사용 가능 | lockfile 기반 설치 → build → manifest/assets 검증 |
| npm 없음 + 검증된 패키지 | 일치하는 검증된 빌드로 실행 가능 |
| npm 없음 + index만 있음 | 최신 빌드로 간주하지 않는다. 운영 모드에서는 실패 또는 검증된 릴리스 설치 안내 |
| source/build 불일치 | 개발 모드는 명확한 경고, 운영 릴리스는 적용 차단 |
| 기존 설치에 metadata 없음 | 호환 이행 절차를 제공한다. 조용히 최신이라고 표시하지 않는다. |
| 빌드 실패 | 이전 성공 dist를 새 성공 빌드처럼 사용하지 않는다. |

운영 모드/개발 모드 구분이 없다면 새 설정을 작게 도입하고 기본값·기존 사용자 이행 절차를 문서화한다. 운영 서버의 설치를 예고 없이 깨뜨리는 변경은 하지 않는다.

릴리스 전환은 실행 중인 dist 위에서 파일을 하나씩 교체하는 방식보다 검증된 별도 release 디렉터리 전환을 우선 검토한다. 기존 탭이 참조하는 이전 hash asset의 유지 정책도 정의한다. `index.html`의 갱신 정책과 hash asset의 캐시 정책을 구분하고 기존 no-cache/immutable 설정을 불필요하게 약화시키지 않는다.

#### 필수 테스트

- 최신 source → build → 검증 성공.
- source만 변경 → stale build 검출.
- asset 누락/변조, index가 참조한 asset 없음 → 실패.
- build 실패 뒤 이전 dist가 남아 있음 → 새 성공으로 처리되지 않음.
- npm 없는 환경에서 검증된 패키지는 허용, index만 있는 낡은 패키지는 최신으로 승인하지 않음.
- Windows와 Linux에서 경로 구분자/줄바꿈 차이로 불필요한 source hash 불일치가 나지 않음. 실제 bytes와 정규화 정책을 명시한다.
- 브라우저에서 backend/frontend build 정보를 확인할 수 있음.

**완료 조건:** “git pull 후 서버 재시작만 했는데 새 프런트 기능이 적용되지 않는 상황”을 식별할 수 있고, 검증된 동일 소스 릴리스를 만드는 실행 경로가 존재한다.

---

### P0-2. 실제 실패를 차단하는 CI

**대상 후보:** `.github/workflows/`, 테스트 설정, 프로젝트 개발 의존성, 릴리스 검증 명령.

#### 작업

1. 기존 repository/조직 CI를 확인한다. 이미 있는 것을 중복 생성하지 않는다.
2. Python 테스트, TypeScript 검사, 프런트 테스트, production build를 각각 실패가 전파되는 단계로 구성한다.
3. Linux와 Windows에서 중요한 파일 잠금·경로·subprocess 차이를 검증한다. 비용 때문에 전체 matrix를 줄여야 하면 실제 지원 범위와 미검증 범위를 명시한다.
4. Python/Node 버전과 패키지 설치 경로를 프로젝트의 검증된 범위에 맞춘다. 직접 고정 버전이 있어도 전이 의존성/플랫폼 차이가 남는지 확인한다.
5. Python 검사 도구가 없다면 개발 전용 의존성으로 분리한다. runtime 요구 패키지를 무조건 늘리지 않는다.
6. lint 부채는 baseline과 신규 위반을 구분한다. 저장소 전체 자동 포맷을 첫 PR에 섞지 않는다.
7. CPU 계약 테스트와 실제 GPU/MMS 통합 검증을 분리한다. GPU가 없는 일반 CI가 실제 정확도 검증까지 통과한 것처럼 표시되지 않게 한다.
8. CI status 이름과 운영자가 설정해야 할 required checks를 문서화한다. 권한 없이 branch protection/ruleset을 바꾸지 않는다.

#### 보안·정확성 기준

- 일반 검증 workflow는 필요한 최소 권한을 사용한다.
- 신뢰하지 않는 PR 코드를 운영 데이터·비밀정보가 있는 self-hosted GPU runner에서 자동 실행하지 않는다.
- `pull_request_target`과 신뢰하지 않는 코드 checkout을 결합해 비밀정보를 노출하는 경로를 만들지 않는다.
- GPU/MMS 검증은 신뢰된 브랜치·승인된 수동 실행 등 별도 정책으로 제한한다.
- `continue-on-error`, 무조건 성공하는 wrapper, 전체 테스트 skip으로 품질 gate를 무력화하지 않는다.
- 테스트 결과·실패 로그·빌드 출처는 artifact로 보관하되 원본 데이터와 비밀정보를 포함하지 않는다.
- 의존성 문제를 해결하려고 `npm audit fix --force`나 대규모 버전 업그레이드를 자동 수행하지 않는다.

#### 필수 테스트

의도적으로 작은 실패를 넣은 임시 로컬/테스트 브랜치 상태에서 테스트 또는 build가 비정상 종료하는지 확인하고 해당 실패 변경은 최종 코드에 포함하지 않는다. workflow YAML이 존재하는 것만으로 성공 판정을 하지 않는다. 원격 workflow를 실행하지 못했으면 `로컬 명령 검증 완료 / 원격 CI 실행 미확인`으로 구분한다.

**완료 조건:** 신규 코드 결함·타입 오류·build 실패를 실제 비정상 상태로 보고하는 검증 경로가 있고, required check 설정 여부가 명시돼 있다.

---

### P0-3. 실제 MMS 기준 데이터와 결과 비교 harness

**핵심:** 결과 비교 도구를 만드는 것과 실제 데이터로 결과 동등성을 확인하는 것은 다른 완료 항목이다.

#### 1) 데이터 세트 구분

| 세트 | 목적 | 처리 |
|---|---|---|
| 합성 소형 fixture | 좌표·schema·저장·오류 계약의 빠른 테스트 | 작은 안전한 fixture는 Git 포함 가능 |
| 실제 MMS 대표 fixture | 입력→추론→투영→지주→SHP/JSON 회귀 | 원본/가중치는 승인된 비공개 저장소에 보관 |
| 장애/성능 fixture | 강제 종료·큰 점군·빠른 화면 전환 등 | 운영 복제본·합성 데이터, 별도 출력 |

실제 fixture의 권장 사례: 정상 표지·지주, 가림/희소 점군, 관측 지주 하단과 지면 간격, 식생/가드레일 잡음, 경사지/국소 설치면, 여러 Job/Track, 동일 시설물의 복수 프레임 관측, 파노라마 seam을 지나는 bbox, 빈 검출, 의도된 calibration/CRS 오류.

작은 대표 범위부터 시작한다. 전체 MMS 원본을 Git이나 일반 CI artifact에 올리지 않는다.

#### 2) fixture manifest

실제 값은 조사하여 채운다. 아래 placeholder를 실행 성공 사례처럼 사용하지 않는다.

```json
{
  "schema_version": 1,
  "fixture_id": "<fixture-id>",
  "source_kind": "real_mms",
  "input_fingerprint": "<sha256>",
  "model_fingerprint": "<sha256>",
  "calibration_fingerprint": "<sha256>",
  "config_fingerprint": "<sha256>",
  "baseline_commit": "<tested-commit>",
  "coordinate_contract": {
    "horizontal_crs": "<verified-definition>",
    "vertical_reference": "<verified-or-explicitly-unknown>",
    "units": "<verified-units>"
  },
  "required_outputs": ["<actual-output-bundles>"],
  "comparison_profile": "<reviewed-profile-id>"
}
```

#### 3) 비교 대상

- 입력 선택: 처리 frame/track 수와 선택 identity.
- 검출: 객체 수, 클래스별 수, confidence/검출 근거, 유효·검토·실패 사유 분포.
- 위치: 표지 대표점·지주 바닥점의 XY/Z 오차, 유효 좌표 여부.
- 관계: `support_id`, 관측 병합, 중복 제거, 표지-지주 연결.
- 납품: SHP feature 수·geometry type·Z·속성·한글 인코딩·CRS sidecar.
- 프레임 JSON과 SHP 사이의 **정책을 반영한** 대응 관계. 여러 관측이 하나로 병합되므로 raw detection 수와 SHP 수를 무조건 같게 검사하지 않는다.
- crop: 기존 산출물 의미, 원본 속성/source identity 보존 경로, QA용 파생 데이터 구분.
- 안전성: 실패한 run에서 성공 결과가 노출되지 않는지, 현재 attempt 결과만 귀속되는지.

#### 4) 매칭·허용오차

- 안정적인 ID가 있으면 그것으로 매칭한다.
- ID가 변할 수 있으면 클래스·관계·공간 제약을 포함한 일대일 매칭을 사용한다. 독립 nearest 매칭으로 여러 객체를 같은 기준 객체에 붙이지 않는다.
- unmatched/ambiguous 객체를 숨기거나 성공 매칭에서 제외해 오차를 낮추지 않는다.
- timestamp, run_id, 임시 경로 등 비교에서 제외할 비결정적 필드는 명시적 allow-list로 관리한다. 모든 metadata를 통째로 제외하지 않는다.
- 허용오차는 권위 좌표의 정밀도, LAS scale, 실제 동일 코드 반복 실행 분산을 근거로 결정한다.
- 보기 좋은 결과를 만들려고 임의로 `0.1m` 같은 허용오차를 부여하지 않는다. 정해지지 않은 값은 release blocker다.
- 실제 추론의 비결정성과 구조 변경의 영향을 분리한다. 필요하면 고정 검출 결과를 입력으로 하는 downstream 비교와 전체 추론 비교를 함께 둔다.
- SHP는 파일 전체 bytes가 아니라 의미 비교가 필요할 수 있다. 반대로 원본 record 보존을 보장하는 경로는 필요한 원본 byte/필드 보존을 별도로 검사한다.

#### 5) 권장 harness

권장 파일명은 `scripts/compare_mms_results.py`, `tests/golden/`, `docs/refactor/GOLDEN_DATASET.md`다. 기존 도구가 있으면 재사용한다.

도구는 최소 `PASS / FAIL / BLOCKED_VALIDATION`을 구분하고 다음을 출력한다.

```text
baseline/candidate source 및 fixture 식별 정보
비교한 frame·feature 수
missing/extra/ambiguous feature
XY/Z 차이 및 최대 오차 사례
속성/schema/CRS 차이
관계·실패 사유 차이
의도적으로 제외한 비결정 필드
실제 데이터 검증 여부
```

릴리스의 `require-real` 검증 경로에서 실제 데이터가 없으면 반드시 실패/차단으로 종료한다. 선택적 로컬 실행의 데이터 없음은 별도 상태로 보고하되 성공으로 바꾸지 않는다.

#### 완료 gate

- **P0-3a 도구 완료:** 합성 정상/오류 fixture로 비교기의 false pass를 방지했다.
- **P0-3b 실제 검증 완료:** 동일 실제 fixture·설정·모델로 baseline/candidate를 실행하여 승인된 비교 기준을 통과했다.
- P0-3a만 완료된 상태에서는 계산 경로의 실제 결과 동등성을 주장하지 않는다. 계산 경로 변경의 운영 활성화는 P0-3b가 필요하다.

---

### P0-4. 기능 상태·운영 문서·오류 진단 정리

#### 기능 상태

`계획 → 구현됨 → 자동 검증됨 → 실제 MMS 검증됨 → 배포 활성화됨`을 구분한다.

- `REVIEW_WORKSPACE_UI_ENABLED`의 현재 값과 이유를 확인한다.
- 운영 체크리스트를 **현재 활성 기능**과 **검수 workspace 활성화 후 검증**으로 나눈다.
- 실제 capability, UI 공개 정책, 사용자 권한을 구분한다. API에 구현돼 있다는 이유만으로 사용자 메뉴를 공개하지 않는다.
- 서버 capability와 배포 flag를 결합하는 정책을 도입해도 default는 기존 동작을 유지한다.
- `operator-local`은 단일 운영 식별자임을 명확히 한다. 이를 인증된 다중 사용자 이력처럼 표시하지 않는다.
- 과거 설계 문서와 현재 아키텍처를 구분하고, 완료됐다는 설명과 비활성 상태를 혼동하지 않게 한다.

#### 오류 분류

`pipeline_error_info()`와 호출부를 조사하여 입력 권한 오류가 출력 오류로 분류되는 경로를 검증한다. 단순히 모든 `PermissionError`를 반대 코드로 바꾸지 않는다.

권장 분류 축:

```text
operation: read_input / write_output / load_model / validate_config / publish / recover
stage: 실제 실패 단계
code: 안정적인 기계 판독 코드
retryable: 실제 정책에 근거한 값
cause_type: 원 예외 타입
message: 민감정보를 가린 작업자 안내
```

예: 입력 read 권한 부족, 출력 write 권한 부족, 디스크 부족, 잘못된 calibration, 모델 없음, 실행 소유권 불명은 구별한다. `PermissionError`는 일시 오류로 자동 재시도하지 않는다.

기존 API 소비자가 오류 코드를 사용하면 호환 alias/새 필드/API 버전 정책을 정한다. 내부 로그에는 원인 연결을 보존하되 외부에는 절대경로·비밀정보를 노출하지 않는다. 오류를 기록하다 manifest 저장도 실패하면 원래 실패를 잃지 않게 두 오류를 구분해 남긴다.

**완료 조건:** 활성 기능과 검증 절차가 일치하고, 읽기·쓰기 실패 안내가 재현 테스트로 구분되며 기존 API/UI 계약을 해치지 않는다.

## 7. P1 — 실행·저장·계산 경계 개선

### P1-1. 공통 실행 경계와 child 소유권·복구

**현재 출발점:** API 요청 안에서 YOLO를 직접 돌리는 구조로 되돌리지 않는다. 기존 웹→별도 CLI subprocess 경계를 활용한다.

#### 1) 공통 실행 인터페이스 추출

- 웹 `RunManager`의 준비·실행·취소·결과 검증과 CLI의 실제 처리를 구분한다.
- 기존 `PipelineContext`와 manifest를 재사용하는 얇은 `JobExecutor` 또는 동등한 인터페이스를 만든다.
- 함수 이동만으로 호출 책임이 명확해지는 부분부터 분리한다.
- 모델/대형 점군 cache는 job/process-local 수명으로 관리한다. 서버 import 때 무조건 GPU 모델을 로딩하도록 만들지 않는다.
- 기존 job_id, attempt, pending manifest, request/effective config hash 검증을 유지한다.

권장 책임:

```text
JobExecutor
  prepare(job)       입력·실행 계약 확정
  start(job)         격리 실행 시작·소유권 기록
  observe(job)       상태·heartbeat·결과 근거 확인
  request_cancel(job) 소유권 확인 후 취소 요청
  reconcile(job)     재시작 상태 조정
  validate_result(job) 현재 attempt의 완전한 산출물 검증
```

메서드명은 예시다. 현재 동기/비동기 구조와 충돌하는 프레임워크를 억지로 추가하지 않는다.

#### 2) 소유권 정보

PID만으로 소유권을 증명하지 않는다. 다음 정보를 조합하되 실제 운영체제에서 확인 가능한 보장만 사용한다.

- run_id, attempt, 임의 launcher UUID.
- child PID와 process start identity.
- process group/Windows Job Object 또는 동등한 격리 식별자.
- 실행 중인 child가 확인한 job/attempt identity.
- durable 실행 상태와 heartbeat.

PID·시각·UUID를 기록했다는 사실만으로 OS 프로세스 identity가 검증됐다고 주장하지 않는다. PID 재사용과 부모만 종료된 경우를 테스트한다. command line/환경 변수에 비밀정보를 넣거나 그대로 로그에 내보내지 않는다.

#### 3) 상태 전이·원자성

- queue claim, starting, running, cancellation, terminal 상태 전이는 기존 CAS를 활용한다.
- spawn 직전/직후 crash에서는 child가 실제 시작됐는지 불명확할 수 있다. 이 경우 임의 재실행하지 않는다.
- durable success와 늦은 취소·shutdown의 경쟁에서 기존 성공 우선 정책을 훼손하지 않는다.
- 단순한 “exactly once”를 주장하지 않는다. 불확실한 경계에서는 보수적으로 차단하고, 허용된 재시도는 멱등성과 명확한 소유권에 의존한다.
- `--no-run-worker` 의미를 유지한다. 웹 화면을 띄웠다는 이유로 GPU 작업이 자동 시작되지 않게 한다.

#### 4) 재시작 정책

| 재시작 시 관찰 | 동작 |
|---|---|
| 아직 시작되지 않았음이 증명됨 | 기존 정책에 따라 재queue 가능 |
| 소유 child 생존과 identity 확인됨 | 설계된 재연결/감시 정책 적용. 중복 start 금지 |
| 소유 child 종료 + 완전한 success artifact 확인 | success 조정 가능 |
| 종료됐으나 결과 불완전 | 실패/중단으로 조정, 결과 공개 차단 |
| child 존재 여부/소유권 불명 | 관련 GPU 실행 lane 차단, 수동 점검 상태 표시 |
| PID가 존재하지만 다른 프로세스 | 절대 종료하지 않음 |

소유권 불명 상태에서도 가능한 읽기·검수 화면까지 불필요하게 차단하지 않는다. 운영 PID를 자동으로 정리하는 데모 코드를 작성하지 않는다.

#### 5) 취소·자원 해제

- child와 그 하위 worker의 정리 범위를 명확히 한다.
- Linux process group과 Windows 동작을 별도 검증한다. 신뢰되지 않은 PID에 무조건 kill/taskkill을 실행하지 않는다.
- API 요청이 취소됐지만 CPU/점군 작업 thread가 실행 중인 경우 실제 작업 종료까지 semaphore/reader 수명을 유지한다.
- 테스트용 subprocess는 해당 테스트가 만든 프로세스만 종료한다.

#### 필수 테스트

spawn 전 crash, spawn 직후 crash, running 중 부모 종료, child 완료 후 DB 반영 전 crash, 취소와 성공의 경쟁, PID 재사용, 살아 있는 orphan, 중복 queue claim, request 취소와 reader 정리, `--no-run-worker`, worker lock 충돌.

**완료 조건:** 로컬 복제 환경에서 재현 가능한 lifecycle 테스트가 있으며, 소유권 불명 child를 죽이거나 중복 GPU 작업을 시작하지 않는다. 실제 OS/GPU 검증 여부는 별도로 남긴다.

---

### P1-2. `pipeline.py`를 계산 의미 변경 없이 단계적으로 분리

#### 추출 순서

| 순서 | 책임 | 위험/방법 |
|---|---|---|
| 1 | 입출력 계약·오류 정보·설정 전달 | 타입·wrapper부터, 기존 args/default 보존 |
| 2 | 입력 탐색·calibration preflight 연결 | 이미 분리된 resolver 재사용 |
| 3 | 실행 orchestration·결과 집계 | 기존 함수 호출 순서·병렬 정책 유지 |
| 4 | JSON/SHP 출력 조립·품질 검증 연결 | 기존 writer/publisher 재사용 |
| 5 | 2D 검출 호출 경계 | 동일 모델·설정·입출력, golden 필요 |
| 6 | 투영·지주 추정의 호출 경계 | 수치 알고리즘은 그대로, golden 필수 |

**주의:** 디렉터리 이동만으로 architecture 개선을 완료했다고 하지 않는다. I/O·계산·상태 기록의 호출 책임과 테스트가 분리돼야 한다.

#### 각 추출 작업의 순서

1. 기존 함수의 입력·반환·예외·파일 쓰기·mutable 상태를 기록한다.
2. 현재 동작을 고정하는 characterization test를 먼저 추가한다.
3. 동일 함수를 호출하는 adapter/서비스로 하나의 책임을 옮긴다.
4. 기존 import/public entrypoint는 compatibility wrapper로 유지한다.
5. 이전과 새 경로를 같은 fixture에서 비교한다.
6. 실제 계산 경로가 바뀌면 P0-3b를 통과하기 전 운영 활성화하지 않는다.
7. 다음 책임으로 이동한다. 한 PR에서 함수 이동과 알고리즘 변경을 함께 하지 않는다.

#### 권장 구조 방향

```text
mms_shp_detection/
  app/
    pipeline_service.py        # 기존 실행 서비스 확장
    job_executor.py            # 필요할 때 추출
    stages/                    # 실제 입출력 책임이 생긴 단계만 추가
  domain/
    models.py                  # 기존 불변 결과/오류 모델 확장
    calibration.py             # 기존 resolver 유지
  infrastructure/
    manifest_writer.py         # 기존 durability 유지
    ...                        # 실제로 공통화 가능한 저장/프로세스 adapter
  pipeline.py                  # 호환 진입점 + 점차 축소되는 orchestration
  geometry.py / pointcloud.py / pole.py / shp_writer.py
```

위 구조를 맞추기 위해 모든 파일을 옮기지 않는다. 새로운 공통 abstraction이 기존 코드보다 이해하기 어렵다면 도입하지 않는다.

#### 타입·상태·manifest

- `Namespace`/dict를 한 번에 전부 제거하지 않는다. 단계 경계에서 검증된 typed view와 compatibility adapter를 둔다.
- mutable runtime 정보와 재현성에 포함되는 설정을 분리한다. 내부 실행 필드가 config hash를 의도치 않게 바꾸지 않게 한다.
- 모든 point/record 배열에 동일 mask/index 변환을 적용해야 하는 기존 계약을 보존한다.
- source index를 좌표 nearest-match로 복원하지 않는다.
- stage 분리 때문에 기존 `detect_project_and_estimate` 진행률·terminal 조건이 깨지지 않게 한다. 필요하면 additive 세부 단계 정보를 먼저 도입한다.
- manifest를 두 계층이 중복 종료하거나 실패 stage가 남았는데 success를 기록하는 경로를 만들지 않는다.
- 거대한 NumPy 배열을 매 단계 JSON 직렬화하는 방식으로 분리하지 않는다. 실제 checkpoint에 필요한 경계에서만 적절한 artifact를 사용한다.

**완료 조건:** 이전 public 호출과 결과가 유지되고, 하나의 책임이 독립 테스트 가능해졌으며, 실제 계산 전환에는 golden 증거가 연결돼 있다.

---

### P1-3. 과거 산출물 보존과 일관된 백업·복원

#### 1) 산출물 보존

- 웹의 기존 실행별 output 디렉터리를 유지·검증한다.
- CLI `output_dir` 기본 의미와 기존 납품 파일 경로를 조용히 바꾸지 않는다.
- CLI에 run-scoped 출력이나 immutable archive를 추가한다면 명시적 모드·이행 절차·호환 경로를 제공한다.
- `run_history`의 manifest 보관과 실제 산출물 bytes 보관을 구분해서 표시한다.
- 과거 결과를 보존하는 경우 파일 hash·크기·논리 상대경로·run/attempt를 함께 기록한다.
- 원본 데이터 전체를 매번 복사하지 않는다. 원본 참조와 fingerprint, 실제 생성 산출물의 보존 범위를 구분한다.
- retention/자동 삭제는 정책 없이 도입하지 않는다. 용량 부족을 이유로 과거 납품 결과를 자동 삭제하지 않는다.

권장 논리 구조:

```text
run/attempt 식별자
  ├─ 요청/effective config snapshot
  ├─ manifest와 validation report
  ├─ 이번 실행이 선언한 산출물
  ├─ 산출물 checksum 목록
  └─ provenance 및 필요한 버전 정보
```

#### 2) 원자적 파일 쓰기

`manifest_writer`, 일반 artifact writer, `security.atomic_replace_bytes`, preview writer 등의 호출처를 조사한다.

- 임시 파일명이 같은 프로세스 내 동시 쓰기에서 충돌하는지 테스트한다. PID만 있는 이름이 실제 경합 경로에 쓰이는지 확인 없이 취약점으로 단정하지 않는다.
- 필요한 곳은 안전한 unique temp, flush/fsync, replace, 정리 정책으로 통일한다.
- 원자적 rename과 동시 writer 직렬화는 다른 보장이다. read-modify-write에는 적절한 lock/CAS가 별도로 필요하다.
- 크고 민감한 artifact를 JSON manifest와 같은 방식으로 무조건 반복 복사하지 않는다.
- write/flush/replace 실패 시 이전 최종본을 보존하고 임시 파일을 안전하게 정리한다.
- path containment와 symlink/junction 검사 범위를 축소하지 않는다.

#### 3) 백업·복원

백업 대상은 registry DB 하나가 아니다. 레이어별 feature DB, outbox, 실행 산출물, 설정/버전 메타데이터의 일관성이 필요하다.

1. 개발 복제본에서 실제 writer 경로를 조사한다.
2. maintenance/write fence 또는 검증된 snapshot 절차를 설계한다. 독립 DB 백업을 연속 실행했다고 전역 일관성이 생겼다고 주장하지 않는다.
3. WAL 사용 DB의 본체 파일만 실행 중 복사하지 않는다. SQLite backup API 등 검증된 방식을 사용하되 다중 DB 시점 일관성은 별도로 보장한다.
4. outbox를 모두 반영한 일관된 시점으로 만들거나, pending intent를 포함해 복구 가능한 상태로 명시적으로 보존한다.
5. CLI/다른 프로세스 writer가 fence 밖에서 쓰지 않는지 확인한다. 증명할 수 없으면 유지보수 중 정지/검증된 저장소 snapshot 같은 추가 조건을 요구한다.
6. 완전히 별도의 테스트 디렉터리에 복원한다.
7. 피처 수·revision·provenance·task 상태·미반영 intent·산출물 hash를 검사한다.

#### 필수 테스트

같은 CLI output root 재사용 시 과거 bytes 보존 정책, SHP 일부 쓰기 실패, 디스크 부족, replace 실패, manifest만 남은 불완전 run, registry/feature DB 불일치, pending outbox가 있는 복원, 한글 경로·속성, 기존 DB의 additive migration 반복 실행.

**완료 조건:** 백업 명령의 존재가 아니라 실제 분리 경로 복원과 계약 검증 성공 증거가 있다. 운영 백업 실행·기존 산출물 이동은 별도 승인 범위다.

---

### P1-4. 단계 checkpoint와 제한적 재시도

**선행 조건:** 실행 소유권과 재사용할 단계 입출력이 명확해야 한다. 상태 enum에 `retrying`이 있다는 이유만으로 자동 retry를 붙이지 않는다.

#### checkpoint 유효성

재사용 조건을 하나의 검증 함수로 모은다.

```text
동일 input fingerprint
AND 동일 effective configuration
AND 동일 model/calibration identity
AND 호환 stage/method/schema version
AND 의존 단계 fingerprint 일치
AND 완전한 출력 목록·크기/hash/schema 검증
AND 실패/부분 쓰기 상태가 아님
```

- 파일 존재만으로 완료로 처리하지 않는다.
- 현재 attempt와 이전 attempt에서 재사용한 artifact의 출처를 구분한다.
- 기존 프레임 `skip_existing` fingerprint와 새 checkpoint가 충돌하지 않게 한다.
- 좌표·지주 계산 입력이 바뀌면 downstream 결과를 무효화한다.
- checkpoint 버전이 알 수 없으면 보수적으로 재계산하거나 명시적으로 차단한다. 임의 최신 버전으로 해석하지 않는다.
- 재개 실패가 원본 출력이나 이전 검증 완료 결과를 손상시키지 않아야 한다.

#### retry 분류

| 원인 | 기본 정책 |
|---|---|
| calibration/CRS/schema 오류, 입력 누락 | 비재시도. 원인 수정 필요 |
| 입력/출력 권한 부족 | 기본 비재시도. 무한 backoff 금지 |
| 검증된 일시 I/O 오류 | 횟수·대기·중복 쓰기 정책이 있는 경우만 제한 재시도 |
| CUDA OOM | 기존 정책 먼저 유지. 자동으로 모델/정밀도/결과 기준 변경 금지 |
| child identity 불명 | 재실행 차단 |
| artifact 손상 | 손상 범위를 격리하고 검증된 이전 단계부터 재실행 |
| 취소됨 | 사용자 의도 없이 자동 부활시키지 않음 |

retry마다 attempt·원인·재사용 단계·실제로 실행한 단계를 기록한다. 실패 시 안전하게 종료하고, 이미 시작한 작업의 소유권을 잃은 상태에서 새 작업을 시작하지 않는다.

**완료 조건:** 손상·입력 변경·중도 종료·중복 재요청에서 false reuse가 없고, 정상 재개 결과가 baseline과 일치한다. 실제 데이터 검증 전에는 계산 재개 기능의 운영 사용을 차단한다.

---

## 8. P2 — 작업자 UI와 체감 성능 개선

### P2-1. UI 상태와 편집 workflow 분리

**원칙:** React 교체나 새 상태관리 라이브러리 설치부터 시작하지 않는다. 현재 상태·요청·작업 문맥의 소유권을 먼저 나눈다.

#### 책임 분리 후보

| 책임 | 포함할 상태/동작 | 섞지 말아야 할 것 |
|---|---|---|
| DatasetWorkspace | dataset/track/frame 선택·목록·범위 | 피처 저장 transaction |
| ViewerSelection | 선택/hover/focus·뷰어 동기화 | 실행 큐·전체 레이어 재조회 |
| FeatureEditing | draft/proposal/commit/충돌·undo/redo | bootstrap·데이터 탐색 전체 |
| LayerCatalog | 레이어 목록·가시성·revision·feature cache | 지주 추정 내부 알고리즘 |
| ReviewSession | session/task/QA/completion | 일반 미리보기 로더의 수명 전체 |
| RunManagement | 실행 제출·상태·결과 | 객체 편집 중 임시 좌표 |
| WindowBridge | 분리 창·iframe 통신·단축키 relay | 업무 규칙의 중복 구현 |

기존 Context를 작은 hook/reducer/service로 나누는 방법을 우선 검토한다. 도입 도구보다 명확한 상태 전이가 중요하다.

#### 편집 상태 전이

권장 모델이며 현재 동작에 맞게 세분화한다.

```text
idle
  → target_selected
  → draft
  → proposal_loading
  → proposal_ready
  → committing
  → committed

실패: proposal_failed / conflict / commit_failed
취소: cancelled → idle 또는 기존 선택 유지
```

- booleans 여러 개로 서로 모순되는 상태를 만들지 않는다.
- proposal은 생성 당시 dataset/layer/frame/track/task/session 범위를 고정한다.
- frame/task가 바뀌면 이전 응답이 새 작업을 덮어쓰지 않게 한다.
- AbortController만으로 충분하다고 보지 않는다. 이미 완료된 응답·취소 불가 서버 작업에도 scope token/request identity 검사를 적용한다.
- commit 응답이 유실돼도 같은 idempotency key로 안전하게 확인·재시도한다. 화면에서 매 재시도마다 새 key를 생성해 중복 피처를 만들지 않는다.
- near-duplicate override와 사유·필수 속성 검증은 서버 확정 시에도 유지한다.
- 저장 실패 뒤 draft를 조용히 삭제하지 않는다. 재시도·취소 가능한 상태로 남긴다.

#### 화면 구성 방향

- 현재 작업 대상과 선택 레이어를 한곳에서 분명히 표시한다.
- 지도·파노라마·점군은 같은 선택을 보여 주되, 속성 확인만 했는데 강제로 프레임이 이동하지 않게 기존 옵션을 유지한다.
- 저장되지 않은 proposal과 저장된 feature는 형태·상태 문구로 구분한다. 색만으로 구분하지 않는다.
- 객체를 선택하면 품질·사유·관련 지주·수정 동작을 바로 확인할 수 있게 한다.
- 빈 데이터, loading, 오류, 읽기 전용, 미검증 기능 상태를 서로 다르게 표시한다.
- 모든 설정을 첫 화면에 늘어놓지 말고 기본 작업과 고급 설정을 구분한다.

#### 분리 창과 popup fallback

- `DetachablePanel`과 파노라마·점군 분리 기능은 유지한다.
- 속성표 popup 실패 시 도킹 패널 또는 같은 창 내 접근 가능한 대체 편집 경로를 제공한다.
- popup 생성은 사용자 gesture와 연결한다. 비동기 응답 후 자동 popup에 의존하지 않는다.
- 창 닫기·재부착 시 선택·draft·오류 상태가 유실되지 않게 한다.
- iframe/window 메시지는 origin, source window, message type, 필요한 scope를 검증한다. 무분별한 `'*'` 대상 통신을 새로 넣지 않는다.
- 단축키의 실제 실행 주체는 하나로 유지해 main/popup 중복 저장을 방지한다.
- 대화상자·텍스트 입력·버튼 focus와 modifier 처리를 기존 테스트와 실제 브라우저에서 검증한다.

#### CSS·접근성

전역 CSS는 한 번에 갈아엎지 않는다. 수정하는 기능의 스타일을 namespace/컴포넌트 경계로 이동하고 기존 화면에 영향을 주는 selector를 테스트한다. 키보드 focus, dialog 닫기/복귀, 오류 안내, loading 중 조작 가능 범위를 확인한다.

#### 필수 검증

빠른 dataset/frame 전환, 오래 걸린 proposal의 늦은 응답, commit 중 task 변경, 팝업 차단, 분리 창 닫기/복귀, main/popup 동시 단축키, 입력 필드에서 `B/N/M` 타이핑, 저장 응답 유실·재시도, revision 충돌, undo/redo, 선택한 표지와 지주의 동기화.

jsdom 테스트와 실제 브라우저 검증을 구분한다. WebGL·분리 창·외부 지도 SDK를 실제로 실행하지 않았다면 테스트 수만으로 완료 처리하지 않는다.

**완료 조건:** 기능을 제거하지 않고 상태 책임이 작아졌으며, 동일 편집 흐름이 main/popup/fallback에서 검증된다.

### P2-2. 기존 성능 개선 보존과 preview 경계 정리

**먼저 확인:** PR #2의 점 선택 가속과 JPEG decoder downsampling은 선행 스냅샷 main에 이미 포함돼 있다. 다시 동일 기능을 구현하지 않는다.

#### 1) 실제 적용 여부

P0-1의 빌드 버전과 실제 로드된 JS를 확인한다. 새 소스가 존재하는 것과 브라우저가 그 코드를 실행하는 것을 구분한다. 기존 PR의 합성 benchmark 수치를 새 운영 측정 결과로 복사하지 않는다.

#### 2) 명시적인 preview 생성 경계

- `install_panorama_fastpath()`와 `media._resize_panorama`의 모든 호출·테스트를 조사한다.
- 명시적 import, 생성기 인터페이스, dependency injection 중 현재 구조에 가장 작은 변경을 선택한다.
- 최적화된 구현은 유지하고 런타임 함수 치환에 의존하는 연결만 제거한다.
- circular import와 import 순서에 따라 최적화가 활성/비활성되는 문제를 테스트한다.
- 기존 test monkeypatch 지점이 바뀌면 의도와 계약을 보존하며 테스트를 이행한다. 테스트를 단순 삭제하지 않는다.
- JPEG draft, EXIF orientation, WebP/JPEG fallback, 원자적 쓰기, 기존 응답 media type·크기 계약을 보존한다.
- 이미지 orientation 변경이 projection/camera 축과 어긋나지 않게 실제 사용 경로를 검증한다.

#### 3) 점 선택 정확도

가속 경로와 기본 경로의 differential test를 보존·보강한다.

```text
큰/작은 점군
indexed/non-indexed geometry
지원하지 않는 geometry의 fallback
position attribute 갱신
회전·이동·스케일 transform
경계 ray·hit 없음·다중 후보·동일 거리
threshold와 draw range 등 실제 지원 계약
선택 point index와 결과 순서
cold 인덱스 생성 이후 warm 재사용
```

같은 클릭에서 다른 점이 선택되는데 속도가 빨라졌다는 이유로 통과시키지 않는다. 최초 인덱스 생성 비용과 반복 선택 비용을 분리해 측정한다. worker로 옮기는 최적화는 측정상 필요할 때만 추가하고, 버퍼 수명·복사 비용·취소·stale index를 함께 검증한다.

#### 4) preview/cache 정책

- 현재 점 예산·반경·focus/corridor·관련 지주 exact lookup의 의미를 유지한다.
- cache key에 실제 derivative 결과를 바꾸는 입력 fingerprint·budget·color/focus·생성기 버전을 포함하는지 확인한다.
- 생성 알고리즘 변경 후 기존 cache가 조용히 재사용되는지 테스트한다. 필요한 경우 cache version을 명시적으로 올린다.
- 동일 요청 합치기, 동시 생성 제한, 취소 후 실제 worker 종료까지 자원 유지, cache 용량 제한을 보존한다.
- preview cache 정리를 원본·학습용 crop·최종 산출물 삭제와 연결하지 않는다.
- global `cache` 폴더 전체를 무조건 지우는 해결책을 쓰지 않는다. 생성물임이 확인된 경로만 정책에 따라 정리한다.
- 대량 레이어나 프레임 이동 시 불필요한 전체 재조회·재렌더가 있는지 측정한다. 기존 revision별 cache와 exact identity 조회를 무효화하지 않는다.

#### 5) 측정 항목

| 항목 | 구분해야 할 조건 |
|---|---|
| 점 선택/hover | 최초 인덱스 생성, warm 선택, point 수, transform |
| 파노라마 첫 표시 | cache miss/hit, 원본 크기·format, preview 크기 |
| 프레임 이동 | 단독 창/분리 창, 점군+파노라마 동시 요청 |
| UI 응답성 | main-thread 긴 작업, JS heap, GPU resource 해제 |
| 서버 비용 | 메모리, 디코드·인코드 시간, 파일 I/O, semaphore 대기 |
| 장시간 사용 | 여러 frame 반복, 창 숨김/복귀, 메모리·listener 누수 |

고정 데이터·환경·조건에서 baseline/candidate를 반복 측정하고 p50/p95·표본 수·cache 상태를 남긴다. 허용 회귀 폭과 목표는 baseline의 변동성을 본 뒤 `VALIDATION.md`에 고정한다. 측정하지 않은 향상 배수나 “끊김 없음”을 주장하지 않는다.

**완료 조건:** 정확도 회귀가 없고 실제 변경 경로가 측정됐으며, API/cache/분리 창 계약이 유지된다.

---

### P2-3. 검수 workspace의 공개 준비

이 작업은 검수 기능을 무조건 켜는 작업이 아니다. 기존 비활성 이유를 확인하고 실제 운영자 흐름을 완결하는 작업이다.

#### 활성화 전 필수 조건

- session 생성·범위 고정·task 생성·claim·resolution이 실제 데이터에서 동작한다.
- 다른 task/frame으로 이동한 뒤 이전 proposal을 저장해 새 task가 완료되는 일이 없다.
- QA 미실행, stale QA, open error/task, outbox 미반영 등 완료 차단 사유를 서버가 판정한다.
- 화면의 작업 수만으로 완료 버튼을 활성화하지 않는다.
- task 상태와 geometry/provenance 저장 상태가 응답 유실·재시작 이후에도 일치한다.
- 권한·capability·배포 flag의 조합별 메뉴/API 동작이 일치한다.
- report/export는 생성 전후 revision 일관성을 확인하고 불완전 결과를 성공으로 내보내지 않는다.
- 활성화 대상 사용자에게 작업 안내와 롤백 경로가 있다.

활성화는 코드에 `true`를 하드코딩하는 방식보다 검증된 배포 설정으로 다룬다. 기본 동작은 기존과 호환되게 유지한다. 실제 MMS 검증이 없으면 `IMPLEMENTED_UNVERIFIED` 또는 `BLOCKED_VALIDATION`으로 남기고 운영 공개하지 않는다.

---

## 9. P3 — 운영 경계 보강, 필요한 경우에만 확장

### P3-1. 단일 작업 서버의 보안·사용자 식별 정리

#### 지금 보강할 수 있는 범위

1. Basic 인증·루프백 기본 바인딩·허용 저장소 루트·path containment·보안 헤더를 유지한다.
2. `--allow-remote-bind`가 접근 인증이나 TLS를 자동 설정하지 않는다는 안내를 명확히 한다.
3. 선택형 Basic 인증이 있는데 CLI 안내가 “built-in authentication 없음”으로 오해되게 쓰였는지 확인하고 실제 기능과 맞춘다.
4. API·파일 다운로드·SSE·정적 자원 중 인증 적용이 필요한 경로를 inventory로 만들고 누락 여부를 검사한다.
5. 외부 노출 시 필요한 TLS reverse proxy/접근 통제 설정을 문서화한다. 운영 방화벽·인증을 이 작업에서 임의 변경하지 않는다.
6. origin/host/CSRF·브라우저에 자동 전달되는 인증의 위험은 현재 endpoint 동작으로 확인한다. 확인하지 않은 취약점을 확정 사실로 보고하지 않는다.
7. 지도 브라우저 키와 서버 비밀정보를 구분한다. 브라우저용 키가 소스에 보인다는 사실만으로 서버 credential 유출이라고 단정하지 않는다. 운영 origin 제한·키 설정은 실제 제공자 계약에 맞춰 별도 검증한다.
8. 프런트 feature flag는 권한 검사를 대신하지 않는다.

#### 작업자 신원

- 현재 단일 운영 모드에서는 `operator-local` 의미를 유지하고 공유 식별자라는 점을 표시한다.
- 다중 작업자 ID를 도입할 때 request body가 보낸 임의 `operator_id`를 인증된 사용자로 신뢰하지 않는다.
- 신뢰되는 인증 경계에서 principal을 만들고 API 처리·provenance·audit에 연결한다.
- reverse proxy 사용자 헤더를 쓴다면 외부 사용자가 해당 헤더를 직접 위조할 수 없는 배치와 신뢰 경계를 검증한다.
- 기존 provenance를 임의로 새 사용자 ID에 귀속시키지 않는다. 기존 자료는 legacy/local로 보존한다.

**완료 조건:** 기존 방어 기능이 약화되지 않고 실제 인증 적용 범위와 사용자 식별의 한계가 정확히 드러난다. 새로운 외부 인증 서비스 도입은 별도 요구·권한이 있어야 한다.

---

### P3-2. 다중 작업자·저장소·배포 확장은 조건부

**기본 정책: 설계·요구 확인까지만 진행한다. 실제 필요와 별도 승인 없이 인프라를 확대하지 않는다.**

다음 질문에 답할 수 있을 때 구현을 결정한다.

- 동시 작업자·동시 편집·GPU 처리량의 실제 요구가 무엇인가?
- 현재 단일 worker/process-local fence가 병목인지 측정했는가?
- 데이터셋별 권한이 필요한가? 누가 조회·수정·검수 완료·삭제·내보내기를 할 수 있는가?
- API와 GPU worker를 별도 호스트로 운영해야 하는가?
- 데이터와 metadata 저장소의 백업·복구·운영 담당자는 누구인가?

#### 확장 시 보존할 것

- SQLite→다른 DB로 바꾸는 것만으로 동시성이 해결됐다고 보지 않는다.
- layer revision CAS, 멱등성, outbox, session fence, completion gate를 새 프로세스 경계에서 재검증한다.
- 분산 queue는 claim/lease/fencing/retry·artifact ownership을 함께 설계한다.
- 실제 채택 전까지 ASGI multiworker나 복수 RunManager를 같은 state-dir에 허용하지 않는다.
- Docker 도입이 필요하면 먼저 현재 코드·현재 설정의 동일 결과를 검증한다. 컨테이너화와 알고리즘 변경을 섞지 않는다.
- Kubernetes·자동 확장은 소규모 내부 작업 서버의 기본 필수 조건으로 넣지 않는다.

요구가 없으면 `DEFERRED_WITH_REASON`으로 남기는 것이 올바른 완료다. 불필요한 외부 서비스 생성은 하지 않는다.

---

## 10. API·schema·의존성 관리 규칙

### 10.1 API 계약

- 실제 FastAPI/Pydantic 응답과 `webui/src/types.ts`, `webui/API_CONTRACT.md`를 대조한다.
- 기존 endpoint 경로·필드 의미를 유지한다. 새 필드는 가능하면 additive로 도입하고 구형 응답을 처리하는 UI fallback을 둔다.
- 서버의 dict 응답이 OpenAPI에 완전하게 표현되지 않으면, 타입 생성기를 붙였다는 이유만으로 계약이 보장된다고 하지 않는다.
- OpenAPI 타입 생성은 schema coverage와 런타임 검증이 충분할 때 도입한다. 생성 타입과 수동 업무 모델을 구분한다.
- 알 수 없는 상태·버전은 중립적 표시 또는 fail-closed로 다룬다. 임의 성공 상태로 매핑하지 않는다.
- 사용자에게 보이는 진행률과 durable 상태가 다른 경우 둘의 차이를 표현한다.

### 10.2 DB·설정 migration

- schema version을 명확히 하고 migration을 반복 실행해도 기존 데이터가 손상되지 않게 한다.
- 기존 DB·설정·localStorage의 fixture를 만들어 이전 버전에서의 이행을 테스트한다.
- rollback은 코드만 되돌리면 되는지, DB/설정 변환도 필요한지 작업마다 정의한다.
- 역호환되지 않는 migration은 운영 적용 전에 검증된 백업과 복원 경로가 있어야 한다.
- 새 컬럼/테이블 도입과 기존 데이터 삭제를 같은 변경으로 묶지 않는다.

### 10.3 의존성

- Python은 기존 고정 직접 의존성을 존중한다. frontend lockfile과 기존 설치 경로를 유지한다.
- 신규 도구는 목적·대체 가능성·runtime/dev 구분을 기록한다.
- 현재 사용 버전이 오래돼 보인다는 이유만으로 업그레이드하지 않는다. 호환 문제나 검증된 보안 수정이 필요하면 별도 변경으로 다룬다.
- 모델·GPU toolkit·지도 SDK·Node/Python 버전을 한 PR에서 동시에 바꾸지 않는다.
- 지원 플랫폼의 실제 설치 가능 여부와 결과를 기록한다. 다른 환경의 성공을 현재 환경 성공으로 복사하지 않는다.

---

## 11. 검증 시나리오와 수용 기준

아래 ID를 `VALIDATION.md`의 테스트/수동 검증 증거와 연결한다. 기존 테스트가 커버하면 중복 구현 대신 연결을 기록한다.

| ID | 시나리오 | 통과 기준 | 필요한 환경 |
|---|---|---|---|
| T01 | source만 변경하고 기존 dist 실행 | stale build 식별, 새 버전으로 오인하지 않음 | 로컬 build |
| T02 | npm 없음 + 검증된/오래된 패키지 | 정합성 정책에 따라 분기 | 설치 시뮬레이션 |
| T03 | test/type/build 의도적 실패 | CI/명령 exit code가 실패로 전파 | 로컬+원격 CI |
| T04 | baseline vs candidate 정상 fixture | 정책별 수치·속성·관계 동등 | 합성/실제 구분 |
| T05 | missing/extra/ambiguous feature | 비교기가 실패를 숨기지 않음 | 비교기 fixture |
| T06 | calibration 누락/모호성/CRS 오류 | 모델 실행 전 실패, 저장 가능한 가짜 좌표 없음 | 합성+대표 MMS |
| T07 | 지주 하단 관측 부족·잡음·경사 | 기존 바닥점/검토/실패 정책 유지 | 실제 MMS 필요 |
| T08 | 다중 frame 동일 표지·지주 | 기존 dedupe/support 관계 유지 | 실제 MMS 필요 |
| T09 | spawn 전후 부모 종료 | 무증거 재실행 없음, 상태 복구 정확 | 격리 subprocess |
| T10 | child 생존·PID 재사용·identity 불명 | 다른 프로세스 종료 금지, 중복 lane start 금지 | 대상 OS |
| T11 | 취소와 성공 commit 경합 | 유효 durable success 보존, 불완전 성공 없음 | 실행기 테스트 |
| T12 | API 취소 중 점군 thread 실행 | 완료 전 reader/semaphore 조기 해제 없음 | 동시성 테스트 |
| T13 | 같은 mutation key 재전송 | 피처·revision 중복 증가 없음 | API+SQLite |
| T14 | 다른 사용/창의 revision 수정 | 충돌 검출, 최신 geometry 덮어쓰기 없음 | API+브라우저 |
| T15 | feature commit 후 registry 반영 실패 | outbox 복구, 잘못된 완료/삭제 차단 | 장애 주입 |
| T16 | QA 뒤 레이어 수정 | stale QA로 완료 차단 | API+UI |
| T17 | 과거 CLI 출력 루트 재사용 | 선언된 보존 정책 준수, 과거 bytes 오인 없음 | 별도 출력 루트 |
| T18 | 디스크 부족·부분 SHP·replace 실패 | 이전 최종본 유지, false success 없음 | fault injection |
| T19 | 다중 DB·outbox 백업/복원 | revision·provenance·task·artifact 정합 | 테스트 복제본 |
| T20 | 빠른 dataset/frame/task 전환 | 이전 응답이 새 문맥을 덮어쓰지 않음 | UI+브라우저 |
| T21 | 팝업 차단·닫기·재부착 | 핵심 작업 fallback 및 상태 보존 | 실제 브라우저 |
| T22 | main/popup 단축키·입력 focus | 중복 저장/오발동 없음 | 실제 브라우저 |
| T23 | raycast 가속 vs 기본 | 동일 선택 계약, 갱신/fallback/경계 처리 | TS differential |
| T24 | panorama orientation/cache 변경 | geometry 정합·캐시 무효화·미디어 계약 유지 | 이미지 fixture+MMS |
| T25 | 반복 탐색·창 숨김/복귀 | 측정된 성능 기준과 자원 수명 준수 | 실제 브라우저 |
| T26 | 인증 누락·잘못된 경로·symbolic link | 기존 접근/경로 방어 유지 | 보안 테스트 |
| T27 | legacy DB/API/config/localStorage | 정의한 호환/이행 정책 준수 | 이전 버전 fixture |
| T28 | 실패/중단 run의 결과 접근 | 과거 산출물을 현재 성공 결과로 노출하지 않음 | API+artifact |

### 검증 수준의 표시

```text
STATIC_REVIEWED          코드/설정만 검토
UNIT_CONTRACT_PASSED     단위·계약 테스트 통과
INTEGRATION_PASSED       지정 통합 환경에서 통과
REAL_MMS_PASSED          실제 MMS 결과 검증 통과
BROWSER_PASSED           실제 브라우저 작업 흐름 통과
OPERATION_APPROVED      운영 적용 승인 확보
```

이들은 일렬로 대체되는 하나의 수치가 아니다. build 수정은 실제 GPU 테스트가 필요 없을 수 있고, 계산 수정은 `UNIT_CONTRACT_PASSED`만으로 충분하지 않다. 작업별 필요한 검증 수준을 선언한다.

---

## 12. R0 — 릴리스와 롤백

### 12.1 변경 유형별 릴리스 gate

| 변경 유형 | 릴리스 전 필수 증거 |
|---|---|
| 문서·오류 메시지 | 실제 코드와의 일치, 해당 계약/스냅샷 테스트 |
| 빌드·패키징 | source/build 정합성, 설치 smoke, 기본 API/UI 실행 |
| 실행 lifecycle | 대상 OS subprocess/cancel/restart 검증, 현재 결과 공개 계약 |
| 계산 호출·단계·checkpoint | 실제 MMS baseline 비교, 모델/설정/CRS 동등성 |
| UI 상태·분리 창 | 관련 단위 테스트 + 실제 브라우저 작업 흐름 |
| DB/보존/백업 | 이전 DB migration, 복원 rehearsal, 데이터 정합성 |
| 인증·다중 사용자 | 서버 권한 테스트, 사용자 식별·provenance 검증, 승인 |

**실제 운영 서버 적용은 사용자 승인 없이 수행하지 않는다.** 개발 PR의 코드 완료와 운영 적용 완료를 구분한다.

### 12.2 권장 전환 순서

1. 읽기 가능한 baseline 기록을 남긴다. 사용자 승인 없는 원격 tag 생성은 하지 않아도 된다.
2. 작은 변경을 별도 브랜치/PR로 검증한다.
3. 운영 복제 환경에서 같은 데이터·모델·설정으로 새 경로를 비교한다.
4. 필요한 새 경로는 default-off 또는 제한 범위의 opt-in으로 검증한다. feature flag로 실패를 숨기지 않는다.
5. frontend/backend가 동일 릴리스인지 확인한다.
6. 데이터/설정/DB rollback 준비와 운영 적용 승인을 확인한다.
7. 적용 후 smoke와 결과 정합성을 확인한다. 승인 범위 밖이면 적용 절차와 준비 상태만 제공한다.

### 12.3 롤백 원칙

- 사용자 작업을 지우는 `git reset --hard`, DB 삭제, 전체 cache 삭제를 기본 rollback으로 쓰지 않는다.
- 코드 rollback은 선택한 변경 단위의 revert 또는 이전 검증된 release 전환으로 한다.
- DB schema가 바뀌었으면 이전 binary가 안전하게 읽을 수 있는지 먼저 검증한다. 모르면 코드만 되돌리지 않는다.
- 운영 중 새 편집 데이터가 생겼다면 과거 DB snapshot 복원으로 그 편집을 잃지 않도록 별도 이행 계획이 필요하다.
- 새 결과와 기존 결과를 함께 보존해 어떤 조건에서 생성됐는지 확인할 수 있게 한다.
- rollback 성공도 같은 smoke·산출물 검증으로 확인한다.

### 12.4 PR 구성 예시

```text
PR-1: build provenance + stale-dist 검사 + 설치 경로 테스트
PR-2: CI 명령/워크플로 + baseline/회귀 harness
PR-3: 오류·기능 공개 상태·운영 체크리스트 정합성
PR-4: JobExecutor/child ownership의 작은 추출과 lifecycle 테스트
PR-5: 첫 처리 책임 추출 + golden 동등성 증거
PR-6: 산출물 보존·백업/복원
PR-7: UI state/분리 창 fallback
PR-8: preview 명시 호출·성능 검증
```

실제 변경이 겹치면 조정한다. 무조건 이 개수의 PR을 만들지 않는다. 코드 이동 PR과 알고리즘 변경 PR은 분리한다. PR의 존재가 병합 승인을 의미하지 않으며 `main` 자동 병합은 금지한다.

---

## 13. AI-agent 실행 규율과 체크포인트

### 13.1 한 작업 단위의 실행 루프

```text
현재 상태/계약 확인
→ 구체적인 실패·검증 기준 하나 선택
→ 재현/보호 테스트 추가
→ 최소 범위 구현
→ 관련 테스트 실행
→ 넓은 회귀·build 실행
→ diff·민감정보·사용자 변경 검토
→ 선택적 commit
→ CHECKPOINT 갱신
→ 다음 의존성 충족 작업
```

- 한 변경에서 동시에 여러 architecture 축을 바꾸지 않는다.
- 큰 파일을 읽을 때 필요한 함수·호출자·테스트부터 읽는다. 전체 소스를 매번 프롬프트에 복사하지 않는다.
- stage별 token budget이 부족하면 완료 상태와 다음 명령을 저장하고 안전하게 종료한다.
- tool 오류는 호출 schema부터 확인한다. 예를 들어 도구가 `requires_approval`을 요구하면 정확한 타입·필드를 제공한다. 실제 위험 작업에 일괄 `false`를 넣거나 전역 승인 해제로 우회하지 않는다.
- 잘못된 tool schema, 실행 환경 문제, 테스트 실패를 구분한다. 같은 실패 명령을 설명 없이 반복하지 않는다.
- 새 라이브러리 도입·API 변경·DB migration은 결정 이유와 버린 대안을 짧게 남긴다.
- 범위 내 문제는 직접 수정하되 무관한 기존 버그까지 확대하지 않는다. 별도 backlog로 분리한다.

### 13.2 작업 상태

| 상태 | 의미 |
|---|---|
| `TODO` | 아직 착수하지 않음 |
| `IN_PROGRESS` | 조사/구현 중 |
| `IMPLEMENTED_UNVERIFIED` | 코드 작성, 필요한 검증 일부 미완료 |
| `PASSED` | 해당 작업에 선언된 검증 gate 통과 |
| `BLOCKED_VALIDATION` | 데이터/GPU/브라우저 등 검증 환경 부재 |
| `BLOCKED_PERMISSION` | 운영 변경·게시 등 권한 부재 |
| `ALREADY_FIXED` | 현재 기준에서 이미 해결됨을 근거로 확인 |
| `DEFERRED_WITH_REASON` | 요구·선행 조건 부재로 의도적 보류 |

`PASSED`라고 적을 때 어떤 검증 gate를 통과했는지 함께 적는다. `IMPLEMENTED_UNVERIFIED`를 일반 완료율에 섞어 “100% 완료”로 표현하지 않는다.

### 13.3 체크포인트 최소 양식

`docs/refactor/CHECKPOINT.md`에 다음을 실제 값으로 남긴다.

```markdown
# RoadInventory-MMS 개선 체크포인트

- 기록 시각/시간대:
- 작업 브랜치/HEAD:
- 기준 SHA:
- 현재 작업 ID:
- working tree 상태:
- 사용자 변경/수정 금지 파일:

## 완료 및 근거
| 작업 ID | 상태 | 변경 요약 | 검증 명령/결과 | commit |
|---|---|---|---|---|

## 미검증·차단
| 작업 ID | 부족한 조건 | 이미 한 검증 | 활성화 금지 범위 | 해결 후 다음 단계 |
|---|---|---|---|---|

## 다음 작업
- 다음에 읽을 함수/파일:
- 재현/검증할 시나리오:
- 수정할 책임 경계:
- 실행할 정확한 명령:
- 예상되는 성공 조건:

## 운영/원격 변경
- push/PR 여부:
- main 병합 여부: 하지 않음
- 운영 데이터/서비스 변경 여부:
- rollback 방법 및 남은 조건:
```

다음 에이전트가 전체 대화를 읽지 않고도 재개할 수 있어야 한다. 원본 경로·비밀정보 대신 안전한 별칭을 쓴다. 아직 결정하지 않은 명령을 실제 실행한 것처럼 적지 않는다.

### 13.4 최종 보고 형식

최종 응답에는 다음 순서로 실제 결과를 제공한다.

1. **현재 판정:** 구현 완료/부분 완료/검증 차단 중 무엇인지.
2. **변경 사항:** 사용자에게 달라지는 부분과 유지한 부분.
3. **검증:** 명령·환경·pass/fail/skip·실제 MMS·실제 브라우저 여부.
4. **미완료:** 남은 작업 ID, 이유, 위험, 활성화 여부.
5. **형상:** 브랜치·commit·PR·dirty 상태, main 미병합.
6. **재개 지점:** CHECKPOINT 경로와 다음 하나의 구체적인 작업.

“전체 구조 개선 완료” 같은 포괄적인 표현은 모든 필수 gate가 충족됐을 때만 쓴다. 안전한 일부 작업이 완료됐다면 그 범위를 정확히 보고한다.

---

## 14. 에이전트용 시작 프롬프트

아래를 이 문서와 함께 실제 개발 환경의 AI-agent에 전달한다.

```text
첨부한 ROADINVENTORY_MMS_INCREMENTAL_REFACTOR_AI_AGENT_SPEC_KO.md를 실행 명세로 사용해
현재 dbparkJ/RoadInventory-MMS 저장소를 개선해줘.

먼저 AGENTS.md, 현재 원격/로컬 HEAD, 사용자 미커밋 변경, 실제 실행 환경을 확인하고
A0 기준선과 보호 계약을 기록해. 문서의 aac6a30은 과거 검토 기준이므로 최신이라고 가정하지 마.
이미 해결된 문제는 중복 구현하지 마.

새 저장소나 전면 재작성 없이 별도 기능 브랜치에서 P0부터 구현해.
조사 문서만 만들고 끝내지 말고, 선행 조건이 충족된 작은 작업을 실제 수정·테스트·commit해.
계산 알고리즘·좌표·SHP/API 계약, 원본 MMS, 편집 이력, 파노라마/점군 분리 창은 보존해.

구조 변경과 알고리즘 개선은 섞지 마. 실제 MMS/GPU/브라우저 검증이 없으면
그 항목을 BLOCKED_VALIDATION으로 기록하고 검증이 필요한 새 경로를 운영 활성화하지 마.
대신 독립적으로 진행할 수 있는 안전한 작업은 계속해.

main 직접 수정·자동 병합·force push·브랜치 삭제·운영 DB/서비스 변경은 하지 마.
push/PR도 현재 사용자 지시와 권한이 허용한 범위에서만 진행해.
각 작업마다 실제 테스트 결과와 docs/refactor/CHECKPOINT.md를 갱신하고,
끝날 때 완료/미검증/차단, 브랜치·commit, 다음 구체적인 작업을 구분해서 보고해.
```

---

## 부록 A. 선행 검토 근거

다음은 선행 검토의 근거를 다시 찾기 위한 링크다. 고정 SHA 링크는 당시 코드를 가리킨다. PR/Actions 페이지는 상태가 바뀔 수 있으므로 A0에서 실행 시점의 상태를 다시 읽는다. 아래 링크를 열었다는 사실과 실제 테스트를 실행했다는 사실을 구분한다.

| 번호 | 근거 |
|---|---|
| S01 | [기준 커밋 aac6a30](https://github.com/dbparkJ/RoadInventory-MMS/commit/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf) |
| S02 | [성능 개선 PR #2](https://github.com/dbparkJ/RoadInventory-MMS/pull/2) |
| S03 | [기준 SHA의 dist](https://github.com/dbparkJ/RoadInventory-MMS/tree/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/webui/dist) · [해당 경로 이력](https://github.com/dbparkJ/RoadInventory-MMS/commits/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/webui/dist) |
| S04 | [webapp/app.py](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/mms_shp_detection/webapp/app.py) |
| S05 | [setup_web.sh](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/scripts/setup_web.sh) |
| S06 | [pipeline.py](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/mms_shp_detection/pipeline.py) |
| S07 | [overlays.py](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/mms_shp_detection/webapp/overlays.py) |
| S08 | [store.py](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/mms_shp_detection/webapp/store.py) |
| S09 | [OverlayContext.tsx](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/webui/src/components/OverlayContext.tsx) |
| S10 | [styles.css](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/webui/src/styles.css) |
| S11 | [GitHub Actions — 실행 시 재확인](https://github.com/dbparkJ/RoadInventory-MMS/actions) |
| S12 | [현재 아키텍처·테스트 공백·운영 한계](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/docs/current_architecture.md) |
| S13 | [App.tsx — 검수 UI flag·popup·상태](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/webui/src/App.tsx) |
| S14 | [운영자 MMS 스모크 체크리스트](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/docs/OPERATOR_MMS_SMOKE_CHECKLIST.md) |
| S15 | [pipeline_service.py — 실행 모델·오류 변환](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/mms_shp_detection/app/pipeline_service.py) |
| S16 | [run_web.py — 바인딩·선택형 인증](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/scripts/run_web.py) |
| S17 | [panorama_fastpath.py — 최적화·함수 치환](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/mms_shp_detection/webapp/panorama_fastpath.py) |
| S18 | [requirements.txt](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/requirements.txt) · [webui/package.json](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/webui/package.json) |
| S19 | [WEB_UI_ARCHITECTURE.md](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/docs/WEB_UI_ARCHITECTURE.md) |
| S20 | [ARCHITECTURE_IMPROVEMENT_REPORT.md](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/docs/ARCHITECTURE_IMPROVEMENT_REPORT.md) |
| S21 | [지주 정확도 회귀 테스트](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/tests/test_pole_accuracy_regressions.py) |
| S22 | [security.py — 경로·파일 쓰기 helper](https://github.com/dbparkJ/RoadInventory-MMS/blob/aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf/mms_shp_detection/webapp/security.py) |

---

## 부록 B. 마지막 자체 점검

작업 완료를 보고하기 전에 에이전트는 다음에 답한다.

- 실제 현재 HEAD를 확인했는가, 과거 보고서를 최신 상태로 오인하지 않았는가?
- 사용자의 미커밋 변경·원본 데이터·운영 서비스를 보호했는가?
- 수정한 책임을 테스트로 보호했고, 관련 없는 알고리즘/의존성 변경을 섞지 않았는가?
- 소스와 실제 제공하는 프런트 build의 일치를 검증했는가?
- 실제 MMS·GPU·브라우저 검증 여부를 각각 정확히 표시했는가?
- feature flag, skip, mock, 넓힌 허용오차로 실패를 숨기지 않았는가?
- 좌표·SHP·provenance·revision·outbox 계약을 유지했는가?
- 기존 분리 창·단축키를 제거하거나 사용 불가능하게 만들지 않았는가?
- 모든 성공 주장을 실행 로그/검증 결과/commit과 연결할 수 있는가?
- 남은 차단과 다음 재개 작업을 다른 에이전트가 바로 이해할 수 있는가?
- main 병합·운영 반영을 별도 승인 없이 하지 않았는가?

**최종 원칙: 겉으로 새로워진 코드보다, 기존 결과를 보존하면서 변경 이유·검증 근거·복구 방법이 명확한 코드를 만든다.**
