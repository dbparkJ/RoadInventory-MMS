# 기능 상태와 오류 계약

기준: 2026-09-08, 증분 리팩터링 P0-4. 구현 여부, 자동 검증, 실제 MMS 검증, 배포 활성화는 서로 다른 상태다. 실행 환경과 전체 검증 결과는 [VALIDATION.md](VALIDATION.md), 기존 호출 구조는 [current_architecture.md](../current_architecture.md)에 기록한다. 아래 내용은 운영 서버를 조사하거나 배포를 적용했다는 뜻이 아니다.

## 현재 공개 범위

| 기능 | 구현·자동 검증 근거 | 실제 MMS 검증 | 기본 UI 공개 정책 |
| --- | --- | --- | --- |
| 데이터 탐색, 지도·파노라마·점군, 분리 창 | 기존 뷰어·분리 창 코드와 프런트 회귀 테스트 | 이번 변경의 운영 스모크 미수행 | 기존 활성 상태 유지 |
| 지주 `N`, `B → B`, 수동 표지판 bbox `M`, 임시 제안·확정 | `OverlayContext`, `ManualObjectContext`, 관련 API·편집 테스트 | 이번 변경의 실제 좌표·저장 검증 미수행 | 기존 활성 상태 유지 |
| 검수 session/task, 큐, QA, 완료, 보고서 | 서버 API와 React 컴포넌트, 검수 계약 테스트 존재 | 활성화를 위한 실제 MMS 스모크 미수행 | `App.tsx`의 `REVIEW_WORKSPACE_UI_ENABLED = false` 유지 |
| 능동학습 export | 서버 capability·독립 정책 및 export API 존재 | 이번 변경에서 export 실데이터 검증 미수행 | 서버 허용과 해당 UI 공개 조건이 모두 필요 |
| 권한 오류 진단 | 입력 scanner·텍스트 output writer 경계와 `test_pipeline_error_diagnostics.py` | 실제 NAS/운영 ACL 장애 미수행 | 기존 실행 응답에 추가 진단 정보 제공 |

`capabilities.review_workspace`는 서버 계약의 제공 여부다. 프런트 공개 flag나 사용자 권한을 대체하지 않는다. 검수 기능의 비활성화 이유는 기존 배포 정책에 따라 작업 바·큐·QA와 session 상태·검수 단축키를 함께 보류하는 것이다. 이번 변경으로 활성화하지 않는다. 체크리스트는 [현재 활성 기능](../OPERATOR_MMS_SMOKE_CHECKLIST.md#1-현재-활성-기능-검증)과 [검수 workspace 활성화 후 검증](../OPERATOR_MMS_SMOKE_CHECKLIST.md#2-검수-workspace-활성화-후-검증)을 분리한다.

`operator-local`은 단일 운영 모드의 공유 작업자 식별자다. 기존 선택형 Basic 인증과 별개이며, 인증된 다중 사용자별 이력을 보장하지 않는다. 다중 작업자 권한이나 ASGI worker 수를 이번 변경으로 확장하지 않는다.

## 실행 오류 API 호환성

`RunErrorInfo`의 기존 `code`, `message`, `stage`, `job_id`, `retryable`, `cause_type`를 유지한다. operation은 기존 확장 가능한 `context` 객체의 선택적 `operation` 키로 제공한다. 최상위 schema나 기존 필수 필드는 변경하지 않는다. 현재 UI는 `message`를 표시하고 오류 code 타입은 문자열이므로 새 입력 오류 코드를 수용한다.

| 검증된 원인 | code | context.operation | 안내 정책 |
| --- | --- | --- | --- |
| 입력 탐색의 실제 읽기에서 PermissionError | `INPUT_READ_FAILED` | `read_input` | 입력 읽기 권한 확인, 원래 예외 경로·내용을 메시지에 넣지 않음 |
| `atomic_write_text`의 실제 write/replace에서 PermissionError | `OUTPUT_WRITE_FAILED` | `write_output` | 출력 권한·파일 잠금 확인, 원래 예외 경로·내용을 메시지에 넣지 않음 |
| 작업 경계가 증명되지 않은 PermissionError | 기존 `OUTPUT_WRITE_FAILED` | 없음 | 기존 호환 동작 유지, 입력/출력 원인을 확정하지 않음 |
| 기존 `PipelineError` | 기존 code | 기존 context | calibration 등 기존 구조화 오류를 그대로 유지 |
| 그 밖의 설정·누락·일반 오류 | 기존 code | 기존 context | 이번 작업에서 무관한 원인을 일괄 재분류하지 않음 |

권한 오류는 모두 기존처럼 `retryable=false`다. stage 하나가 입력 읽기와 cache/manifest 쓰기를 함께 수행하므로 stage 이름만으로 입력 오류를 추정하지 않는다. 명시한 두 호출 경계에서만 원래 PermissionError 객체에 내부 진단 표식을 남기고, 실행 경계가 원래 job/stage와 함께 직렬화한다. 따라서 원 예외 타입·traceback과 기존 호출자의 catch 동작을 보존한다. 내부 표식 자체는 API 필드가 아니다.

원래 예외는 내부 진단에 유지하고, 외부 run 응답은 기존 `runs.py`의 재귀적 경로 redaction을 계속 사용한다. 이 변경은 모든 모델·calibration·NAS·디스크 오류를 분류한 것으로 표시하지 않는다. operation이 없는 오류도 새 호출 경계를 추가할 때 개별 재현 테스트로 보강해야 한다.

실패한 stage를 기록하거나 terminal manifest를 읽고 저장하는 중 추가 I/O/검증 오류가 발생하면 원래 실패·취소를 다시 발생시키고 기록 실패를 예외 note로 덧붙인다. 원래 오류 없이 stage 저장 자체가 실패한 경우에는 실패가 그대로 전파된다. 성공 상태를 임의로 만들지 않으며 자동 재시도를 추가하지 않는다.

## 검증 범위

`tests/test_pipeline_error_diagnostics.py`는 합성 pose CSV의 실제 reader 경로에 읽기 실패를 주입하고, 출력 write/replace 권한 실패, 원 예외 보존, 중첩 작업 경계, legacy 코드, 기존 구조화 오류, stage/terminal manifest 추가 장애를 검사한다. GPU 모델 추론은 실행하지 않는다. 실제 MMS 처리 결과 동등성은 P0-3b의 별도 검증 조건이다.
