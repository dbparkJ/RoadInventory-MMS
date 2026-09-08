# CI와 동일 소스 패키지

`.github/workflows/validation.yml`은 PR, main/refactor 브랜치 push, 수동 실행에 반응한다. GitHub-hosted Windows/Linux에서 Python 3.12.10 CPU 계약 suite와 Node 22.17.0 frontend/type/build를 각각 실행한다. GPU가 없는 일반 CI의 성공은 실제 MMS·GPU 정확도 signoff가 아니다.

설치·테스트 단계는 실패를 전파하는 bash shell로 실행하며 continue-on-error나 전체 skip을 사용하지 않는다. Python runtime 고정 버전은 유지하고 pytest만 requirements-dev.txt로 분리했다. 기존 lint 부채를 일괄 포맷하지 않는다. 직접 고정 버전 밖 전이 의존성은 완전한 lock이 아니므로 새 runner 설치 결과를 별도 검증한다.

## 로컬 명령

프로젝트 Python interpreter를 사용한다. Windows는 `.venv/Scripts/python.exe`, Linux는 `.venv/bin/python`이다.

```text
python -m pytest -q --junitxml=test-results/python.xml
npm --prefix webui ci --no-audit --no-fund
node webui/node_modules/typescript/bin/tsc -b webui
npm --prefix webui test -- --maxWorkers=2
npm --prefix webui run build
python scripts/check_ci_failure_propagation.py
python scripts/build_web.py verify
python scripts/package_release.py --output .cache/release/roadinventory-mms.zip
```

실패 probe는 임시 fixture에서 pytest/Vitest assertion, TypeScript 타입 오류, Vite config 오류를 의도적으로 생성한다. 예상한 실패 marker와 비정상 종료 코드를 모두 확인해야 probe 자체가 성공한다. tracked 소스/기존 dist를 수정하지 않으며 정상 테스트 gate는 그대로 별도로 실행된다.

package_release는 **현재 checkout의 source fingerprint와 dist를 검사한 다음** 필요한 소스·실행 스크립트·프런트 빌드를 하나의 ZIP으로 만든다. Git 없는 별도 임시 디렉터리에 풀어 같은 검사에 통과해야 공개한다. 로컬 `.env`가 빌드 입력이면 비밀정보를 자동 복사하지 않고 패키징을 실패시킨다. config.yaml은 이 checkout의 HEAD에 저장된 템플릿과 같을 때만 포함하며 로컬 수정은 거부한다. Git 없는 설치본의 실행·검증에는 Git이 필요 없지만, config를 포함한 재패키징에는 원본 checkout이 필요하다. 동일 output 파일은 덮어쓰지 않으며 복사 실패로 생긴 부분 파일은 제거한다. data/models/DB/.git/node_modules는 포함하지 않는다. 의존성·운영 데이터·모델은 승인된 별도 설치 경로에서 준비해야 한다.

검증 결과 XML과 검증된 소스/UI ZIP만 Actions artifact로 14일 보관한다. operational logs·원본 MMS·환경 변수 전체는 artifact에 올리지 않는다. workflow 권한은 contents: read이며, 운영 self-hosted GPU runner/pull_request_target/자동 배포를 사용하지 않는다.

## Required checks

관리자가 아래 status를 main 보호 설정에서 required로 지정할 수 있다. 이번 작업은 repository ruleset/protection을 변경하지 않는다.

- `Python CPU (ubuntu-latest)`
- `Python CPU (windows-latest)`
- `Frontend and package (ubuntu-latest)`
- `Frontend and package (windows-latest)`

A0에서 rulesets는 빈 목록, main은 unprotected였다. 원격 실행 결과는 VALIDATION.md에서 실제 관찰된 상태로 갱신한다. YAML 존재와 로컬 통과만으로 원격 CI 성공을 주장하지 않는다.

CI action은 확인한 공식 commit SHA로 고정했다: [checkout](https://github.com/actions/checkout), [setup-python](https://github.com/actions/setup-python), [setup-node](https://github.com/actions/setup-node), [upload-artifact](https://github.com/actions/upload-artifact). 애플리케이션 runtime 의존성의 무관한 최신화는 하지 않는다.
