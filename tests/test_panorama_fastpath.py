from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, JpegImagePlugin, features

from mms_shp_detection.webapp import media
from mms_shp_detection.webapp.panorama_fastpath import resize_panorama_fast


def _write_jpeg(path: Path, size: tuple[int, int] = (2048, 1024)) -> None:
    image = Image.new("RGB", size, (48, 96, 144))
    image.save(path, format="JPEG", quality=90)


class PanoramaFastPathTests(unittest.TestCase):
    def test_webapp_installs_panorama_fastpath(self) -> None:
        self.assertIs(media._resize_panorama, resize_panorama_fast)

    def test_resize_panorama_uses_jpeg_decoder_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            source = directory / "source.jpg"
            _write_jpeg(source)
            draft_calls: list[tuple[str | None, tuple[int, int]]] = []
            original_draft = JpegImagePlugin.JpegImageFile.draft
            original_check = features.check

            def record_draft(image, mode, size):
                draft_calls.append((mode, size))
                return original_draft(image, mode, size)

            with (
                mock.patch.object(
                    JpegImagePlugin.JpegImageFile,
                    "draft",
                    new=record_draft,
                ),
                mock.patch.object(
                    features,
                    "check",
                    side_effect=lambda feature: (
                        False if feature == "webp" else original_check(feature)
                    ),
                ),
            ):
                output, media_type = resize_panorama_fast(
                    source,
                    directory / "preview",
                    512,
                )

            self.assertEqual(draft_calls, [("RGB", (512, 256))])
            self.assertEqual(output.suffix, ".jpg")
            self.assertEqual(media_type, "image/jpeg")
            with Image.open(output) as resized:
                self.assertEqual(resized.size, (512, 256))

    def test_resize_panorama_honors_exif_rotation_before_sizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            source = directory / "rotated.jpg"
            image = Image.new("RGB", (1200, 600), (32, 64, 96))
            exif = Image.Exif()
            exif[274] = 6
            image.save(source, format="JPEG", quality=90, exif=exif)
            draft_calls: list[tuple[str | None, tuple[int, int]]] = []
            original_draft = JpegImagePlugin.JpegImageFile.draft
            original_check = features.check

            def record_draft(image_file, mode, size):
                draft_calls.append((mode, size))
                return original_draft(image_file, mode, size)

            with (
                mock.patch.object(
                    JpegImagePlugin.JpegImageFile,
                    "draft",
                    new=record_draft,
                ),
                mock.patch.object(
                    features,
                    "check",
                    side_effect=lambda feature: (
                        False if feature == "webp" else original_check(feature)
                    ),
                ),
            ):
                output, _ = resize_panorama_fast(
                    source,
                    directory / "preview",
                    300,
                )

            self.assertEqual(draft_calls, [("RGB", (600, 300))])
            with Image.open(output) as resized:
                self.assertEqual(resized.size, (300, 600))

    def test_resize_panorama_prefers_low_latency_jpeg_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            source = directory / "source.jpg"
            _write_jpeg(source, (1024, 512))
            save_options: list[dict[str, object]] = []
            original_save = Image.Image.save
            original_check = features.check

            def record_save(image, fp, format=None, **params):
                save_options.append({"format": format, **params})
                return original_save(image, fp, format=format, **params)

            with (
                mock.patch.object(Image.Image, "save", new=record_save),
                mock.patch.object(
                    features,
                    "check",
                    side_effect=lambda feature: (
                        False if feature == "webp" else original_check(feature)
                    ),
                ),
            ):
                resize_panorama_fast(source, directory / "preview", 512)

            self.assertEqual(save_options[-1]["format"], "JPEG")
            self.assertIs(save_options[-1]["optimize"], False)
            self.assertIs(save_options[-1]["progressive"], True)

    def test_resize_panorama_uses_fast_webp_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            source = directory / "source.jpg"
            _write_jpeg(source, (1024, 512))
            save_options: list[dict[str, object]] = []
            original_save = Image.Image.save
            original_check = features.check

            def record_save(image, fp, format=None, **params):
                save_options.append({"format": format, **params})
                # Keep this regression test independent of the optional WebP
                # encoder while still asserting the selected effort options.
                return original_save(image, fp, format="PNG")

            with (
                mock.patch.object(Image.Image, "save", new=record_save),
                mock.patch.object(
                    features,
                    "check",
                    side_effect=lambda feature: (
                        True if feature == "webp" else original_check(feature)
                    ),
                ),
            ):
                output, media_type = resize_panorama_fast(
                    source,
                    directory / "preview",
                    512,
                )

            self.assertEqual(output.suffix, ".webp")
            self.assertEqual(media_type, "image/webp")
            self.assertEqual(save_options[-1]["format"], "WEBP")
            self.assertEqual(save_options[-1]["method"], 1)
            self.assertEqual(save_options[-1]["quality"], 82)

    def test_resize_panorama_reuses_existing_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            source = directory / "source.jpg"
            _write_jpeg(source)
            original_check = features.check
            with mock.patch.object(
                features,
                "check",
                side_effect=lambda feature: (
                    False if feature == "webp" else original_check(feature)
                ),
            ):
                output, _ = resize_panorama_fast(
                    source,
                    directory / "preview",
                    512,
                )
                initial = output.read_bytes()
                source.unlink()
                reused, media_type = resize_panorama_fast(
                    source,
                    directory / "preview",
                    512,
                )

            self.assertEqual(reused, output)
            self.assertEqual(media_type, "image/jpeg")
            self.assertEqual(reused.read_bytes(), initial)


if __name__ == "__main__":
    unittest.main()
