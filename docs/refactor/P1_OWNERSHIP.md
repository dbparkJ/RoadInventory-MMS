# P1-1 실행 소유권·재시작 경계

기준 `bc5cef6`의 RunManager/CAS/manifest 결과 검증을 유지하고, OS 소유권과 private control
채널을 `infrastructure/process_owner.py`의 얇은 adapter로 분리했다. 계산 알고리즘·모델·설정과
CLI 출력 경로는 변경하지 않는다. CLI 진입점은 웹 소유 실행인 경우에만 아래 handshake를 수행한다.

## 시작·관찰·취소

- DB의 starting CAS에서 run/attempt/임의 launcher UUID를 기록한 뒤 child를 시작한다.
  child는 pipeline/model import 전에 자신의 실제 OS birth identity와 job identity를 확인하고
  receipt를 기록한다. 부모의 private stdin `start:<launcher>` 승인 전에는 계산을 시작하지 않는다.
- spawn 전 실패가 증명되면 exit 기록을 남긴다. spawn 여부가 불확실하거나 receipt가 없으면
  임의 재시작하지 않는다. 이전 버전의 PID만 있는 실행도 ownership unknown으로 다룬다.
- Windows는 생성 시각과 signalled process handle로 생존을 확인한다. venv redirector의
  실제 Python child는 OS ancestry로 확인하고 retained Job Object에 넣은 후 승인한다.
  kill-on-close와 retained Job handle로 하위 프로세스를 정리하며 taskkill/PID 재검색으로 종료하지 않는다.
- Linux는 boot ID, procfs start tick, process group을 확인한다. 취소/부모 종료 EOF는 원래
  child에게만 연결된 stdin 채널로 전달한다. 살아 있는 group leader가 자기 group을 종료한다.
  재시작된 서버는 PID나 PGID를 근거로 다른 프로세스를 kill하지 않는다.
- 실행 중 5초 간격으로 birth identity를 재확인하고 `last_observed_alive_at`을 DB에 기록한다.
  이는 OS 생존 관찰이며 모델 진행률이나 child 계산 heartbeat를 의미하지 않는다.
- Python/CUDA native 작업의 중단 응답 시간을 보장하지 않는다. 취소를 확인할 수 없으면
  ownership block을 유지한다. 실제 GPU 장시간·native hang 검증은 별도 gate이다.

## 재시작과 durable success

살아 있거나 재사용됐거나 확인 불가인 identity는 `ownership_blocked=1`로 기록한다.
이 플래그는 상태가 interrupted/failed/completed가 되더라도 queue claim을 막는다.
현재 child가 쓸 수 있는 manifest를 서버가 실패 상태로 덮어쓰지 않는다. 읽기/검수 API는 유지한다.

child 및 필요한 group/job 종료가 확인되면 그 exit 증거를 영속화하고 block을 해제한 뒤
기존 manifest/result 검증으로 상태를 조정한다. exit 증거를 남겨 나중의 PID 재사용이 이전 run을
다시 살아 있다고 판정하지 않게 한다. 완전한 현재 attempt의 durable success는 늦은 취소나
부모의 오류보다 우선한다. exit code 0만으로 completed로 바꾸지 않는다.

`--no-run-worker`는 그대로이며 새 GPU 작업을 자동 실행하지 않는다. 알 수 없는 legacy owner를
자동 정리하거나 block을 지우는 관리 endpoint는 추가하지 않았다. 운영자는 서비스·CLI·외부
writer 상태를 별도로 확인해야 하며, 이 개발 작업에서 운영 PID를 조회해 종료하지 않았다.

공개 응답의 선택 필드 `execution.requires_inspection` 및 기존 error 문구로 점검 필요를 알린다.
실행 큐와 알림 화면에서도 이 필드를 표시하므로 durable 결과가 completed여도 새 실행의
대기 이유를 볼 수 있다. 결과 열기와 완료 상태는 유지한다. 실제 브라우저 UI 확인은 미완료다.
내부 PID/UUID/receipt 절대경로를 새 API 필드로 노출하지 않는다.

## 저장소 이행·검증·제한

registry에 nullable `ownership_json`, 기본 0의 `ownership_blocked` 두 컬럼을 반복 실행 가능한
additive migration으로 추가한다. 기존 행·provenance·attempt는 삭제하지 않는다. 동일 state의
ASGI 단일 worker/OS lock을 유지하며, 전역 다중 host exactly-once를 주장하지 않는다.

Windows/Python 3.12.10에서 전체 Python **567 passed, 7 skips**. 별도 수정 전후 결과는
체크포인트/PR에 기록한다. 새로운 실제 OS 프로세스 테스트는 다음을 확인한다.

- OS와 일치하는 child receipt, private cancel, 승인 전 계산 금지와 EOF 종료.
- 이 테스트가 만든 child/grandchild의 취소 및 부모만 종료한 경우의 정리.
- PID 재사용/조회 불가에서 종료 권한을 얻지 않음.
- 살아 있는 owner를 복구한 동안 queue를 반복 claim해도 시작 불가, exit 관찰 후 재개.
- 다른 run의 exit 기록 불신, 정상 manifest reconciliation, cancellation/success 경합,
  spawn/shutdown 경합, no-worker/worker lock 및 기존 결과 검증.

기존 subprocess double 기반 manifest/CAS 테스트는 명시적인 simulated owner를 사용한다.
커널 소유권 테스트는 실제 별도 프로세스를 사용한다. 이는 실제 MMS/GPU 처리 결과 비교가 아니다.
Linux hosted CI와 최종 통합 결과는 PR/체크포인트에 기록하며 실제 GPU/native-hang 복구 승인은
`BLOCKED_VALIDATION`으로 남긴다.

```text
python -m pytest -q tests/test_process_ownership.py tests/test_webapp_run_safety.py tests/test_execution_architecture.py
```

관련 OS 근거: [Windows process wait](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject).
실제 보장은 위 subprocess 테스트로 확인한다. 이전 코드로 rollback하면 새 소유권 차단을 읽지
못하므로 모든 실행이 종료됐음을 확인한 maintenance 상태에서 검증된 이전 release를 사용한다.
새 컬럼을 제거하거나 운영 DB를 초기화하지 않는다.
