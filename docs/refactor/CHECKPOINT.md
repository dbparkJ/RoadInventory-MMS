# RoadInventory-MMS 개선 체크포인트

- 기록: 2026-09-08 09:35, Asia/Seoul.
- 브랜치: `refactor/roadinventory-p0-reliability`.
- 통합 기준 SHA: `d3d7d9af07518282afcb332df100724c1b04de9d`.
- 구현/테스트 HEAD: `392c3438898d102e5b5d66b98458d0ca79ab119b`, 배포파일 commit `406862c` (이 기록은 이후 문서 전용 커밋).
- 현재: A0 및 P0 코드/패키지 구현 완료, 실제 MMS/브라우저 gate 차단. 전체 구조 개선 완료가 아님.
- 작업 트리: 코드·build는 커밋/푸시됨. 이 체크포인트와 연결된 상태 문서만 갱신 중이며 종료 시 선택 커밋/푸시 후 clean 여부를 확인한다.
- 사용자 변경: 최초 명세 문서는 사전 통합에서 커밋 완료. 원본 data/models/운영 상태 및 별도 worktree는 수정 금지.

## 완료 및 근거

| 작업 | 상태/증거 | commit |
|---|---|---|
| 사전 정리 | [PR #3](https://github.com/dbparkJ/RoadInventory-MMS/pull/3) main 병합 및 로컬 main 동기화; 기존 브랜치 보존 | d3d7d9a |
| A0 | 기준선/보호 계약/테스트 실패 구분 | 4434120 |
| P0-1 | build source/backend/asset 검증, 개발 경고·운영 gate, Node-free 설치, Gitless package/API smoke 통과; 실제 browser 미검증 | 9c9b4c4, 406862c |
| P0-2 | PASSED: Windows/Linux CPU·frontend·build·probes·package 4 job 모두 success | 4093ea0, b1eaa64, 392c343 |
| P0-3a | 합성 frame/PointZ/DBF/CRS/관계 비교·false-pass 방어 20개 통과 | 371776b |
| P0-4 | 명시 input/output 오류, 원래 예외 보존, 현재/비활성 기능 체크리스트 분리 | 4529c29 |

로컬 전체 Python **560 passed / 7 skipped**, frontend **398 passed / 39 files**. build·source/asset verify·Gitless archive·실제 HTTP production smoke 통과. skip/초기 실패/원격 상태는 [VALIDATION.md](VALIDATION.md)에 명령·로그와 함께 기록했다. 기능 검수 flag, 모델/추론 설정, 계산 알고리즘은 유지했다.

최종 코드의 [GitHub CI 34173471638](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34173471638)은 전체 success. Python Ubuntu **565 passed / 2 skipped**, Windows **564 passed / 3 skipped**, frontend 양쪽 **398 passed**다. 모든 code/build/test 변경을 검증했으며 이 체크포인트 이후 문서 전용 커밋은 runtime source fingerprint를 바꾸지 않는다.

검증된 로컬 archive는 `.cache/release/roadinventory-mms-p0-371776b.zip`이다. 이 파일은 Git에 넣지 않았으며 원본 데이터/모델/운영 DB가 포함되지 않는다. 실제 Git archive `406862c`의 source/assets도 verified다.

## 차단/다음 작업

| 작업 | 부족한 조건 | 이미 한 검증 | 활성화 금지/재개 |
|---|---|---|---|
| P0-3b / P1-2 | 사용자가 검토된 실제 golden/XY·Z tolerance 없음 확인 | GPU smoke 및 합성 비교기 테스트 | 실제 동등성 주장/계산 경로 전환 금지. 대표 fixture와 profile 확정 후 --require-real |
| P0-1 browser / P2 UI | Browser runtime에서 연결 browser 없음, discovery=[] | 실제 HTTP index/assets/build metadata, jsdom | 실제 화면·popup workflow signoff 아님. 연결 browser에서 체크리스트 실행 |
| P2-3 | 실제 데이터·browser UI gate 미충족 | 비활성 UI/서버 계약 테스트 유지 | REVIEW_WORKSPACE_UI_ENABLED=false 유지 |
| R0 운영 | 운영 적용 승인·실제 기능별 gate 없음 | 별도 package/테스트 상태 디렉터리 smoke | 운영 service/DB/release 전환 실행하지 않음 |

## 다음 하나의 개발 작업

P1-1의 첫 변경 단위: `webapp/runs.py`의 `RunManager._execute`, `recover_after_restart`, `_terminate`와 `tests/test_webapp_run_safety.py`의 restart/cancel 계약을 읽고, **재시작 후 살아 있는 child와 PID 재사용을 구별하는 소유권 증거**의 재현 테스트부터 작성한다. 아직 이 인터페이스나 DB migration을 구현했다고 간주하지 않는다.

```text
git status --short --branch
git fetch origin
python -m pytest -q tests/test_webapp_run_safety.py tests/test_execution_architecture.py
```

프로젝트 interpreter를 사용하고 P0 source gate 상태를 먼저 확인한다. 다음 변경도 별도 기능 브랜치/작은 commit으로 분리한다. 성공 조건은 유효한 durable success를 보존하면서, identity가 불명확한 PID를 종료하지 않고 동일 GPU lane의 중복 시작을 차단하는 Windows/Linux subprocess 계약이다. P1-3 보관/복원, P2 UI/성능, P3 운영 경계는 [WORK_ITEMS.md](WORK_ITEMS.md)에 별도 TODO로 남겼다.

## 원격/운영

새 변경은 [draft PR #4](https://github.com/dbparkJ/RoadInventory-MMS/pull/4)에 push했고 main에 병합하지 않았다. 사전 PR #3 통합만 이번 사용자 명시 요청에 따른 예외다. force push/브랜치 삭제/운영 서비스·DB·원본 변경 없음. HTTP smoke의 직접 생성 child는 종료했다.

롤백은 검증된 이전 release 전환 또는 해당 코드 commit revert이며 reset/DB 삭제를 사용하지 않는다. 이번 변경에는 DB schema migration이 없다. 이전 metadata 없는 설치로 돌아가면 development 호환 경고 상태와 production 시작 gate의 이행 정책을 다시 확인한다.
