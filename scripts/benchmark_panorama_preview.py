"""Measure the existing panorama dispatch on deterministic synthetic images.

Cold means derivative cache miss; the OS page cache is intentionally not flushed.
These CPU/file timings do not measure browser, network, GPU, or real MMS latency.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import numpy as np
import PIL
from PIL import Image, features

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mms_shp_detection.webapp import media
from mms_shp_detection.webapp.panorama_fastpath import resize_panorama_fast


def benchmark(samples: int, label: str) -> dict:
    cases = []
    original_check = features.check
    webp_available = original_check("webp")
    configurations = [(1, False), (6, False)]
    if webp_available:
        configurations += [(1, True), (6, True)]
    with tempfile.TemporaryDirectory(prefix="mms-preview-benchmark-") as temporary:
        root = Path(temporary).resolve()
        y, x = np.indices((2048, 4096), dtype=np.uint16)
        pixels = np.stack(
            (
                (x // 8 + y // 8) % 256,
                (x // 16 + y // 4) % 256,
                (x // 4 + y // 16) % 256,
            ),
            axis=-1,
        ).astype(np.uint8)
        for orientation, webp in configurations:
            image = Image.fromarray(pixels)
            exif = Image.Exif()
            exif[274] = orientation
            source = root / f"source-{orientation}.jpg"
            image.save(source, quality=90, exif=exif)
            cold_ms, warm_ms, hashes = [], [], set()
            with mock.patch.object(
                features,
                "check",
                side_effect=lambda name, use_webp=webp: (
                    use_webp if name == "webp" else original_check(name)
                ),
            ):
                # Initialize Pillow codec/plugins before recording samples.
                media._resize_panorama(
                    source, root / "warmup" / f"preview-{orientation}-{webp}", 1024
                )
                for index in range(samples):
                    target = root / f"{orientation}-{webp}" / str(index) / "preview"
                    started = time.perf_counter_ns()
                    output, media_type = media._resize_panorama(source, target, 1024)
                    cold_ms.append((time.perf_counter_ns() - started) / 1_000_000)
                    started = time.perf_counter_ns()
                    reused, reused_type = media._resize_panorama(source, target, 1024)
                    warm_ms.append((time.perf_counter_ns() - started) / 1_000_000)
                    if (reused, reused_type) != (output, media_type):
                        raise RuntimeError(
                            "Cache response changed between cold and warm calls."
                        )
                    hashes.add(hashlib.sha256(output.read_bytes()).hexdigest())
            cases.append(
                {
                    "orientation": orientation,
                    "encoder": "webp" if webp else "jpeg",
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "output_sha256": sorted(hashes),
                    "cold_ms": {
                        "p50": float(np.percentile(cold_ms, 50)),
                        "p95": float(np.percentile(cold_ms, 95)),
                        "samples": cold_ms,
                    },
                    "warm_ms": {
                        "p50": float(np.percentile(warm_ms, 50)),
                        "p95": float(np.percentile(warm_ms, 95)),
                        "samples": warm_ms,
                    },
                }
            )
    return {
        "label": label,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "pillow": PIL.__version__,
        "numpy": np.__version__,
        "webp_available": webp_available,
        "samples_per_case": samples,
        "source_size": [4096, 2048],
        "requested_width": 1024,
        "percentile_method": "numpy.percentile linear",
        "cold": "derivative cache miss; OS cache uncontrolled",
        "warm": "same output reused immediately; no decode",
        "includes": "generator decode/resize/encode/file operations",
        "excludes": "fixture generation, process import, HTTP, browser, semaphore queue, GPU",
        "generator_source_sha256": hashlib.sha256(
            inspect.getsource(resize_panorama_fast).replace("\r\n", "\n").encode()
        ).hexdigest(),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    result = benchmark(args.samples, args.label)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(result, target, indent=2)
        target.write("\n")
    print(
        json.dumps(
            {
                "label": args.label,
                "samples_per_case": args.samples,
                "cases": [
                    {
                        "encoder": case["encoder"],
                        "orientation": case["orientation"],
                        "cold_p50_ms": case["cold_ms"]["p50"],
                        "cold_p95_ms": case["cold_ms"]["p95"],
                        "warm_p50_ms": case["warm_ms"]["p50"],
                        "warm_p95_ms": case["warm_ms"]["p95"],
                    }
                    for case in result["cases"]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
