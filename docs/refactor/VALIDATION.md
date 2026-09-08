# 실제 실행 검증 기록

2026-09-08, Asia/Seoul. Windows / 프로젝트 CPython 3.12.10 / Node 22.17.0. 아래 로그는 원본 데이터나 인증정보를 포함하는 운영 로그가 아니라 로컬 테스트 로그이며 `.cache/refactor-validation/`에 보관한다. 원격 artifact에는 합성 테스트 결과만 게시한다.

| 단계 | 명령 (저장소 루트) | 결과 | 로그 |
|---|---|---|---|
| baseline Python | `.venv/Scripts/python.exe -m pytest -q --junitxml=.cache/refactor-validation/integration-python.xml` | 498 passed / 4 skipped / exit 0 | integration-python.log/xml |
| baseline 설치 | `npm --prefix webui ci --no-audit --no-fund` | exit 0 | integration-npm-ci.log |
| baseline frontend | `npm --prefix webui test -- --run` | 397 passed / 1 failed / exit 1 | integration-frontend.log |
| 경합 재현 | `npm --prefix webui test -- --run --maxWorkers=2` | 397 passed / 1 failed / exit 1 | integration-frontend-limited.log |
| 단독 재현 | `npm --prefix webui test -- src/components/QaIssuePanel.test.tsx --maxWorkers=1` | 6 passed / 2 failed / exit 1 | integration-qa-isolated.log |
| baseline build | `npm --prefix webui run build -- --outDir ../.cache/refactor-validation/baseline-dist` (기존 build 명령) | tsc/Vite exit 0 | integration-build.log |
| dependency | `.venv/Scripts/python.exe -m pip check` | No broken requirements / exit 0 | 세션 출력 |
| GPU smoke | `.venv/Scripts/python.exe scripts/verify_environment.py --expected-torch-runtime cu128` | CUDA NMS `[0]`/유한 행렬곱/exit 0 | gpu-smoke.log |

Python의 4 skip은 Windows symlink 생성 권한 부족이다. Linux CI에서 해당 계약을 검증할 예정이다. 기존 Starlette/httpx deprecation 경고 1개는 무관한 의존성 업그레이드로 처리하지 않는다.

프런트 기존 실패는 QA 세션 비동기 초기화 전에 disabled launcher를 누르는 테스트 준비 경합으로 조사 중이다. 원래 timeout을 늘리거나 테스트를 제외하지 않는다.

## 검증 수준

- STATIC_REVIEWED: A0 호출/저장/설정 경계.
- UNIT_CONTRACT_PASSED: baseline Python에 한정, 프런트 실패는 별도 표시.
- INTEGRATION_PASSED: CUDA environment smoke에 한정.
- REAL_MMS_PASSED: **아님**. 대표 fixture/golden/tolerance profile 미확정.
- BROWSER_PASSED: 아직 미실행.
- OPERATION_APPROVED: **아님**. 사전 GitHub 브랜치 통합 승인과 운영 적용 승인을 혼동하지 않는다.
