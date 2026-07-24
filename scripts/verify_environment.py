from __future__ import annotations

import argparse

import cv2
import laspy
import pyproj
import torch
import torchaudio
import torchvision
import ultralytics
from torchvision.ops import nms


LOCKED_PACKAGE_VERSIONS = {
    "torch": "2.7.1",
    "torchvision": "0.22.1",
    "torchaudio": "2.7.1",
}
SUPPORTED_TORCH_RUNTIMES = {
    "cu128": "12.8",
    "cu126": "12.6",
    "cu118": "11.8",
    "cpu": None,
}


def _base_version(version: str) -> str:
    return version.split("+", maxsplit=1)[0]


def _runtime_from_cuda_version(cuda_version: str | None) -> str:
    for runtime, expected_cuda in SUPPORTED_TORCH_RUNTIMES.items():
        if cuda_version == expected_cuda:
            return runtime
    supported = ", ".join(
        f"{runtime} ({cuda or 'no CUDA'})"
        for runtime, cuda in SUPPORTED_TORCH_RUNTIMES.items()
    )
    raise RuntimeError(
        f"Unsupported bundled PyTorch CUDA runtime {cuda_version!r}; expected one of: {supported}"
    )


def _verify_version(package: str, actual: str, expected_runtime: str) -> None:
    expected_version = LOCKED_PACKAGE_VERSIONS[package]
    if _base_version(actual) != expected_version:
        raise RuntimeError(f"Expected {package} {expected_version}, got {actual}")
    local_version = actual.partition("+")[2]
    if local_version.split(".", maxsplit=1)[0] != expected_runtime:
        raise RuntimeError(
            f"Expected {package} wheel runtime {expected_runtime}, got {actual}"
        )


def verify_environment(
    *,
    allow_cpu: bool,
    expected_torch_runtime: str = "auto",
) -> None:
    print(f"torch={torch.__version__}")
    print(f"torchvision={torchvision.__version__}")
    print(f"torchaudio={torchaudio.__version__}")
    print(f"torch_cuda_runtime={torch.version.cuda}")
    print(f"cuda_available={torch.cuda.is_available()}")
    print(f"opencv={cv2.__version__}")
    print(f"laspy={laspy.__version__}")
    print(f"pyproj={pyproj.__version__}")
    print(f"ultralytics={ultralytics.__version__}")

    installed_runtime = _runtime_from_cuda_version(torch.version.cuda)
    if (
        expected_torch_runtime != "auto"
        and installed_runtime != expected_torch_runtime
    ):
        raise RuntimeError(
            f"Expected PyTorch runtime {expected_torch_runtime}, got {installed_runtime} "
            f"(torch.version.cuda={torch.version.cuda!r})"
        )
    expected_torch_runtime = installed_runtime
    _verify_version("torch", torch.__version__, expected_torch_runtime)
    _verify_version("torchvision", torchvision.__version__, expected_torch_runtime)
    _verify_version("torchaudio", torchaudio.__version__, expected_torch_runtime)
    if laspy.__version__ != "2.7.0":
        raise RuntimeError(f"Expected laspy 2.7.0, got {laspy.__version__}")
    if pyproj.__version__ != "3.7.2":
        raise RuntimeError(f"Expected pyproj 3.7.2, got {pyproj.__version__}")
    if expected_torch_runtime == "cpu":
        if torch.cuda.is_available():
            raise RuntimeError("CPU-only PyTorch wheel unexpectedly reports CUDA as available")
        print("CPU-only PyTorch smoke verification selected.")
        print("environment_check=OK")
        return
    if not torch.cuda.is_available():
        if allow_cpu:
            print("CUDA smoke test skipped (--allow-cpu).")
            print("environment_check=OK")
            return
        raise RuntimeError(
            "CUDA is unavailable. Run scripts/bootstrap_environment.py with the intended "
            "virtual environment, or pass --allow-cpu for an intentional CPU setup."
        )

    device = torch.device("cuda:0")
    print(f"gpu={torch.cuda.get_device_name(device)}")
    print(f"compute_capability={torch.cuda.get_device_capability(device)}")
    boxes = torch.tensor([[0, 0, 10, 10], [1, 1, 9, 9]], dtype=torch.float32, device=device)
    scores = torch.tensor([0.9, 0.8], device=device)
    kept = nms(boxes, scores, 0.5)
    product = torch.randn((256, 256), device=device) @ torch.randn((256, 256), device=device)
    if not torch.isfinite(product).all():
        raise RuntimeError("CUDA matrix multiplication returned a non-finite value")
    print(f"cuda_nms={kept.cpu().tolist()}")
    print("environment_check=OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the isolated MMS inference environment.")
    parser.add_argument("--allow-cpu", action="store_true", help="Do not fail when CUDA is unavailable.")
    parser.add_argument(
        "--expected-torch-runtime",
        choices=("auto", *SUPPORTED_TORCH_RUNTIMES),
        default="auto",
        help="Expected installed PyTorch wheel runtime. auto accepts any supported locked runtime.",
    )
    args = parser.parse_args()
    verify_environment(
        allow_cpu=args.allow_cpu,
        expected_torch_runtime=args.expected_torch_runtime,
    )


if __name__ == "__main__":
    main()
