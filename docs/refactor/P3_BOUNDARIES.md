# P3-1 단일 서버의 인증·브라우저 요청 경계

기준은 P0 체크포인트 `bc5cef6`이다. 운영 설정·방화벽·인증 계정은 변경하지 않았다.
공유 `operator-local`, 기존 사용자 제공 actor와 과거 provenance의 의미를 유지한다.

## 실제 인증 적용 범위

`WebAppConfig.auth_username/auth_password`를 함께 설정하면 전역 Basic 인증이 라우팅보다
먼저 실행된다. API별 선택 적용이나 프런트 feature flag에 의존하지 않는다.

| 경로/기능 | Basic 설정 시 | 검증 |
|---|---|---|
| health, build, bootstrap, storage | 인증 필수 | 실제 ASGI 응답, build no-store |
| dataset 탐색·등록, upload 생성/chunk/complete | 인증 필수 | OpenAPI 전체 HTTP method/path 목록 순회 |
| run 제출·취소·삭제·상태 | 인증 필수 | 동일 전수 검사, worker 꺼짐 확인 |
| preview, 원본/파생 파일, SHP/이미지 ZIP | 인증 필수 | 동일 전수 검사, archive 인증 후 missing ID는 404 |
| run SSE | 인증 필수 | 동일 전수 검사, 인증 후 missing ID는 404 |
| 편집·proposal·QA·검수·report API | 인증 필수 | 동일 전수 검사; 비활성 UI도 인증 우회 불가 |
| UI HTML, assets, SPA fallback | 인증 필수 | 실제 합성 정적 파일 응답 검사 |
| docs, redoc, OpenAPI, 알 수 없는 URL | 인증 필수 | 실제 401 challenge 검사 |

인증을 설정하지 않은 기본 로컬 모드는 그대로다. 인증 성공은 접근 허용을 뜻하며
별도의 역할·dataset별 권한이나 인증 사용자별 편집 이력을 보장하지 않는다.

## 이번 변경

- CLI의 “내장 인증 없음” 안내를 선택형 Basic 인증의 실제 기능과 맞췄다.
- 인증/브라우저 출처 검사에서 조기 반환하는 401·403에도 기존 보안 헤더를 적용하고
  `Cache-Control: no-store`를 지정한다.
- Basic 인증을 설정한 정적 assets는 브라우저의 immutable cache를 유지하되 shared cache에
  공개 저장되지 않도록 `private`로 지정한다. 인증 없는 assets의 기존 public cache는 유지한다.
  기존 media FileResponse/304의 명시 public cache도 인증이 켜져 있으면 private로 바꾼다.
- POST/PUT/PATCH/DELETE 등 쓰기 요청의 Origin이 현재 요청의 scheme/host/port와 다르거나
  불투명(null)·복수·잘못된 값이면 endpoint 실행 전에 403을 반환한다.
  `Sec-Fetch-Site: cross-site`도 거부한다. 같은 origin 및 Origin 없는 CLI 요청은 지원한다.

Origin의 기본 port를 정규화하되 실제 다른 port는 구분한다. same-site 하위 도메인도
다른 origin이면 쓰기를 거부한다. CORS를 활성화하거나 허용 origin을 넓히지 않았다.
업로드 생성 전 거부되어 테스트 저장소에 staging이 생기지 않는지 검증한다.

## 운영 조건과 남은 한계

- Basic은 TLS를 제공하지 않는다. 외부 접근에는 검증된 TLS endpoint와 접근 통제가 필요하다.
  reverse proxy를 사용할 경우 앱 listener로 직접 우회하지 못하게 배치한다.
- 앱은 Host allowlist를 새로 만들지 않았다. proxy에서 실제 운영 Host만 허용하고,
  앱에 전달하는 Host/scheme과 proxy 신뢰 범위를 일치시켜야 한다. 임의 외부
  `X-Forwarded-*`나 사용자 헤더를 신뢰하도록 설정하지 않는다.
- Origin/Fetch Metadata는 브라우저 쓰기 방어이며 인증 또는 전면적인 CSRF 해결을 뜻하지 않는다.
  브라우저 헤더가 모두 없는 API 요청은 호환성을 위해 허용한다. 실제 reverse proxy와
  브라우저의 Basic 재전송·origin 동작은 배포 환경에서 추가 검증해야 한다.
- HTTP GET은 기존 preview 생성·읽기 기능을 유지한다. 인증 없는 원격 공개가 안전하다고
  해석하지 않는다. 해당 외부 공개·방화벽 설정은 이번 작업에서 수행하지 않았다.
- `created_by`, `actor`, `claimed_by`는 현재 요청에 포함되는 업무 식별값이다. task claim과
  idempotency/CAS 검사는 유지하지만 이를 인증된 principal로 승격하지 않는다.
  `X-Forwarded-User` 같은 임의 헤더와 Basic 사용자명을 과거 provenance에 덮어쓰지 않는다.
- 브라우저 지도 키는 클라이언트용 값이다. 서버 비밀번호와 구분하며 실제 운영 origin 제한은
  지도 제공자 배포 설정에서 확인해야 한다. 이 작업에서 키를 교체하거나 노출하지 않았다.
- ASGI 단일 worker, 허용 root/path 제한, symlink/junction 방어, review UI 비활성은 유지한다.

헤더 설계 참고: [Starlette middleware](https://www.starlette.io/middleware/),
[MDN Fetch Metadata](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Fetch_metadata).
적용 여부는 문서 추론 대신 이 저장소의 ASGI 테스트로 검증한다.

## 검증 및 되돌리기

`tests/test_webapp_auth_boundaries.py`는 실제 등록된 OpenAPI 경로 전체의 인증 challenge,
정적 파일·SSE·ZIP 경계, 한글 upload, 교차 origin의 상태 변경 거부, 같은 origin/CLI 호환,
401·403 보안 헤더와 cache 정책을 검증한다. 기존 health/path/upload/편집 테스트도 실행한다.
실제 브라우저는 연결 불가여서 실행하지 못했고 MMS 정확도와 무관한 변경이다.

2026-09-08 로컬 Windows/Python 3.12.10에서 신규 경계 테스트 18개가 통과했다.
전체 회귀 결과와 hosted OS 결과는 PR 및 최종 체크포인트에 기록한다.

DB migration과 새 runtime 의존성은 없다. 코드 revert 및 해당 소스에 맞는 UI 재빌드로
되돌릴 수 있다. 현재 운영 인증/방화벽/서비스를 바꾸는 명령은 실행하지 않았다.
