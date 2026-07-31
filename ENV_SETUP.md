# MMS 실행환경 자동 구성

`scripts/setup.ps1`과 `scripts/setup.sh`는 현재 터미널에서 64-bit CPython 3.12를 자동으로 찾은 뒤 `scripts/bootstrap_environment.py`를 실행합니다. bootstrap은 Windows와 Linux에서 다음 작업을 비대화형으로 수행합니다.

1. x86-64 Windows/Linux와 64-bit CPython 3.12 자동 탐지
2. `nvidia-smi`를 통한 NVIDIA GPU, driver, compute capability, driver 지원 CUDA 버전 확인
3. 프로젝트의 `.venv` 생성 또는 기존 환경 재사용
4. driver 범위에 맞는 공식 PyTorch wheel과 `requirements.txt`의 고정 버전 설치
5. `pip check` 실행
6. `scripts/verify_environment.py`를 통한 torch/torchvision/torchaudio 버전, 선택 CUDA runtime, CUDA NMS와 행렬곱 smoke test

bootstrap은 `nvidia-smi`가 보고한 driver 지원 최대 CUDA를 기준으로 `cu128`, `cu126`, `cu118` 중 가장 높은 호환 wheel을 선택합니다. PyTorch wheel은 필요한 CUDA runtime을 자체 포함하므로 일반 설치에서는 시스템 CUDA Toolkit이나 `nvcc` 설치가 필수가 아닙니다. `nvidia-smi`의 `CUDA Version`은 설치된 Toolkit 버전이 아니라 현재 driver가 지원하는 최대 CUDA 버전입니다.

setup launcher는 Python이나 운영체제 패키지를 자동 설치하지 않습니다. 설치 여부를 임의로 바꾸거나 관리자 권한을 요구하는 대신, 찾을 수 있는 Python 명령을 검사하고 필요한 조치를 설명하는 오류로 종료합니다.

## 웹 작업실까지 자동 구성

웹 API용 Python 패키지는 `requirements.txt`에 고정되어 있습니다. 다음 launcher는 기존
Python/CUDA setup을 먼저 실행하고, Node.js/npm을 찾을 수 있으면 `npm ci`와 배포용
UI build까지 비대화형으로 이어서 실행합니다.

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_web.ps1
```

Linux:

```bash
bash scripts/setup_web.sh
```

저장소에 빌드된 `webui/dist`가 포함된 운영 배포본은 웹 서버 실행에 Node.js가
필요하지 않습니다. 소스 UI를 다시 빌드해야 하는 개발 환경에서만 Node.js LTS와 npm이
필요합니다. launcher는 관리자 권한으로 OS 프로그램을 임의 설치하지 않으며, npm과
빌드 산출물이 모두 없을 때 필요한 조치를 설명하고 종료합니다.

설치 예정 명령만 확인하려면 다음처럼 실행합니다. 이 경우 npm 패키지도 설치하지
않습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_web.ps1 --dry-run
```

```bash
bash scripts/setup_web.sh --dry-run
```

## Windows

PowerShell에서 프로젝트 루트로 이동한 뒤 실행합니다.

```powershell
Set-Location D:\mms_project
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

launcher는 `py -3.12`, `python3.12`, `python`, `python3` 순으로 64-bit CPython 3.12를 찾습니다. bootstrap은 활성화된 환경이나 전역 `pip`를 사용하지 않고 항상 `.venv\Scripts\python.exe -m pip`를 호출합니다.

## Linux

```bash
cd /path/to/mms_project
bash scripts/setup.sh
```

launcher는 `python3.12`, `python3`, `python` 순으로 64-bit CPython 3.12를 찾습니다. 배포판에서 `venv` 모듈을 별도 패키지로 제공한다면 먼저 Python 3.12용 venv 패키지가 설치돼 있어야 합니다. 예를 들어 Ubuntu 계열에서는 관리자에게 `python3.12-venv` 설치를 요청합니다.

launcher 없이 bootstrap을 직접 실행해도 됩니다. 이 경우 호출한 interpreter가 3.12가 아니더라도 bootstrap이 터미널에서 올바른 3.12 interpreter를 다시 검색합니다.

```powershell
py .\scripts\bootstrap_environment.py
```

```bash
python3 scripts/bootstrap_environment.py
```

## 사전 확인만 하기

아래 명령은 OS/Python/GPU/driver를 탐지하고 실행 예정 명령을 출력하지만 `.venv`나 패키지를 변경하지 않습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 --dry-run
```

```bash
bash scripts/setup.sh --dry-run
```

## CUDA runtime 자동 선택

framework 기본 버전은 `torch 2.7.1`, `torchvision 0.22.1`, `torchaudio 2.7.1`로 고정됩니다. wheel runtime은 다음 기준으로 자동 선택됩니다.

| driver 지원 CUDA | 선택 runtime |
|---|---|
| 12.8 이상 | `cu128` |
| 12.6 이상, 12.8 미만 | `cu126` |
| 11.8 이상, 12.6 미만 | `cu118` |

로컬 `nvcc`가 있으면 버전을 출력하지만 wheel 선택에는 사용하지 않습니다.

## CPU fallback

GPU가 없거나 driver가 지원하는 CUDA가 11.8보다 낮으면 기본 실행은 즉시 실패합니다. CPU 실행을 의도한 경우에만 `--allow-cpu`를 명시합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 --allow-cpu
```

```bash
bash scripts/setup.sh --allow-cpu
```

이 옵션은 자동 선택에 호환 CUDA 장비가 없을 때 공식 CPU wheel을 설치합니다. CUDA 장비 탐지 여부와 관계없이 CPU wheel을 선택하려면 `--torch-runtime cpu`를 명시합니다.

## 재실행

동일 명령을 다시 실행해도 기존 `.venv`를 삭제하거나 덮어 만들지 않습니다. 현재 driver에 맞는 PyTorch wheel과 고정 requirements를 다시 적용한 뒤 dependency 및 CUDA 검사를 반복합니다. driver 변경으로 자동 선택 결과가 달라지면 해당 PyTorch wheel도 교체됩니다.

기존 `.venv`가 Python 3.12가 아니거나 중간에 손상돼 interpreter가 없다면 스크립트는 해당 디렉터리를 자동 삭제하지 않고 중단합니다. 필요한 파일을 백업하고 `.venv`를 직접 다른 이름으로 이동한 뒤 다시 실행하십시오.

## 선택 옵션

특정 Python이나 `nvidia-smi`를 지정할 수 있습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 `
  --python C:\Python312\python.exe `
  --nvidia-smi C:\Windows\System32\nvidia-smi.exe
```

자동 선택 대신 특정 공식 wheel runtime을 고정할 수 있습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 --torch-runtime cu126
```

지원 값은 `auto`, `cu128`, `cu126`, `cu118`, `cpu`입니다. 명시한 CUDA runtime이 driver 지원 범위보다 높으면 설치 전에 실패하며 자동으로 하위 버전을 선택하지 않습니다.

다른 프로젝트 사본이나 가상환경 경로를 검사할 때:

```bash
python3 scripts/bootstrap_environment.py \
  --project-root /srv/mms_project \
  --venv-dir .venv
```

`--venv-dir`, `--requirements`, `--verify-script`의 상대경로는 모두 `--project-root`를 기준으로 해석합니다.

## 정상 완료 기준

마지막에 다음 값이 출력돼야 합니다.

```text
environment_check=OK
[bootstrap] environment_ready=.../.venv
```

예를 들어 `cu128`이 선택된 GPU 환경에서는 다음 핵심값을 확인합니다. driver에 따라 `cu126` 또는 `cu118`이 출력될 수 있습니다.

```text
torch=2.7.1+cu128
torchvision=0.22.1+cu128
torchaudio=2.7.1+cu128
torch_cuda_runtime=12.8
cuda_available=True
cuda_nms=[0]
```

자동화/CI에서는 종료 코드가 0인지 확인하면 됩니다. 탐지 또는 설치 실패는 2, 사용자 중단은 130을 반환합니다.
