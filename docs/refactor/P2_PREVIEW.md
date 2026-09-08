# P2-2 파노라마 preview 명시 경계

상태: 명시 import 전환과 자동 회귀 검증 완료. 실제 MMS, Browser 방향·시각 확인 및 운영 성능 승인은 `BLOCKED_VALIDATION`이다. 아래 합성 CPU 측정은 진단 자료이며 성능 개선 또는 운영 합격을 뜻하지 않는다.

## 변경과 보존 계약

기준 커밋은 `bc5cef65267bc0fe86234404c90ab929599e24c8`이다. 기존 패키지 초기화는 `install_panorama_fastpath()`로 `media._resize_panorama`를 덮어썼다. `media`를 reload하면 기존의 느린 함수 본문이 다시 활성화됐고, installer를 호출하면 사용자의 patch를 덮어썼다. 새 회귀 테스트 두 개로 변경 전 실패를 확인했다.

`media.py`가 `resize_panorama_fast`를 `_resize_panorama`라는 이름으로 직접 import한다. 패키지 초기화의 installer 호출과 중복된 이전 함수 본문을 제거했다. 기존 `webapp.install_panorama_fastpath()` 및 `panorama_fastpath.install_panorama_fastpath()`는 인자 없이 호출 가능한 no-op으로 유지한다. 라우트는 계속 `media._resize_panorama`를 조회하므로 기존 테스트·확장 patch 지점도 유지된다. 새로운 생성기나 설정 계층은 추가하지 않았다.

최적화된 `resize_panorama_fast` 본문은 변경하지 않았다. 기준·후보에서 해당 함수 소스 SHA-256은 `a58c2b37a5b7f8574e122b0189948fc81397951397b4151ebc9fcd55b3349911`로 동일하다. JPEG decoder draft, EXIF 적용 순서와 방향, 크기 반올림, Lanczos/reducing_gap, RGB/RGBA 변환, WebP/JPEG 옵션·fallback, 임시 파일 원자적 게시·정리 및 응답 media type을 유지한다.

`panorama-v3` fingerprint, source 경로·크기·mtime·frame ID·요청 width 입력, 기존 WebP/JPEG 파생 파일 재사용, ETag/304, 요청별 lock, semaphore, 취소된 요청의 owner 보존을 변경하지 않았다. 점군 캐시 버전·예산·focus·quota와 point picking 알고리즘·threshold·정렬·overlap cycling도 변경하지 않았다. 알고리즘 변경이 없으므로 캐시 버전을 올리거나 캐시를 삭제하지 않는다.

## 자동 검증

- 변경 전 panorama/media/health 및 신규 이미지 계약: 46 passed, 1 skipped. 별도 신규 import 회귀: 예상대로 2 failed(reload, installer patch 보존).
- 변경 후 같은 범위와 import 회귀: 48 passed, 1 skipped, 13.36초. Windows 환경의 기존 symlink 권한 skip 1개이며 이미지·import 테스트 skip은 없다. 빌드를 의도적으로 갱신하지 않은 worktree의 개발 모드 provenance 경고가 포함된다.
- 실제 Pillow JPEG 출력 bytes 비교와 EXIF 1–8 사분면 색상·크기 확인, 실제 WebP/RGBA 출력 비교, encoder/publish 실패의 원래 예외·임시 파일 정리, fingerprint invalidation을 검증했다.
- package/media/fastpath/app 네 가지 최초 import 순서를 각각 독립 Python 프로세스에서 확인하고, `media` reload 후에도 같은 생성기를 사용하는지 확인했다. 기존 installer 호출은 patch를 보존한다.
- 기존 media 라우트 테스트의 디스크 캐시 재시작 재사용·304 및 취소된 요청 owner 정리 검증을 재실행했다.
- 기존 frontend 5파일 103테스트 통과(6.40초): `acceleratedPointRaycast`, `PointCloudView`, `PanoramaView`, `panoramaProjection`, `panoramaNavigation`. source는 이 작업에서 수정하지 않았다. Vitest/jsdom 검증이며 실제 Browser 실행이 아니다.

기존 accelerated raycast differential 테스트는 대형/소형 점군, XY 경계, position version 재생성, 변환·draw range·threshold를 바꾼 40개 ray의 native hit index를 비교한다. PointCloudView 테스트는 pointer 기준 선택, overlap 중복 제거·cycling, 실제 vertex reprojection, 범위 밖 후보 및 no-hit를 확인한다. indexed/morph fallback, 완전히 같은 순위 tie 및 cold/warm 선택 순서에 대한 완전한 differential matrix는 기존 테스트만으로 입증되지 않는다. 이 제한과 실제 Browser 선택 결과 확인을 후속 검증 항목으로 남긴다.

재현 명령(프로젝트 root):

```powershell
python -m pytest -q tests/test_panorama_fastpath.py tests/test_panorama_preview_contract.py tests/test_webapp_media.py tests/test_webapp_health_safety.py --tb=short
cd webui
node node_modules/vitest/vitest.mjs run src/lib/acceleratedPointRaycast.test.ts src/views/PointCloudView.test.ts src/views/PanoramaView.test.tsx src/lib/panoramaProjection.test.ts src/lib/panoramaNavigation.test.ts
```

## 합성 CPU 측정

원시 samples, 입력·출력 hash, 환경 및 사전 고정한 진단 상한은 [P2_PREVIEW_BENCHMARK.json](P2_PREVIEW_BENCHMARK.json)에 보관한다. `scripts/benchmark_panorama_preview.py`로 source 변경 전에 기준 A/B, 변경 후 후보 A/B를 순서대로 실행했다. 각 실행은 case당 cold 31회와 warm 31회, 총 992회의 호출을 기록했다.

환경: Windows 11 `10.0.26200`, Python 3.12.10, Pillow 12.3.0, NumPy 2.5.1, WebP encoder 사용 가능. 공유 개발 호스트의 다른 작업 부하는 통제하지 않았다. 입력은 결정적 RGB gradient JPEG 4096×2048, quality 90, EXIF 1 또는 6이고 요청 width는 1024다. EXIF 1 출력은 1024×512, EXIF 6 출력은 1024×2048이다. JPEG fallback과 실제 WebP를 각각 측정했다.

case당 codec warmup 1회 후 `perf_counter_ns`로 측정했다. cold는 매번 새로운 출력 경로를 사용하는 **파생 캐시 miss**이며 OS page cache는 비우지 않았다. warm은 바로 직전 출력 경로를 재사용한다. 생성기의 decode/resize/encode/파일 작업을 포함하며 fixture 생성, import, HTTP, browser rendering, semaphore 대기 및 GPU는 제외한다. 백분위는 NumPy linear percentile이다. 표의 모든 값은 ms이며 `p50 / p95` 순서다.

| 인코더·EXIF | 실행 | cold p50 / p95 | warm p50 / p95 |
|---|---|---:|---:|
| JPEG·1 | 기준 A | 24.323 / 27.775 | 0.416 / 0.553 |
| JPEG·1 | 기준 B | 25.910 / 31.581 | 0.410 / 0.490 |
| JPEG·1 | 후보 A | 27.211 / 32.651 | 0.491 / 0.692 |
| JPEG·1 | 후보 B | 27.116 / 30.436 | 0.399 / 0.604 |
| JPEG·6 | 기준 A | 59.579 / 74.683 | 0.444 / 0.668 |
| JPEG·6 | 기준 B | 81.964 / 97.722 | 0.568 / 0.776 |
| JPEG·6 | 후보 A | 68.382 / 77.616 | 0.532 / 0.904 |
| JPEG·6 | 후보 B | 62.753 / 85.007 | 0.501 / 0.677 |
| WebP·1 | 기준 A | 46.290 / 60.499 | 0.414 / 0.660 |
| WebP·1 | 기준 B | 57.974 / 62.503 | 0.614 / 0.874 |
| WebP·1 | 후보 A | 55.740 / 64.611 | 0.554 / 0.800 |
| WebP·1 | 후보 B | 45.081 / 48.234 | 0.416 / 0.615 |
| WebP·6 | 기준 A | 169.577 / 193.781 | 0.532 / 0.790 |
| WebP·6 | 기준 B | 177.639 / 202.457 | 0.566 / 0.819 |
| WebP·6 | 후보 A | 171.317 / 204.512 | 0.610 / 1.036 |
| WebP·6 | 후보 B | 134.501 / 143.723 | 0.449 / 0.756 |

후보 측정 전에 각 case/cache/백분위의 진단 상한을 `max(기준 A, 기준 B) + abs(기준 A - 기준 B)`로 고정했다. 후보 A는 16항목 중 6항목을 초과했다. warm JPEG·1 p50/p95, warm JPEG·6 p95, cold WebP·1 p95, warm WebP·6 p50/p95다. 후보 B는 16항목 모두 이내였다. 상한을 넓히거나 합격할 때까지 다시 측정하지 않았다. 따라서 이 측정으로 성능 유지 합격을 선언하지 않으며 성능 검증 상태는 `BLOCKED_VALIDATION`으로 유지한다.

모든 반복·기준·후보에서 case별 source 및 output SHA-256이 동일하다. 함수 소스와 출력 bytes 동일성은 구현 보존 근거이며, 공유 호스트에서 관측한 시간 변동의 원인을 확정하거나 운영 성능을 대신 입증하지 않는다.

```powershell
python scripts/benchmark_panorama_preview.py --label baseline-a --samples 31 --output .cache/p2-preview/new-baseline-a.json
# 같은 조건으로 기준 B 실행 후 상한을 고정하고, 후보 checkout에서 후보 A/B를 실행한다.
```

실제 MMS golden 및 승인된 비교 자료가 없고 Browser 연결도 없으므로, 실제 equirectangular 영상의 시각 방향·정렬, 실제 점 선택 결과, browser cold/warm 지연·메모리·취소 UX와 운영 rollout은 미검증이다. 후속 검증에서는 동일 실제 입력과 승인된 환경·허용 범위를 사용해야 한다. 이 작업은 dist를 재빌드하거나 운영 배포하지 않았다.
