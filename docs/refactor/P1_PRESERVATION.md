# P1-3 명시적 산출물 보존과 오프라인 복원

기존 CLI output 경로와 웹 실행별 디렉터리는 바꾸지 않는다. `run_history`의 manifest 보관은
과거 산출물 bytes 보관을 뜻하지 않는다. 같은 CLI output root를 재사용하기 전에 명시적으로
`archive-run`을 실행한 보존본만 별도 파일 bytes를 갖는다. 자동 복사·retention·삭제는 없다.

## 전제와 보장

모든 web/CLI/외부 writer를 중지한 **오프라인 상태**에서만 archive/backup을 사용한다.
`--offline` 및 작업자·관찰 시각·외부 증거 참조를 담은 JSON은 운영자의 확인 기록이며,
프로그램이 모든 writer 종료를 자동 증명하는 장치가 아니다. 운영 백업을 이번 작업에서
실행하지 않았으며 검증은 임시 합성 저장소에서 수행했다.

- registry와 layer DB 각각의 `BEGIN IMMEDIATE` lock을 동시에 유지하고 SQLite backup API로
  WAL에 커밋된 내용까지 복사한다. 본체 파일만 라이브 복사하지 않는다.
- 파일 inventory/hash와 DB 논리 dump hash를 전후 대조한다. 이것만으로 파일 writer까지
  전역 snapshot에 참여한다고 주장하지 않는다. 새 DB·불명확한 DB·active run/upload/scan·
  미해결 프로세스 소유권은 백업을 거부한다.
- registry의 session/task와 feature DB의 revision·geometry/index·provenance·edit/audit·
  outbox 연결을 확인한다. 복구 가능한 pending resolve/reopen intent는 그대로 보존하며
  백업/복원 명령에서 자동 replay하거나 task 상태를 변경하지 않는다.
- 완료 run은 현재 attempt의 manifest, 실제 PointZ/DBF와 CRS sidecar, 모델 manifest,
  요청/effective 설정, 실행별 생성 산출물을 보존한다. 원본 MMS·모델 weights를 따라 복사하지 않는다.
  실패 run은 성공 artifact로 승격하지 않으며 보존 범위는 해당 실패 manifest와 요청 설정이다.
- archive는 명시 `--config`와 `--artifact` 목록 및 role, 상대경로, hash/size, job/attempt,
  버전/input fingerprint를 기록한다. 필수 파일을 빼고 index를 다시 쓴 경우도 거부한다.
- Windows의 no-replace rename, Linux `renameat2(RENAME_NOREPLACE)`로 이미 존재하는
  목적지를 덮어쓰지 않는다. 미지원 플랫폼은 거부한다. source/destination은 서로 분리해야 한다.
  경로의 symlink/junction과 상위 경로도 거부하며, 삭제 정리는 이 호출이 만든 staging에 한정한다.
- 공용 atomic byte writer는 고유 임시 파일, flush/fsync, replace를 사용한다. 동시 writer의
  read-modify-write 직렬화는 기존 DB transaction/lock/CAS 책임이다. 정리 실패가 원래 오류를 덮지 않는다.

## 명령

`scripts/preserve_mms_state.py`는 `archive-run`, `backup-state`, `restore`, `verify`를 제공한다.
아래는 운영 적용 예시이며 이번 세션에서 실제 운영 경로에 실행하지 않았다. `--config`는
source 내부에 준비한 설정 snapshot의 상대경로이고 반복 가능하다. 비밀값이 포함될 수 있는
보존본은 공개 Git/일반 공유 파일로 게시하지 않는다.

```text
python scripts/preserve_mms_state.py archive-run --source RUN_OUTPUT --destination NEW_ARCHIVE --source-alias reviewed-run --config logs/effective_config.json --artifact txt/frame.txt --offline --offline-evidence OFFLINE_EVIDENCE.json
python scripts/preserve_mms_state.py backup-state --source OFFLINE_STATE --destination NEW_BACKUP --source-alias reviewed-state --config deployment.json --offline --offline-evidence OFFLINE_EVIDENCE.json
python scripts/preserve_mms_state.py verify --source NEW_BACKUP
python scripts/preserve_mms_state.py restore --source NEW_BACKUP --destination NEW_RESTORE_DIRECTORY
```

증거 JSON은 `schema_version: 1`, `mode: "offline-stopped-writers"`, `operator`,
`evidence_reference`, timezone 포함 `observed_at`, `stopped_writers: ["web", "cli", "external"]`를
요구한다. 빈 값이나 placeholder를 실제 점검 기록으로 사용하지 않는다.

성공 exit 0/PASS, 불완전·변경·손상 exit 1/FAIL, 오프라인/파일/lock 전제 부족 exit 2/
BLOCKED_VALIDATION이다. CLI의 실패 요약에는 원본의 절대경로나 DB 내용이 포함되지 않는다.

복원은 새로운 분리 디렉터리만 만든다. 원본 참조와 root 식별자를 다른 실제 경로에 연결하는
배포 설정은 자동 변경하지 않는다. 운영 서버에 연결하거나 정상 outbox reconciliation을 재개하기
전에 동일 source 참조·신뢰 경계·버전으로 검사해야 한다. live swap/서비스 재시작은 별도 작업이다.

## 검증

Windows/Python 3.12.10에서 초기 21개 preservation 테스트 및 전체 Python **581 passed,
7 OS/권한 skips**. 이 결과는 합성 SQLite/PointZ/한글 fixture의 별도 경로 복원 증거이며
실제 운영 백업이나 MMS 정확도 증거가 아니다. 후속 additive ownership guard도 별도로 검사한다.
디스크 부족·publish/replace 실패·동시 temp write·기존 destination 경합·source 변경·
필수 SHP/config/model 누락·registry/feature 불일치·pending outbox·legacy additive migration
반복·cleanup 원래 예외 보존을 포함한다. hosted Windows/Linux 최종 결과는 PR/체크포인트에 기록한다.

```text
python -m pytest -q tests/test_preservation.py
```

원래 output 경로·실제 DB schema를 이 명령이 변환하지 않으므로 도구의 코드 revert가 가능하다.
이미 만든 검증된 보존본은 유지하며 코드 rollback을 이유로 삭제하지 않는다.
