# MMS 실행환경 자동 구성

`bootstrap_environment.py`는 Windows와 Linux에서 다음 작업을 비대화형으로 수행합니다.

1. 운영체제와 64-bit CPython 3.12 자동 탐지
2. `nvidia-smi`를 통한 NVIDIA GPU, driver, compute capability, driver 지원 CUDA 버전 확인
3. 프로젝트의 `.venv` 생성 또는 기존 환경 재사용
4. `requirements.txt`의 고정 버전 설치
5. `pip check` 실행
6. `verify_environment.py`를 통한 torch/torchvision 버전, CUDA 11.8 runtime, CUDA NMS와 행렬곱 smoke test

PyTorch wheel은 자체 CUDA 11.8 runtime을 포함하므로 시스템 CUDA Toolkit이나 `nvcc` 설치는 필수가 아닙니다. `nvidia-smi`의 `CUDA Version`은 설치된 Toolkit 버전이 아니라 현재 driver가 지원하는 최대 CUDA 버전입니다.

## Windows

PowerShell에서 프로젝트 루트로 이동한 뒤 실행합니다.

```powershell
Set-Location D:\mms_project
py -3.12 .\bootstrap_environment.py
```

스크립트는 활성화된 환경이나 전역 `pip`를 사용하지 않고 항상 `.venv\Scripts\python.exe -m pip`를 호출합니다.

## Linux

```bash
cd /path/to/mms_project
python3.12 bootstrap_environment.py
```

배포판에서 `venv` 모듈을 별도 패키지로 제공한다면 먼저 Python 3.12용 venv 패키지가 설치돼 있어야 합니다. 예를 들어 Ubuntu 계열에서는 관리자에게 `python3.12-venv` 설치를 요청합니다.

## 사전 확인만 하기

아래 명령은 OS/Python/GPU/driver를 탐지하고 실행 예정 명령을 출력하지만 `.venv`나 패키지를 변경하지 않습니다.

```powershell
py -3.12 .\bootstrap_environment.py --dry-run
```

```bash
python3.12 bootstrap_environment.py --dry-run
```

## CPU fallback

GPU가 없거나 driver가 CUDA 11.8을 지원하지 않으면 기본 실행은 즉시 실패합니다. CPU 실행을 의도한 경우에만 `--allow-cpu`를 명시합니다.

```powershell
py -3.12 .\bootstrap_environment.py --allow-cpu
```

이 옵션은 requirements를 임의로 CPU 전용 wheel로 바꾸지 않습니다. 고정된 PyTorch 2.7.1+cu118 wheel을 설치하되 CUDA smoke test를 생략할 수 있게 하며, 같은 환경을 CPU 추론에 사용합니다.

## 재실행

동일 명령을 다시 실행해도 기존 `.venv`를 삭제하거나 덮어 만들지 않습니다. 고정 requirements를 다시 적용한 뒤 dependency 및 CUDA 검사를 반복합니다.

기존 `.venv`가 Python 3.12가 아니거나 중간에 손상돼 interpreter가 없다면 스크립트는 해당 디렉터리를 자동 삭제하지 않고 중단합니다. 필요한 파일을 백업하고 `.venv`를 직접 다른 이름으로 이동한 뒤 다시 실행하십시오.

## 선택 옵션

특정 Python이나 `nvidia-smi`를 지정할 수 있습니다.

```powershell
py -3.12 .\bootstrap_environment.py `
  --python C:\Python312\python.exe `
  --nvidia-smi C:\Windows\System32\nvidia-smi.exe
```

다른 프로젝트 사본이나 가상환경 경로를 검사할 때:

```bash
python3.12 bootstrap_environment.py \
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

GPU 환경에서는 추가로 다음 핵심값을 확인합니다.

```text
torch=2.7.1+cu118
torchvision=0.22.1+cu118
torch_cuda_runtime=11.8
cuda_available=True
cuda_nms=[0]
```

자동화/CI에서는 종료 코드가 0인지 확인하면 됩니다. 탐지 또는 설치 실패는 2, 사용자 중단은 130을 반환합니다.

