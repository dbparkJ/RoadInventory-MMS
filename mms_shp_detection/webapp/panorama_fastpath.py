from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

_EXIF_ORIENTATION_TAG = 274
_EXIF_TRANSPOSED_ORIENTATIONS = {5, 6, 7, 8}


def _oriented_size(image: Any) -> tuple[int, int]:
    """Return the dimensions after EXIF orientation is applied."""

    width, height = image.size
    try:
        orientation = int(image.getexif().get(_EXIF_ORIENTATION_TAG, 1))
    except (AttributeError, TypeError, ValueError):
        orientation = 1
    return (height, width) if orientation in _EXIF_TRANSPOSED_ORIENTATIONS else (width, height)


def _decoder_draft_size(
    target_width: int,
    target_height: int,
    *,
    transposed: bool,
) -> tuple[int, int]:
    return (target_height, target_width) if transposed else (target_width, target_height)


def resize_panorama_fast(source: Path, output_base: Path, width: int) -> tuple[Path, str]:
    """Create a cached panorama derivative with bounded decode and encode cost.

    JPEG sources use Pillow's decoder-level ``draft`` downsampling before the
    image is materialized. Large equirectangular sources therefore avoid a full
    resolution RGB decode when the browser only requested a preview derivative.
    """

    from PIL import Image, ImageOps, features

    Image.MAX_IMAGE_PIXELS = 600_000_000
    output_base.parent.mkdir(parents=True, exist_ok=True)
    use_webp = bool(features.check("webp"))
    suffix = ".webp" if use_webp else ".jpg"
    media_type = "image/webp" if use_webp else "image/jpeg"
    output_path = output_base.with_suffix(suffix)
    if output_path.is_file():
        return output_path, media_type

    temporary = output_path.with_name(
        f".{output_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp{suffix}"
    )
    try:
        with Image.open(source) as opened:
            oriented_width, oriented_height = _oriented_size(opened)
            target_width = min(width, oriented_width)
            target_height = max(1, round(oriented_height * target_width / oriented_width))
            try:
                orientation = int(opened.getexif().get(_EXIF_ORIENTATION_TAG, 1))
            except (AttributeError, TypeError, ValueError):
                orientation = 1

            if opened.format == "JPEG" and target_width < oriented_width:
                # JPEG decoders can discard DCT levels before allocating the
                # full pixel buffer. ImageOps.exif_transpose() then operates on
                # the reduced image rather than the original multi-megapixel one.
                opened.draft(
                    "RGB",
                    _decoder_draft_size(
                        target_width,
                        target_height,
                        transposed=orientation in _EXIF_TRANSPOSED_ORIENTATIONS,
                    ),
                )

            image = ImageOps.exif_transpose(opened)
            if image.size != (target_width, target_height):
                image = image.resize(
                    (target_width, target_height),
                    resample=Image.Resampling.LANCZOS,
                    reducing_gap=2.0,
                )
            if use_webp:
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.save(
                    temporary,
                    format="WEBP",
                    quality=82,
                    method=1,
                    exact=False,
                )
            else:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(
                    temporary,
                    format="JPEG",
                    quality=86,
                    optimize=False,
                    progressive=True,
                )
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return output_path, media_type


def install_panorama_fastpath() -> None:
    """Install the optimized implementation without changing the API route."""

    from . import media

    if media._resize_panorama is not resize_panorama_fast:
        media._resize_panorama = resize_panorama_fast
