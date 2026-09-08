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

프런트 기존 실패는 QA 세션 비동기 초기화 전에 disabled launcher를 누르는 테스트 준비 경합이었다. 버튼 활성화를 기다리도록 수정한 뒤 해당 8개와 전체 398개가 통과했다. 원래 timeout·assertion은 유지했다.

## P0 구현 검증

| 범위 | 실제 명령/결과 | 증거 |
|---|---|---|
| 전체 Python | `.venv/Scripts/python.exe -m pytest -q --junitxml=.cache/refactor-validation/p0-python.xml` → **560 passed, 7 skipped, 3 warnings**, exit 0, 141.19초 | p0-python.log/xml |
| 전체 frontend | `npm --prefix webui test -- --maxWorkers=2` → **398 passed / 39 files**, exit 0, 54.00초 | frontend-p0.log |
| 오류 경계 | diagnostics + execution architecture **35 passed / 2 skips**, pipeline helpers + run safety **122 passed** | P0-4 집중 검증 |
| 결과 비교 | `python -m unittest discover -s tests -p test_compare_mms_results.py -v` → **20 passed** | 합성 fixture, 실제 MMS 아님 |
| build/API/install | provenance **19 passed**, runtime API **4 passed**, Windows launcher **2 passed / POSIX 3 skips** | 전체 suite에도 포함 |
| release 보호 | `python -m pytest -q tests/test_release_package.py` → **7 passed** | config override 유출 방지·disk-full 부분 ZIP 제거·source race 포함 |
| 실패 전파 | `python scripts/check_ci_failure_propagation.py` → pytest=1, tsc=2, Vitest=1, Vite=1 확인, probe exit 0 | ci-failure-probes.log; 정상 suite와 독립 |
| 최종 build | `npm --prefix webui run build` → tsc + Vite + staged 검증 exit 0 | p0-build.log |
| source/assets | `python scripts/build_web.py verify` → verified/exit 0 | p0-build-verify.log, post-commit-verify.log |
| release archive | `python scripts/package_release.py --output .cache/release/roadinventory-mms-p0-371776b.zip` → verified/exit 0 | p0-package.log, Gitless 추출본 재검증 포함 |
| 실제 HTTP | 테스트 전용 storage/state, `--no-run-worker --build-mode production`, `/api/build`, bootstrap, index, 4개 참조 asset 확인 → PASS | http-smoke.log; 서버 child 종료 완료 |
| Git bytes | `git archive HEAD`를 별도 임시 디렉터리에 추출 → source/assets verified | 406862c의 실제 Git 저장 bytes 검증 |
| CI portability fix | `python -m pytest -q tests/test_bootstrap_environment.py` → **18 passed** | 설치 wheel과 독립적인 CUDA fallback fixture |
| Windows TEMP 경로 | core/diagnostics/helpers/pointcloud/setup/build-API/run-safety 7개 파일 suite → **189 passed / 4 skipped**, 33.56초 | 기준 root 정규화 후 기존 오류 주입·보호 assertion 유지 |

새 skip 3개는 Windows에서 실행할 수 없는 POSIX 설치 launcher이며, 기존 symlink 4개와 합해 로컬 7개다. Linux CI에서는 POSIX launcher와 symlink 검사를 실행하고 Windows launcher 2개가 skip된다. 전체 suite의 추가 경고 2개는 metadata 없는 합성 app fixture의 의도된 development 경고다.

최종 runtime source build는 clean `371776b8da3f49783f0aeffd9c846c233a88d44d`에서 생성했다. 이후 `b1eaa64`/`392c343`은 테스트 fixture만 변경했고 `406862c`는 생성 artifact만 저장하여 runtime source fingerprint는 동일하다.

- build ID: `3ea5b699776c451b8f3a4187110535af`, `working_tree_dirty=false`.
- source fingerprint: `22d09af6e7d10680dfe39e6124b1eab750159c66af59d02a9ca9e5c6cc321b64`.
- build UTC: `2026-09-08T00:18:25.657465+00:00`.
- 로컬 ZIP: 1,581,886 bytes, SHA-256 `c39c887ab1e5496a5648be08a07b2b383f43469b7955f1a37e9e64e2ced4427f`.
- Gitless ZIP에는 source 입력 139개, asset 18개 및 필요한 launcher/문서가 포함되며 원본 data/models/DB/.git/node_modules는 0개다.
- 생성 artifact는 기존 CRLF bytes/라이브러리 shader 문자열을 유지한다. `.gitattributes`로 변환을 막고 checksum을 검증한다. source diff whitespace 검사는 generated dist를 제외하고 통과했다.

## 원격 CI

[초기 실행 34172853900](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34172853900)에서 Windows/Linux frontend+package 두 job이 성공했다. Ubuntu Python은 **564 passed / 1 failed / 2 skipped**였다. 실패는 CUDA wheel의 GPU 부재 경로를 검사하는 기존 fixture가 실제 설치된 CPU wheel metadata를 사용했기 때문이다. `b1eaa64`에서 CUDA wheel metadata를 fixture에 명시했고 출력 assertion은 그대로 유지했다. 실제 환경 smoke는 모의 값 없이 CPU wheel로 계속 검증한다.

수정 후 [406862c push 검증](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34173058375)과 [PR 검증](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34173060719)에서 Linux/프런트 통과 및 Windows TEMP 경로 문제를 확인했다. 이전 실행의 중단은 새 commit의 concurrency 정책에 따른 것이다.

- Ubuntu CPU: **565 passed / 2 skipped**, 52.46초. skip 2개는 Windows PowerShell launcher이고 POSIX/symlink 계약은 실행됐다. 실제 CPU 환경 smoke와 failure probe도 성공했다.
- Ubuntu frontend: **398 passed / 39 files**, 28.22초. production build·실패 전파·Gitless package 성공.
- Windows frontend: **398 passed / 39 files**, 41.95초. production build·실패 전파·Gitless package 성공.
- Windows CPU 첫 재검증: **553 passed / 11 failed / 3 skipped**. hosted runner의 TEMP 짧은 경로(`RUNNER~1`)와 `Path.resolve()`의 정규 경로가 달라 테스트 fixture의 경로 equality 및 오류 주입 대상이 어긋났다. 실패한 fixture의 기준 root를 정규화하는 test-only 수정으로 assertion·rollback/권한 오류 주입·symlink 방어를 그대로 유지하며 재검증한다. runtime 계산/저장 코드를 변경하는 우회는 하지 않는다.
- 원격 두 ZIP의 source fingerprint와 source 파일 139개는 로컬과 정확히 같다. Ubuntu asset 22개/Windows 18개 차이는 LF/CRLF 입력에서 생성한 새 chunk와 이전 asset 유지 정책의 결과이며 source 차이가 아니다. 각 ZIP은 자신의 실제 artifact bytes로 검증됐다.

최종 코드 `392c3438898d102e5b5d66b98458d0ca79ab119b`의 [Validation 34173471638](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34173471638)은 **4개 job 모두 success**로 완료됐다.

| 최종 hosted job | 결과 | 시간/skip |
|---|---|---|
| Python CPU (ubuntu-latest) | **565 passed / 2 skipped** | 49.01초; Windows PowerShell launcher 2개 |
| Python CPU (windows-latest) | **564 passed / 3 skipped** | 110.84초; POSIX launcher 3개, symlink 보호 검사 실행됨 |
| Frontend and package (ubuntu-latest) | **398 passed / 39 files**, tsc/build/probes/package 통과 | frontend 26.57초 |
| Frontend and package (windows-latest) | **398 passed / 39 files**, tsc/build/probes/package 통과 | frontend 27.26초 |

CPU 환경 smoke는 두 OS에서 실제 CPU wheel로 실행했고, 실패 전파 probe도 각각 성공했다. 이 코드 이후 체크포인트/기록 변경은 문서만이며 source fingerprint는 동일하다. 현재 PR의 최신 check 상태는 [PR #4](https://github.com/dbparkJ/RoadInventory-MMS/pull/4)에서 확인한다. required checks 설정은 변경하지 않았다.

## 검증 수준

- STATIC_REVIEWED: A0 호출/저장/설정 경계.
- UNIT_CONTRACT_PASSED: P0 Python/frontend/합성 비교/배포 계약.
- INTEGRATION_PASSED: CUDA environment smoke, 설치 wrapper, 실제 localhost API/static 및 Gitless package에 한정.
- REAL_MMS_PASSED: **아님**. 사용자가 대표 fixture/golden/tolerance profile이 없다고 확인했으며 실제 검증 미완료 기록을 지시했다.
- BROWSER_PASSED: **아님**. Browser runtime 설정 후 `No browser is available`, 지원되는 discovery 결과 `[]` 확인. 실제 UI 렌더/분리 창/작업 흐름 검증은 차단. HTTP 응답 성공을 브라우저 성공으로 표시하지 않는다.
- OPERATION_APPROVED: **아님**. 사전 GitHub 브랜치 통합 승인과 운영 적용 승인을 혼동하지 않는다.

## P1/P2/P3 독립 변경 및 통합 검증

2026-09-08, Windows/Python 3.12.10/Node 22.17.0. 모든 실제 HTTP·DB·프로세스 테스트는 별도 합성 state/storage를 사용했다. 운영 데이터 백업/복원이나 실제 MMS 비교는 실행하지 않았다.

| 범위 | 실제 검증 결과 | 근거 |
|---|---|---|
| P1 소유권 | 초기 전체 Python 567 passed / 7 skipped. 실제 OS child/grandchild·부모 종료·private 승인·PID 재사용 및 Windows exit 259 테스트 6 passed. 후속 UI 두 파일 12 passed | tests/test_process_ownership.py, test_webapp_run_safety.py, P1_OWNERSHIP.md; 최종 통합 suite 포함 |
| P1 보존/복원 | 초기 전체 Python 581 passed / 7 skipped. 최종 preservation 22개; provenance/package와 합쳐 48 passed | tests/test_preservation.py, P1_PRESERVATION.md; 실제 운영 DB 복원 아님 |
| P2 preview | Python 관련 48 passed / 1 local symlink skip; frontend 관련 103 passed. 992회 합성 cold/warm 비교에서 case별 source/output SHA 동일 | P2_PREVIEW.md, P2_PREVIEW_BENCHMARK.json. 성능은 후보 A 6/16 초과, B 0/16이므로 미통과 |
| P3 경계 | 최종 tests/test_webapp_auth_boundaries.py 19 passed. 실제 Vite proxy→backend 같은 Origin upload 201, 외부 Origin 403 | p3_proxy_smoke.py, p3-proxy-smoke.log; no-run-worker. 합성 프로세스 Job 소유 후 종료·임시 경로 정리까지 성공 |
| 통합 Python | `.venv/Scripts/python.exe -m pytest -q --junitxml=.cache/refactor-validation/incremental-python.xml` → **615 passed / 7 skipped**, exit 0, 178.66초 | incremental-python.log/xml. 합친 runtime source에서 실행, 이후 UI 안내/문서/빌드만 변경 |
| 통합 frontend | `npm --prefix webui test -- --maxWorkers=2` → **400 passed / 39 files**, exit 0, 49.89초 | incremental-frontend-final.log. 최종 소유권 안내 포함 |

통합 Python의 142 warnings에는 통합 재빌드 전 의도된 development source/asset 불일치 경고가 포함된다. production gate를 완화하지 않았다. 최종 통합 build/verify 및 원격 clean build 결과를 아래에 별도 기록한다. 로컬 7 skips는 symlink 생성 권한 4개와 POSIX launcher 3개다. hosted Windows에서는 symlink 검사가 실행된다.

최종 개별 코드·asset의 push 및 PR CI 모두 success:

- P0 `bc5cef6`: [push 34173801258](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34173801258), [PR 34173803932](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34173803932).
- P3 `fc40b89`: [push 34178066755](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178066755), [PR 34178069397](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178069397).
- P2 `75210ce`: [push 34177973925](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34177973925), [PR 34177978167](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34177978167).
- P1 보존 `9af0ca0`: [push 34178161530](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178161530), [PR 34178167114](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178167114).
- P1 소유권의 UI 추가 전 `cbf41a9`: [push 34178158169](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178158169), [PR 34178164283](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178164283). UI 추가 `5631241`은 후속 원격 실행 결과를 확인한다.

소유권 registry additive migration과 preservation의 미해결 ownership 거부는 통합 suite에서 함께 검증했다. Linux native/GPU hang 즉시 종료, 다중 host exactly-once, 실제 브라우저, 실제 MMS 정확도는 이 CI가 증명하지 않는다. Browser discovery는 재개 후에도 `[]`였다.

### 최종 통합 배포 파일과 HTTP

- clean source `c07e4bea2d0136c7a3aa7c3debf0b504fb322875` → `npm --prefix webui run build` exit 0; build ID `dbff836f69bf4365a7bc6d04fb1a1652`, working_tree_dirty=false.
- source/backend fingerprint `deb1994762982677b7ee2c113b6a2f95291a09ffda50f1b1d43b419eaffc2e7f`; `python scripts/build_web.py verify` verified/exit 0. Logs: incremental-build.log, incremental-build-verify.log.
- `python scripts/package_release.py --output .cache/release/roadinventory-mms-incremental-c07e4be.zip` → verified, 180 files. 1,784,745 bytes, SHA-256 `a259b8de55dfa41aff1de25d9afe8f4d313327d0804ffc5a0b031af1925cdf7d`. Gitless 추출본 재검증 및 원본 data/models/DB/.env/.git/node_modules 미포함 검사 통과. ZIP은 ignored 로컬 산출물이며 공개 release로 게시하지 않았다.
- artifact commit `e28c39c1d3f5992f2f3fdd83f00471f9874fd32b`의 실제 `git archive` bytes → verified. Log: incremental-git-archive.log. 과거 asset도 기존 cache 보호를 위해 유지한다.
- 실제 localhost 서버에 `--no-run-worker --build-mode production` 적용. `/api/build` verified/no-store, bootstrap metadata 일치, index와 참조 asset 4개 exact bytes, 인증 asset private cache 확인. Vite proxy 쓰기 201/외부 Origin 403 확인. Log: incremental-http-smoke.log. 직접 생성한 Python/Node를 시작 전 소유 Job에 연결하고 종료·임시 경로 정리까지 exit 0으로 확인했다. 실제 브라우저 테스트는 아니다.
- P1 소유권 최종 `5631241`도 [push 34178972208](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178972208), [PR 34178974478](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34178974478)의 Windows/Linux 4개 job 모두 success다.

### 통합 원격 CI 완료

검증된 source/artifact `e28c39c1d3f5992f2f3fdd83f00471f9874fd32b`의 [Validation 34179470399](https://github.com/dbparkJ/RoadInventory-MMS/actions/runs/34179470399)은 **4개 job 모두 success**다. 실제 로그는 incremental-ci.log에 저장했다.

| hosted job | 결과 |
|---|---|
| Python CPU ubuntu-latest | **619 passed / 3 skipped**, 65.01초 |
| Python CPU windows-latest | **619 passed / 3 skipped**, 195.09초 |
| Frontend and package ubuntu-latest | **400 passed / 39 files**, tsc/build/failure probes/Gitless package success, 184 package files |
| Frontend and package windows-latest | **400 passed / 39 files**, tsc/build/failure probes/Gitless package success, 180 package files |

Linux skips는 Windows launcher 2개와 Windows 전용 exit 259 사례 1개다. Windows skips는 POSIX launcher 3개이며 실제 Windows Job/exit 259와 symlink 보호 검사는 실행됐다. 추가 Ubuntu asset 4개는 LF/CRLF 입력에 따라 생성한 chunk와 과거 asset 유지 정책의 결과이며 각 OS package의 source/asset 검증이 통과했다. CPU environment smoke와 의도된 실패 전파도 양 OS에서 통과했다.

이후 체크포인트/검증 기록 commit은 문서만이며 runtime fingerprint와 검증된 build bytes를 바꾸지 않는다. 신규 main 병합/운영 적용은 실행하지 않았다. 최종 branch의 자동 후속 CI는 [브랜치 Actions](https://github.com/dbparkJ/RoadInventory-MMS/actions?query=branch%3Arefactor%2Froadinventory-incremental)에서 확인할 수 있다.
