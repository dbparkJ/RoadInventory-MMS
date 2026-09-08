# RoadInventory-MMS 개선 체크포인트

## 최신 후속 작업: 3D 점군 표시 성능

- 기록: 2026-09-08, Asia/Seoul. 사용자 보고에 따라 3D 점군 지연을 재현하고 수정했다.
- 현재 branch: `refactor/pointcloud-loading-performance`; source `a4191be`, 검증된 build commit `63dcb96`. 이후 문서 commit은 `git log -5 --oneline`으로 확인한다.
- 원인: 사전 통합의 catalog v5→v6로 첫 재인덱싱 50.6초, 표시에도 전체 원본 속성 decode, 반복 마스크 계산, body 취소 누락, 41.2초 인덱싱 재시도 한계.
- 수정: 공유 LRU 내 4필드 preview, 요청당 32MiB 이내 indices 재사용, body/재시도 취소와 총 120초·2초 간격 인덱싱 대기. 좌표·색·표본 순서·예산·원본 record 보존 계약은 유지했다.
- 실제 100만점 프레임 4회 교차 측정: 첫 생성 중앙값 1474.65→988.54ms, 재생성 691.69→508.18ms, decoded cache 277.27→107.44MiB. 모든 출력 bytes 동일. 실제 전체 MMS/SHP golden 및 browser 검증은 아니다.
- 로컬 Python 624 passed / 7 OS·권한 skips, frontend 412 passed / 41 files, production build/Gitless package/HTTP/Git bytes 검증 통과. 원격 CI 34181671986의 4개 job 모두 success: 양 OS Python 각각628 passed/3 skips, frontend 각각412 passed. 상세/원시 측정은 [POINT_PREVIEW_PERFORMANCE.md](POINT_PREVIEW_PERFORMANCE.md)에 기록했다.
- [draft PR #9](https://github.com/dbparkJ/RoadInventory-MMS/pull/9), base는 이전 통합 branch. main 병합·원본/운영 DB/기존 cache 삭제·운영 서버 재시작 없음.
- 다음 한 작업: 아래 서버 명령과 브라우저 강력 새로고침으로 새 빌드를 적용한 뒤 동일 프레임 첫 진입/이동/재방문의 실제 화면 시간을 확인한다. 브라우저 연결이 없으면 측정 완료로 표시하지 않는다.

```powershell
cd D:\mms_project
.\.venv\Scripts\python.exe .\scripts\run_web.py --host 127.0.0.1 --port 8000 --build-mode production
```

## 이전 통합 단계 기록

- 기록: 2026-09-08 11:23, Asia/Seoul.
- 작업 브랜치: `refactor/roadinventory-incremental`.
- 통합 build source: `c07e4be`, 검증된 artifact HEAD: `e28c39c` (이후 문서 전용 commit은 `git log -5 --oneline`으로 확인).
- 기준: `main@d3d7d9af07518282afcb332df100724c1b04de9d`.
- 판정: **부분 구현 및 자동 검증 완료, 실제 MMS/브라우저·GPU 장애/성능 gate 미완료**. 전체 구조 개선 완료가 아니다.
- 현재: 독립 변경과 배포 파일 통합 완료. Gitless package·HTTP·Git bytes 및 원격 4-job CI 검증 통과. 최종 근거는 VALIDATION의 통합 검증 절에 기록했다.
- working tree: 구현·개별 branch·통합 build는 commit/push 완료. 이 기록만 문서 전용 commit으로 마감하며 최종 clean/push 상태를 확인한다.
- 사용자 변경: 최초 명세는 사전 통합으로 보존. 원본 data/models/운영 상태와 기존 사용자 별도 worktree는 수정하지 않는다.

## 완료 및 근거

| 작업 | 변경과 검증 범위 | branch head / PR |
|---|---|---|
| 사전 정리 | 기존 브랜치 통합 후 GitHub/local main 동기화, 기존 브랜치 보존 | `d3d7d9a`, [merged #3](https://github.com/dbparkJ/RoadInventory-MMS/pull/3) |
| A0/P0 | source/UI/backend provenance, production gate, Node-free 배포, 4-job CI, 합성 비교, 오류 경계 | `bc5cef6`, [draft #4](https://github.com/dbparkJ/RoadInventory-MMS/pull/4) |
| P1-1 | 영속 intent·실제 OS identity, private 실행 승인, Windows Job, 재시작·취소·PID 재사용 보호, 대기 사유 안내 | `5631241`, [draft #7](https://github.com/dbparkJ/RoadInventory-MMS/pull/7) |
| P1-3 | offline evidence, 과거 성공 산출물 bytes 보관, SQLite backup/관계 검사, 별도 경로 restore, 고유 임시 파일·원래 오류 보존 | `9af0ca0`, [draft #8](https://github.com/dbparkJ/RoadInventory-MMS/pull/8) |
| P2-2 | 동일 최적화 본문을 명시 import로 연결, codec/EXIF/캐시/import 계약 및 hash·시간 측정 | `75210ce`, [draft #6](https://github.com/dbparkJ/RoadInventory-MMS/pull/6) |
| P3-1 | 등록 경로 auth, 교차 Origin 쓰기 거부, 인증된 media/static private cache, Vite Host 보존 | `fc40b89`, [draft #5](https://github.com/dbparkJ/RoadInventory-MMS/pull/5) |

통합 Python **615 passed / 7 skipped**, 최종 frontend **400 passed / 39 files**. 실제 소유 subprocess, 합성 PointZ/한글 DBF/관계/outbox 복원, auth 경계, 실제 Vite→backend HTTP도 검증했다. [VALIDATION.md](VALIDATION.md)에 명령·로그·원격 결과를 구분한다. 로컬 skip은 Windows symlink 권한 4개와 POSIX launcher 3개다. hosted Linux/Windows가 해당 OS 경로를 검증한다.

통합 [CI 34179470399](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34179470399)는 전체 success다. Python 양 OS 각각 **619 passed / 3 skipped**, frontend 양 OS 각각 **400 passed**, tsc/build/실패 전파/package 모두 통과했다. 확인된 code/artifact HEAD는 `e28c39c`이며 이후 문서 commit은 source fingerprint를 바꾸지 않는다.

각 PR은 P0 기반의 독립 변경이다. 통합에서는 package 문서 allowlist를 합쳤으며 build metadata 충돌은 통합 재빌드로 해결했다. build ID는 `dbff836f69bf4365a7bc6d04fb1a1652`다. 수치 계산·모델·추론 설정·SHP 계약과 review flag는 유지한다.

## 미검증·차단

| 작업 | 부족한 조건 | 검증된 범위 / 재개 |
|---|---|---|
| P0-3b/P1-2/P1-4 | 사용자가 검토된 실제 golden 및 XY·Z tolerance 없음 확인 | 합성 비교 20개·CUDA smoke는 실제 동등성 아님. fixture/profile 확정 후 --require-real; 계산 경로 전환/retry 금지 |
| P0-1/P2-1 | Browser 연결 discovery 재확인 `[]` | HTTP/jsdom은 실제 WebGL·popup·단축키 증거가 아님. 연결 후 아래 첫 작업 실행 |
| P1-1 | 실제 GPU/native-hang 복구 및 브라우저 안내 검증 없음 | Windows/Linux CPU subprocess·큐 차단 검증. 불명확한 owner 차단 자동 해제 금지 |
| P2-2 | 실제 영상/점 선택/browser·안정된 성능 환경 | 992회 source/output bytes 동일. 후보 A 진단 상한 6/16 초과, B 0/16. 성능 통과 선언하지 않음 |
| P2-3 | 실제 데이터/UI gate 없음 | REVIEW_WORKSPACE_UI_ENABLED=false 유지 |
| P3-1/R0 | 실제 proxy/TLS/browser/운영 적용 gate 없음 | 서버 계약·개발 proxy HTTP만 통과. 운영 서비스/인증/방화벽/DB 전환 미실행 |
| P3-2 | 다중 작업자 요구·검증·승인 없음 | 의도적 보류, ASGI 단일 worker 유지 |

## 다음 하나의 작업

**연결된 실제 브라우저에서 P0-1 로드 버전 및 P2-1 편집 workflow 기준선을 검증한다.** `webui/src/components/DetachablePanel.tsx`, `OverlayContext.tsx`, `ManualObjectContext.tsx`와 `docs/OPERATOR_MMS_SMOKE_CHECKLIST.md`의 활성 기능 범위를 먼저 확인한다.

```powershell
git status --short --branch
git fetch origin
.venv/Scripts/python.exe scripts/build_web.py verify
npm --prefix webui test -- --maxWorkers=2
```

브라우저 연결 후 별도 검증 storage/state와 `--no-run-worker --build-mode production`으로 서버를 연다. 운영 root를 사용하지 않는다. 실제 API/build ID·로드 asset, popup 차단/닫기/복귀, 빠른 frame 전환과 늦은 proposal, 입력 중 B/N/M, 저장 응답 유실·충돌·undo/redo를 확인한다. 소유권 안내는 합성 blocked run에서 확인한다. 생성한 프로세스의 OS 소유권을 확보하고 종료/임시 경로 정리까지 검증한다. 성공 조건은 main/popup/fallback의 선택·draft·오류 보존이며 실제 화면을 열지 못하면 BLOCKED_VALIDATION을 유지한다.

## 원격/운영과 롤백

PR #4~#8은 draft이며 새 변경을 main에 병합하지 않았다. 사전 PR #3만 사용자 명시 요청으로 병합했다. 통합 branch는 별도 push checkpoint로 보존하며 거대한 통합 PR은 만들지 않는다. force push/브랜치 삭제/운영 서비스 재시작/원본 변경 없음.

P1-1은 registry에 additive `ownership_json`/`ownership_blocked`를 추가한다. 과거 코드로 되돌리려면 모든 실행 종료를 확인한 maintenance 상태에서 검증된 이전 release를 사용한다. 새 컬럼/보존본 삭제나 DB 초기화로 되돌리지 않는다. 실제 운영 보존본 생성·live restore는 수행하지 않았다. 개별 변경은 해당 commit revert 후 같은 source/UI 재빌드로 되돌릴 수 있다.
