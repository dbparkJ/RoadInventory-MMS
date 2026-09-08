# 보호 계약과 호출 경계

권위 설명은 [current_architecture.md](../current_architecture.md)의 실제 실행/저장 경계와 [실행 명세](../ROADINVENTORY_MMS_INCREMENTAL_REFACTOR_AI_AGENT_SPEC_KO.md) §1이다. 이 파일은 이번 변경을 검토할 때 필요한 테스트 연결을 보완한다.

| 경계 | 호출/쓰기 위치 | 보존 계약 | 보호 테스트 |
|---|---|---|---|
| 입력/설정 | scripts/run_pipeline.py → config → pipeline.run_pipeline | YAML/CLI 기본값·override·snapshot, Job/Track/frame 선택 | test_config.py, test_pipeline_helpers.py |
| calibration | scan_image_tasks → attach_calibration_metadata / CalibrationResolver | 누락/모호/schema/CRS를 임의 기본값으로 우회하지 않음 | test_calibration_resolver.py |
| 계산 | pipeline의 기존 detect/project/estimate stage, pointcloud/pole | 권위 Float64 좌표·축·단위·수직 기준, 지면 접점 정책, 실패 좌표 대체 금지 | test_core.py, test_pole_accuracy_regressions.py, test_pointcloud.py |
| 원본 record | object_crops/source_inventory/source_records | source identity·index·원본 LAS 속성 보존, QA RGB crop과 구분 | test_object_crop_contracts.py, test_object_crop_source_inventory.py |
| 산출물 | frame TXT JSON / staged SHP publish / manifest_writer | PointZ, DBF 필드/폭/정밀도/인코딩, CRS sidecars, publish rollback, 현재 attempt 완전성 | test_core.py, test_execution_architecture.py, test_shp_dedupe.py |
| 웹 실행 | app → RunManager → 별도 CLI child | 단일 worker, cancel/restart 경계, exit code만으로 성공 금지 | test_webapp_run_safety.py, test_execution_architecture.py |
| 편집 | overlays/store/review_edits → SQLite + task_resolution_outbox | revision CAS, 멱등 mutation, provenance, undo/redo, outbox 복구 | test_review_edits.py, test_webapp_overlays.py, test_webapp_review_tasks.py |
| preview | media/panorama_fastpath / acceleratedPointRaycast | 캐시·orientation·선택 동등성, viewer 좌표를 권위 좌표로 사용하지 않음 | test_panorama_fastpath.py, test_webapp_media.py, acceleratedPointRaycast.test.ts |
| UI | App / OverlayContext / ManualObjectContext / ReviewContext / DetachablePanel | 분리 창, N/B→B/M/Esc, 입력 focus, 제안→명시 저장, 늦은 응답 차단 | Workspace/DetachablePanel/OverlayContext/ManualObjectContext/ReviewQueue tests |
| 배포 | setup_web → frontend build → app static_dir | 같은 source/backend/frontend, legacy 이행 상태 공개, 실패 build를 성공으로 재사용 금지 | P0-1 추가 검증 |

여러 SQLite 파일을 하나의 원자적 transaction으로 설명하지 않는다. review workspace UI는 false를 유지한다. P0 오류 진단 변경은 실제 I/O operation 표시에 한정하며 계산·좌표·추론 설정은 바꾸지 않는다.

합성/단위 테스트는 실제 MMS 정확도의 증거가 아니다. 계산 호출 경로를 바꾸는 P1-2는 P0-3b 실제 fixture 및 검토된 profile 비교를 통과해야 한다.
