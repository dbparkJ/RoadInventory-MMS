# A0 기준선 — 2026-09-08

- 조사: 2026-09-08 09:00–09:08, Asia/Seoul.
- 최초 로컬/원격 main: `aac6a30a37983cc7387f51d0ea2c8aeb83e9e7cf`.
- 사용자 요청에 따른 사전 통합: [PR #3](https://github.com/dbparkJ/RoadInventory-MMS/pull/3), `d3d7d9af07518282afcb332df100724c1b04de9d` (09:07:41 KST 병합). 로컬 main도 fast-forward 완료.
- 통합 내용: 미추적 실행 명세를 커밋, 로컬 source-inventory 브랜치 `bc14a81` 푸시/병합, 이미 squash 병합된 성능 브랜치의 이력 통합. 기존 브랜치/별도 worktree는 보존.
- 구현 브랜치: `refactor/roadinventory-p0-reliability`. 이후 구현은 main에 직접 쓰지 않으며 자동 병합하지 않는다.
- 시작 시 사용자 변경은 명세 문서 한 개뿐이며 요청대로 커밋했다. 별도 worktree는 clean. AGENTS.md는 조사한 저장소/상위 경로에서 발견하지 못했다.
- 저장소는 public. 기본 브랜치는 main, repository rulesets는 빈 목록, main protection 조회는 `Branch not protected` (404). 기존 원격 Actions에는 Dependency Graph 성공만 조회됨. 보호 설정은 변경하지 않았다.

## 환경

Windows x86-64, 프로젝트 `.venv`, CPython 3.12.10 / pytest 8.4.2, Node 22.17.0 / npm 10.9.2. Python runtime 직접 의존성은 requirements.txt에 이미 고정되어 있다. `pip check` 성공. WSL은 별도 검증하지 않았다.

RTX 4070 Laptop 8 GiB, driver 591.74, torch 2.7.1+cu128 / CUDA 12.8. `verify_environment.py --expected-torch-runtime cu128`의 CUDA NMS 및 행렬곱 smoke 성공. 모델 2개와 로컬 데이터가 존재하지만, 승인된 대표 fixture·golden 정답·좌표 허용오차 profile은 아직 지정되지 않았다. 실제 MMS 동등성 검증을 수행했다고 해석하지 않는다. 조사 시 작업 드라이브 가용 용량 약 407 GB.

원본 데이터·가중치·운영 DB·서비스는 수정하지 않는다. 테스트 출력은 `.cache/refactor-validation/` 및 테스트별 임시 디렉터리이며, 보고서에는 원본 경로를 포함하지 않는다.

## F01–F14 재확인

| ID | A0 판정 | 실제 근거/범위 |
|---|---|---|
| F01 | ALREADY_FIXED | PR #2는 2026-08-27 병합됨. main과 성능 브랜치의 코드 tree 차이 없음. 재구현하지 않음. |
| F02 | CONFIRMED | dist 마지막 변경 c8d562b, frontend 마지막 변경 aac6a30. 새 별도 build의 JS hash가 포함된 dist와 다름. 운영 설치 상태는 미조회. |
| F03 | CONFIRMED | app.py 기본 webui/dist, setup_web 두 launcher가 npm 없음+index 존재만으로 성공 처리. |
| F04 | CONFIRMED | pipeline/overlays/store/UI context에 큰 책임 결합이 남음. current_architecture.md의 호출 경계 표를 재사용. |
| F05 | CONFIRMED | 로컬 검증 workflow 없음. 원격에는 Dependency Graph 이력만 조회, 보호 설정 없음. |
| F06 | CONFIRMED | 실제 MMS golden 및 장시간 GPU 증거 공백. 이번 CUDA smoke는 그 대체가 아님. |
| F07 | CONFIRMED | App.tsx REVIEW_WORKSPACE_UI_ENABLED=false. 운영 체크리스트의 workspace 절차에 비활성 범위 표시 필요. |
| F08 | CONFIRMED | PipelineContext/StageResult 존재, detect/project/estimate 실제 처리 결합 유지. |
| F09 | CONFIRMED | 프레임 fingerprint 재사용은 있으나 독립 stage checkpoint/retry executor는 없음. |
| F10 | CONFIRMED | 재시작된 child identity 보장 공백. 중복 GPU 사고가 관찰됐다는 뜻은 아님. |
| F11 | CONFIRMED | BasicAuth/worker lock/operator-local/process-local fence 존재. 다중 사용자 인증 이력과 구분. |
| F12 | CONFIRMED | run_history가 과거 산출물 bytes 전체의 불변 보관을 보장하지 않음. |
| F13 | CONFIRMED | panorama_fastpath.install이 media 함수를 치환. 기존 성능 개선은 유지. |
| F14 | CONFIRMED | pipeline_error_info가 모든 일반 PermissionError를 OUTPUT_WRITE_FAILED로 분류. |

## 수정 전 검증

통합 후보 `379b2a9`에서 실행했으며 병합 main `d3d7d9a`와 source tree가 같다.

- Python: **498 passed, 4 skipped**, exit 0, 146.89초. skip은 Windows symlink 생성 권한 제약 4개이며 테스트를 새로 제외한 것이 아니다.
- 프런트: **397 passed, 1 failed**, exit 1. workers=2에서도 동일 QA 테스트 실패. 단독 QA 파일에서는 **6 passed, 2 failed**로 초기 세션 준비 경합 추가 재현. frontend 소스/lock/config는 기존 main과 동일하여 통합 회귀가 아닌 기존 baseline 실패로 분류.
- TypeScript + Vite production build: exit 0. 기존 추적 dist를 보존하고 별도 baseline-dist에 생성.
- npm ci: exit 0. `pip check`: exit 0. CUDA smoke: exit 0.

로그 및 상세 명령은 [VALIDATION.md](VALIDATION.md). A0는 실행 가능한 기준선·사용자 변경 보호·다음 P0 구현 범위를 확보하여 통과했다. 전체 테스트가 모두 통과했다는 뜻은 아니다.
