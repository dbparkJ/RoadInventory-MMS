# 작업 상태

기준: `d3d7d9af07518282afcb332df100724c1b04de9d`. 이번 브랜치는 P0 신뢰성 개선의 검증 가능한 변경 단위다. 후속 구조 변경을 하나의 PR에 섞지 않는다.

| ID | 선행 조건 | 상태 | 범위/다음 gate |
|---|---|---|---|
| A0 | 없음 | PASSED | 기준선·계약·환경·실패 구분 완료, BASELINE/VALIDATION 참조 |
| P0-1 | A0 | IMPLEMENTED_UNVERIFIED | build/API/설치/Gitless gate 통과, 실제 브라우저 연결 부재로 browser gate 남음; 9c9b4c4/406862c |
| P0-2 | A0 | PASSED | Windows/Linux CPU·frontend·build·실패 전파·package 4 job 모두 success, run 34173471638; 4093ea0/b1eaa64/392c343 |
| P0-3a | A0 | PASSED | UNIT_CONTRACT_PASSED: 합성 false-pass 방어 20개; 371776b |
| P0-3b | 실제 fixture/profile | BLOCKED_VALIDATION | 사용자가 검토된 golden/허용오차 없음 확인. GPU/모델 부재가 아님 |
| P0-4 | A0 | PASSED | UNIT_CONTRACT_PASSED: input/output 오류·원래 실패 보존 및 활성 기능 문서; 4529c29 |
| P1-1 | P0-2 | TODO | child ownership/restart OS 검증이 필요한 별도 변경 |
| P1-2 | P0-2/P0-3b | BLOCKED_VALIDATION | 실제 결과 동등성 없이 계산 경로 변경하지 않음 |
| P1-3 | P0-2 | TODO | 과거 결과 bytes 보관 및 DB/outbox 복원 rehearsal 별도 변경 |
| P1-4 | P1-1/P1-2 | TODO | 선행 ownership/checkpoint 경계 확립 후 제한적 retry |
| P2-1 | P0-2/P0-4 | TODO | 상태/분리 창 refactor는 실제 브라우저 workflow gate 필요 |
| P2-2 | P0-1/P0-2 | TODO | 명시 preview 호출과 cold/warm/선택 동등성 검증 |
| P2-3 | P2-1/실제 데이터 | BLOCKED_VALIDATION | REVIEW_WORKSPACE_UI_ENABLED=false 유지 |
| P3-1 | P0-2 | TODO | 단일 운영 보안/actor 경계 별도 검증 |
| P3-2 | P1/P2 및 운영 요구 | DEFERRED_WITH_REASON | 다중 작업자 요구·부하·승인 근거 없음 |
| R0 | 적용 기능별 gate | IMPLEMENTED_UNVERIFIED | 개발 package/API/static smoke 통과. 브라우저/실제 MMS gate 및 운영 전환은 미완료·미실행 |
