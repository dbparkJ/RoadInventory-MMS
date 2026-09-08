# 3D 점군 표시 지연 수정

2026-09-08. 기준 `0059296`, 수정 branch `refactor/pointcloud-loading-performance`.

## 원인

사용자 보고는 3D 점군 화면이다. P0 이후만 비교하면 점군 생성기는 그대로지만, 작업 전 `aac6a30`부터 비교하면 사전 브랜치 통합의 `bc14a81`에서 catalog v5→v6 및 include_scope signature가 추가됐다. 이전 catalog와 파생 MMSP가 무효화되므로 최초 인덱싱과 프레임별 미리보기 생성이 필요했다. 오늘 실제 로컬 로그에는 7개 LAS 재인덱싱 **50.6초**가 기록됐다. 검증된 v6 catalog의 재확인은 **200.84ms**, 기존 JSON bytes 불변이었다. 잘못된 캐시를 재사용하도록 버전/원본 검증을 완화하지 않았다.

원본 속성 보존 기능으로 전체 decoded record가 점당 50→80 bytes로 늘었는데, 화면용 RGB 읽기도 이 전체 배열을 만들었다. 표시에는 네 필드만 필요하다. 큰 점군에서는 동일 512MiB LRU에 유지할 수 있는 블록 수가 줄어든다. sampler는 점수 배분과 추출을 위해 같은 거리/선택 마스크도 두 번 계산했다.

화면에는 추가 지연 요인이 있었다. 응답 헤더 직후 취소 listener/timeout을 정리해 이전 프레임의 3.75~15MB body가 계속 수신됐고, 닫힌 viewer의 응답도 취소 확인 전에 파싱했다. 인덱싱은 8회 재시도, 총 41.2초 이후 포기하므로 실제 50.6초 재구축을 기다리지 못할 수 있었다.

## 변경

- `read_preview_block()`은 XYZ Float64/RGB UInt8/intensity UInt16/classification Int16만 만든다(31 bytes/point). 전체 기록 API와 학습용 원본 속성은 유지한다. full/preview는 기존 source handle·lock·version 검사·동시 요청 병합과 512MiB/64개 LRU 상한을 공유한다. 이미 있는 전체 기록도 재사용한다.
- sampler는 첫 계산의 정확한 band indices를 요청당 최대 **32MiB**만 보관한다. 상한을 넘으면 기존 재계산으로 돌아간다. 거리 조건, 순서, quota, linspace, 색 변환, MMSP v1, 예산 25만/50만/100만과 캐시 키는 유지한다.
- 3D 요청은 body 수신까지 취소/timeout을 유지하고 닫힌 viewer는 파싱하지 않는다. 인덱싱은 2초 간격으로 확인하되 요청·다운로드·재시도 전체를 120초로 제한한다. 프레임 전환과 HTTP 재시도 대기도 즉시 취소한다.

## 측정과 검증 범위

Windows/Python 3.12.10, 동일 실제 100만점 RGB 프레임. 기준 commit의 reader/media를 별도 모듈로 로드하고 변경 전후 순서를 번갈아 4회씩 측정했다. cold는 reader/decoded cache를 새로 만드는 것이며 OS cache는 비우지 않았다. warm은 같은 reader에서 다시 생성하는 것이며 디스크의 완성 MMSP를 그대로 읽는 cache hit와 구분한다. 원본은 읽기만 했고 출력은 메모리에서 비교했다.

| 항목 | 변경 전 중앙값 | 변경 후 중앙값 | 관측 변화 |
|---|---:|---:|---:|
| 첫 생성(cold reader) | 1,474.65ms | 988.54ms | 약 33% 감소 |
| 같은 reader 재생성 | 691.69ms | 508.18ms | 약 27% 감소 |
| 유지된 decoded arrays | 277.27MiB | 107.44MiB | 약 61% 감소 |

메모리 수치는 decoded LRU 배열 합계이며 프로세스 전체/peak RAM이 아니다. 요청의 indices는 별도로 최대 32MiB다. 공유 개발 호스트에서 관측한 단일 프레임 수치이며 전체 현장/브라우저 성능 보증은 아니다. 16개 모든 결과가 기존 파생 파일의 **15,000,040 bytes와 정확히 동일**했고 LAS size/mtime도 불변이었다. 원시 수치는 [POINT_PREVIEW_PERFORMANCE.json](POINT_PREVIEW_PERFORMANCE.json), 로컬 측정기는 `.cache/refactor-validation/point_preview_compare.py`에 있다.

집중 검증에서 실제 LAS PF0/3/7·PCDB 배열 일치, 원본 속성 보존, mixed LRU/버전/동시성/오류 후 재시도, 4색상×3focus 출력 bytes, indices 상한/fallback 및 마스크 재계산 감소를 확인했다. frontend는 delayed body 취소/timeout, late parse, 50.6초 인덱싱 성공, 120초 deadline 및 재시도 취소를 재현했다. 전체 회귀·최종 빌드·CI 결과는 아래에 이어 기록한다.

실제 MMS 추론/SHP 정확도 golden 검증과 실제 브라우저/WebGL 검증은 미완료다. 브라우저 연결 discovery는 `[]`였다. 기존 선택 지주 조회 대기와 scene 재생성 구조는 이번 변경 범위에 포함하지 않았다. 원본 데이터/운영 DB·캐시 삭제나 서버 재시작은 실행하지 않았다. 새 source와 일치하는 dist를 빌드한 뒤 사용자가 서버를 다시 시작해야 적용된다.
