# 작업 상태

기준: `main@d3d7d9af07518282afcb332df100724c1b04de9d`. P0 및 후속 변경은 각각 draft PR로 분리했다. `refactor/roadinventory-incremental`은 함께 검증하는 통합 브랜치다. PASSED는 아래 명시한 검증 범위에 한정한다.

| ID | 상태 | 구현 및 남은 gate |
|---|---|---|
| A0 | PASSED | 기준선·계약·환경·실패 구분; BASELINE/VALIDATION 참조 |
| P0-1 | IMPLEMENTED_UNVERIFIED | build/API/설치/Gitless gate 통과. 실제 브라우저 로드 버전 확인 남음; PR #4 |
| P0-2 | PASSED | Windows/Linux CPU·frontend·build·실패 전파·package 4 job success; PR #4 |
| P0-3a | PASSED | UNIT_CONTRACT_PASSED: 합성 결과 비교 및 false-pass 방어 20개; PR #4 |
| P0-3b | BLOCKED_VALIDATION | 검토된 golden/XY·Z 허용오차 없음 확인. 실제 MMS 비교 미실행 |
| P0-4 | PASSED | input/output 오류·원래 예외 보존·활성 기능 문서; PR #4 |
| P1-1 | IMPLEMENTED_UNVERIFIED | OS birth/Windows Job/private handshake/복구 lane 차단 및 안내 구현. 실제 Windows/Linux subprocess·회귀 CI 통과. 실제 GPU/native-hang·브라우저 안내 검증 남음; PR #7 |
| P1-2 | BLOCKED_VALIDATION | P0-3b 선행 조건 미충족. 실제 동등성 없이 계산 경로 전환하지 않음 |
| P1-3 | PASSED | UNIT_CONTRACT 및 합성 별도 경로 INTEGRATION: offline evidence·산출물 보관·SQLite 관계/손상/복원·atomic write 22개와 양 OS CI 통과. 실제 운영 백업/서비스 전환 미실행; PR #8 |
| P1-4 | BLOCKED_VALIDATION | 단계 fingerprint/retry의 계산 분리·실제 동등성 선행 조건 미충족 |
| P2-1 | BLOCKED_VALIDATION | 실제 브라우저 연결 없음. 상태 책임/분리 창 변경 전 workflow 기준 검증 필요. 소유권 안내 두 화면만 jsdom 검증 |
| P2-2 | IMPLEMENTED_UNVERIFIED | 기존 최적화 본문 보존·명시 호출·hash/import 계약 통과. 992회 합성 측정의 변동으로 성능 합격 근거 부족. 실제 점 선택/영상/browser gate 남음; PR #6 |
| P2-3 | BLOCKED_VALIDATION | P2-1/실제 데이터 gate 미충족. REVIEW_WORKSPACE_UI_ENABLED=false 유지 |
| P3-1 | IMPLEMENTED_UNVERIFIED | 전체 경로 auth·Origin·private cache·실제 Vite proxy HTTP·양 OS CI 통과. 외부 TLS/Host/proxy·실제 브라우저 Basic 재전송 검증 남음; PR #5 |
| P3-2 | DEFERRED_WITH_REASON | 다중 작업자 요구·부하·승인 근거 없음 |
| R0 | IMPLEMENTED_UNVERIFIED | 통합 production build·180파일 Gitless ZIP·실제 Git bytes·HTTP index/asset/build/bootstrap 검증 통과. 실제 MMS/browser/운영 전환 gate 남음 |

남은 실제 검증을 합성/CPU 통과 수로 대체하지 않는다. 운영 원본·DB·서비스 변경과 새 main 병합은 실행하지 않았다. 재개할 다음 한 작업과 명령은 [CHECKPOINT.md](CHECKPOINT.md)에 둔다.
