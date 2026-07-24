from __future__ import annotations

import argparse

import cv2
import laspy
import pyproj
import torch
import torchvision
import ultralytics
from torchvision.ops import nms


def verify_environment(*, allow_cpu: bool) -> None:
    print(f"torch={torch.__version__}")
    print(f"torchvision={torchvision.__version__}")
    print(f"torch_cuda_runtime={torch.version.cuda}")
    print(f"cuda_available={torch.cuda.is_available()}")
    print(f"opencv={cv2.__version__}")
    print(f"laspy={laspy.__version__}")
    print(f"pyproj={pyproj.__version__}")
    print(f"ultralytics={ultralytics.__version__}")

    if not torch.__version__.startswith("2.7.1+cu118"):
        raise RuntimeError(f"Expected torch 2.7.1+cu118, got {torch.__version__}")
    if not torchvision.__version__.startswith("0.22.1+cu118"):
        raise RuntimeError(f"Expected torchvision 0.22.1+cu118, got {torchvision.__version__}")
    if torch.version.cuda != "11.8":
        raise RuntimeError(f"Expected bundled CUDA runtime 11.8, got {torch.version.cuda}")
    if laspy.__version__ != "2.7.0":
        raise RuntimeError(f"Expected laspy 2.7.0, got {laspy.__version__}")
    if pyproj.__version__ != "3.7.2":
        raise RuntimeError(f"Expected pyproj 3.7.2, got {pyproj.__version__}")
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
    args = parser.parse_args()
    verify_environment(allow_cpu=args.allow_cpu)


if __name__ == "__main__":
    main()
