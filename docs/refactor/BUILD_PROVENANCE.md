# P0-1 빌드 출처와 정적 UI 정합성

기본 정적 경로는 프로젝트의 `webui/dist`다. `WebAppConfig.static_dir`와
`scripts/run_web.py --static-dir`를 지정하면 **실제 지정된 디렉터리**를 검증하고 제공한다.
작업 디렉터리를 바꾸어도 launcher의 프로젝트 기준 경로는 바뀌지 않는다.

## 실행과 기존 설치 이행

프로젝트 Python 환경을 구성한 뒤 다음 명령으로 같은 소스의 UI를 생성한다.

```text
cd webui
npm ci --no-audit --no-fund
npm run build
cd ..
python scripts/build_web.py verify
python scripts/run_web.py --build-mode production
```

여기서 `python`은 Windows `.venv/Scripts/python.exe`, Linux/WSL
`.venv/bin/python` 등 프로젝트 interpreter를 의미한다. `npm run build`는
`MMS_BUILD_PYTHON`, 프로젝트 `.venv`, PATH의 Python 순서로 interpreter를 선택한다.
추가 Python 패키지는 필요하지 않으며 기존 프로젝트의 Python 3.12 환경을 권장한다.

- 기본값은 `development`: 기존 metadata 없는 설치는 시작을 유지하지만 stderr에
  명확한 경고를 출력하고 build 상태를 `unverified`로 보고한다.
- `--build-mode production` 또는 `MMS_WEB_BUILD_MODE=production`: metadata 누락,
  소스 불일치, asset 누락/변조, 미완료 build를 발견하면 **상태 DB 생성 전** 시작을 거부한다.
- `scripts/build_web.py verify`는 개발 모드와 무관하게 미검증 상태에서 exit 2를 반환한다.
- `setup_web.sh`/`setup_web.ps1`은 npm이 있으면 `npm ci → build → verify`를 실행한다.
  npm이 없으면 프로젝트 Python으로 포함된 소스와 dist를 검증한다. index만 있는 설치는
  더 이상 성공으로 승인하지 않는다. Python 환경 설치의 기존 옵션과 실패 코드를 유지한다.
- Node 없는 기존 설치는 검증된 소스 포함 release package를 설치하거나, Node가 있는
  별도 checkout에서 빌드한 동일 소스/dist를 함께 배포한다. 긴급 호환 실행은 명시적으로
  개발 모드를 사용하되 이를 최신 UI/운영 검증 통과로 기록하지 않는다.

운영의 기존 실행 옵션을 자동 변경하거나 서비스를 재시작하지 않는다.

## manifest와 입력 범위

`webui/dist/build-info.json`은 schema version, build UUID, 실제 build 시작 당시 Git
commit과 `working_tree_dirty`, source fingerprint, lock hash, API contract version,
UTC build 시각, asset inventory/hash, Python/Node/npm version을 기록한다.
Git을 찾을 수 없으면 commit은 `unavailable`, dirty는 `null`이다. 부모 디렉터리의
무관한 Git checkout을 현재 package의 commit으로 사용하지 않는다.

`working_tree_dirty=true`인 결과를 clean commit의 build로 표시하지 않는다. build 후
commit 또는 문서만 변경된 경우 HEAD가 달라도 **현재 소스 fingerprint**가 일치하는지
판정한다. dirty라는 사실 자체는 실패가 아니며 원본 소스와 artifact 일치 여부가 gate다.

`mms-source-v1-text-lf` 입력은 다음과 같다. 정확한 고정 파일 목록은
`mms_shp_detection/build_provenance.py`의 `REQUIRED_INPUTS`에 있다.

- `webui/src`의 runtime 소스: `/test/`, `/__tests__/`, `*.test.*`, `*.spec.*` 제외.
- `webui/public` 전체, index, package.json/lockfile, Vite/TypeScript 설정,
  `webui/scripts/build.mjs`.
- `mms_shp_detection` 아래 모든 `.py`: 프런트만 최신이고 백엔드는 다른 소스인 package도 거부.
- requirements.txt, pipeline/web launcher, build script, 두 web setup script.
- 존재하는 `webui/.env*` 파일. 별도 `VITE_*` build-time 환경 변수는 값 대신 digest만 기록한다.
  runtime 서버의 환경을 프런트 build 환경과 동일하다고 가정하지 않는다.

문서, 테스트, `.git`, 가중치, 원본 MMS, config.yaml의 운영 설정, DB, cache, node_modules,
dist와 manifest 자체는 source fingerprint 입력이 아니다. manifest에 운영 설정이나
환경 변수 전체를 기록하지 않는다. 민감한 `.env`는 일반 package에 포함하지 말고,
그 파일에 의존하지 않는 release build를 준비한다. 배포 소스에서 입력을 빼면 검증은 실패한다.

경로는 상대 POSIX `/` 형식이고 정렬된 `{path: sha256}` JSON을 다시 SHA-256한다.
Python/TS/JS/JSON/CSS/HTML/SVG/TXT/SH/PS1과 `.env*` 텍스트는 UTF-8로 읽어
CRLF를 LF로 바꾼다. 그 외 byte, BOM, 공백, 마지막 newline은 유지한다.
PNG 등의 binary 입력과 **모든 dist artifact는 실제 bytes**를 해시한다.
따라서 Windows/Linux checkout의 통상적인 소스 줄바꿈 차이는 허용하지만
배포 artifact를 변환하면 변조로 검출된다. `.gitattributes`의 `/webui/dist/** -text`로
Git checkout에서도 artifact bytes를 보존한다. 배포에는 검증된 archive를 사용한다.

manifest 자신과 `.build-incomplete`를 제외한 dist 파일 전체를 검증한다.
index의 로컬 script/link/image/source 참조가 실제 inventory에 존재해야 한다.
소스/asset의 symlink와 junction을 따라 외부 내용을 포함하지 않는다.
해시 정합성은 전자서명 또는 공급망 신뢰 검증이 아니다.

## 실패와 배포 전환

`npm run build`는 `webui/.web-build-*` 임시 디렉터리에서 TypeScript 검사와 Vite
production build를 수행한다. build 전후 source inventory가 다르면 게시하지 않는다.
시작 시 이전 dist에 `.build-incomplete`를 남기며, 실패하면 이전 asset bytes는
보존하지만 그 실행을 성공한 새 build로 승인하지 않는다. 예외/비정상 종료는 npm까지
비정상 종료 코드로 전파된다. 재빌드에 성공하면 marker는 새 dist에 포함되지 않는다.

검증된 임시 dist만 rename으로 전환하고 전환 실패 시 이전 디렉터리를 복구한다.
두 rename 사이의 짧은 공백까지 무중단으로 보장하지는 않는다. **실행 중인 운영
checkout에서 build하지 말고 별도 release checkout/디렉터리에서 생성·검증한 뒤
운영자가 서비스의 release 경로를 전환**한다. 본 변경은 운영 전환을 실행하지 않는다.

새 dist에는 이전 `/assets` 파일 중 새 build에 없는 파일도 보존하여 기존 탭의
hash asset 참조를 유지한다. 자동 삭제 기한은 두지 않는다. 장기간 release를 유지할 때
운영자가 기존 탭의 유예 기간을 정해 지난 release 디렉터리를 별도 보관/정리해야 한다.
서로 다른 release 디렉터리를 전환할 때도 직전 `/assets` 보존 정책을 적용한다.
기존 index `no-cache`, `/assets` `immutable` 헤더를 유지한다.

## 브라우저에서 확인

`/api/build`와 `/api/bootstrap`의 추가 `build` 필드에서 backend fingerprint/API와
frontend build ID/commit/dirty/fingerprint/UTC 시각, 검증 상태와 사유 코드를 확인한다.
`/api/build`는 `Cache-Control: no-store`이며 기존 Basic 인증의 보호를 받는다.
절대경로, 사용자 이름, 인증정보는 응답에 포함하지 않는다.

검증 결과는 **서버 생성 시점 snapshot**이다. 실행 중인 backend 파일이나 dist를
제자리에서 바꾸는 배포를 지원하지 않는다. 같은 package로 서버를 다시 시작하고
`/api/build`에서 새 build ID를 확인한다. 검증 명령 자체는 매번 파일을 다시 읽는다.

## 검증과 한계

```text
python -m unittest discover -s tests -p test_build_provenance.py -v
python -m unittest discover -s tests -p test_webapp_build_provenance.py -v
python -m unittest discover -s tests -p test_web_setup_launchers.py -v
```

첫 suite는 표준 라이브러리만 필요하다. fresh/stale 소스, backend 혼합, index-only,
Git/Node 없는 verify, 변조/누락/추가 asset, index missing reference, CRLF/LF 및 binary
구분, dirty 표기, 실패 build/소스의 build 중 변경, 이전 hash asset 보존을 검증한다.
두 번째 suite는 실제 FastAPI API/static 응답, 개발 경고, 운영 시작 차단과
static override를 검증한다. 실제 production build, archive/Gitless smoke와 원격
Linux/Windows CI 실행 상태는 `CHECKPOINT.md`에 별도로 기록한다.

설치 launcher suite는 실제 shell/PowerShell wrapper를 실행하되 Python/CUDA 설치와
npm 작업을 격리된 명령 fixture로 대체한다. 상대 project-root, 사용자 지정 venv,
npm 없는 검증 성공/실패, npm 실패 코드 전파를 검사한다. 해당 OS의 launcher만
실행하므로 Windows 실행에서는 POSIX 3개, Linux 실행에서는 PowerShell 2개가 skip이다.

이 gate는 MMS 계산 정확도, GPU 실행, 작업자 UI 흐름 전체의 검증을 대체하지 않는다.
