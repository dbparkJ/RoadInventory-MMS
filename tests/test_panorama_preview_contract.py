from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageOps, features

from mms_shp_detection.webapp import media
from mms_shp_detection.webapp.panorama_fastpath import resize_panorama_fast


def write_oriented_jpeg(path: Path, orientation: int) -> None:
    image = Image.new("RGB", (800, 400))
    for box, color in (
        ((0, 0, 400, 200), (220, 30, 40)),
        ((400, 0, 800, 200), (30, 210, 40)),
        ((0, 200, 400, 400), (30, 40, 210)),
        ((400, 200, 800, 400), (210, 210, 40)),
    ):
        image.paste(color, box)
    exif = Image.Exif()
    exif[274] = orientation
    image.save(path, format="JPEG", quality=95, exif=exif)


class PanoramaPreviewContractTests(unittest.TestCase):
    def test_media_dispatch_preserves_jpeg_bytes_and_all_exif_orientations(
        self,
    ) -> None:
        original_check = features.check
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for orientation in range(1, 9):
                with self.subTest(orientation=orientation):
                    source = root / f"source-{orientation}.jpg"
                    write_oriented_jpeg(source, orientation)
                    with mock.patch.object(
                        features,
                        "check",
                        side_effect=lambda name: (
                            False if name == "webp" else original_check(name)
                        ),
                    ):
                        expected, expected_type = resize_panorama_fast(
                            source, root / f"direct-{orientation}", 200
                        )
                        actual, actual_type = media._resize_panorama(
                            source, root / f"media-{orientation}", 200
                        )
                    self.assertEqual(actual_type, expected_type)
                    self.assertEqual(actual_type, "image/jpeg")
                    self.assertEqual(actual.read_bytes(), expected.read_bytes())
                    with Image.open(source) as original, Image.open(actual) as result:
                        oriented = ImageOps.exif_transpose(original)
                        self.assertEqual(
                            result.size, (200, 400 if orientation >= 5 else 100)
                        )
                        reference = oriented.resize(result.size)
                        for u, v in (
                            (0.25, 0.25),
                            (0.75, 0.25),
                            (0.25, 0.75),
                            (0.75, 0.75),
                        ):
                            pixel = (int(result.width * u), int(result.height * v))
                            self.assertTrue(
                                all(
                                    abs(a - b) <= 8
                                    for a, b in zip(
                                        result.getpixel(pixel),
                                        reference.getpixel(pixel),
                                    )
                                )
                            )

    @unittest.skipUnless(features.check("webp"), "Pillow WebP encoder is unavailable.")
    def test_actual_webp_and_alpha_output_match_optimized_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source.png"
            image = Image.new("RGBA", (800, 400), (20, 90, 140, 80))
            image.save(source)
            expected, _ = resize_panorama_fast(source, root / "direct", 200)
            actual, media_type = media._resize_panorama(source, root / "media", 200)
            self.assertEqual(media_type, "image/webp")
            self.assertEqual(actual.read_bytes(), expected.read_bytes())
            with Image.open(actual) as result:
                self.assertEqual(result.format, "WEBP")
                self.assertEqual(result.size, (200, 100))
                self.assertEqual(result.mode, "RGBA")

    def test_encoder_and_publish_failures_leave_no_partial_derivative(self) -> None:
        for failure in ("encode", "replace"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                source = root / "source.jpg"
                write_oriented_jpeg(source, 1)
                original = OSError("synthetic preview failure")
                target = Image.Image if failure == "encode" else Path
                method = "save" if failure == "encode" else "replace"
                with (
                    mock.patch.object(target, method, side_effect=original),
                    self.assertRaises(OSError) as raised,
                ):
                    media._resize_panorama(source, root / "output" / "preview", 200)
                self.assertIs(raised.exception, original)
                self.assertEqual(list((root / "output").iterdir()), [])
                self.assertTrue(source.is_file())

    def test_fingerprint_changes_with_source_frame_and_requested_width(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve() / "source.jpg"
            write_oriented_jpeg(source, 1)
            fingerprint = media._panorama_fingerprint(source, 200, frame_id="frame-a")
            self.assertEqual(
                media._panorama_fingerprint(source, 200, frame_id="frame-a"),
                fingerprint,
            )
            self.assertNotEqual(
                media._panorama_fingerprint(source, 201, frame_id="frame-a"),
                fingerprint,
            )
            self.assertNotEqual(
                media._panorama_fingerprint(source, 200, frame_id="frame-b"),
                fingerprint,
            )
            source.write_bytes(source.read_bytes() + b"changed input bytes")
            self.assertNotEqual(
                media._panorama_fingerprint(source, 200, frame_id="frame-a"),
                fingerprint,
            )


class PanoramaPreviewImportTests(unittest.TestCase):
    def test_entrypoint_import_order_and_media_reload_keep_explicit_generator(
        self,
    ) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for first_import in (
            "mms_shp_detection.webapp",
            "mms_shp_detection.webapp.media",
            "mms_shp_detection.webapp.panorama_fastpath",
            "mms_shp_detection.webapp.app",
        ):
            with self.subTest(first_import=first_import):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import importlib; "
                            f"importlib.import_module({first_import!r}); "
                            "from mms_shp_detection.webapp import media; "
                            "from mms_shp_detection.webapp.panorama_fastpath import resize_panorama_fast; "
                            "assert media._resize_panorama is resize_panorama_fast; "
                            "importlib.reload(media); "
                            "assert media._resize_panorama is resize_panorama_fast"
                        ),
                    ],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_installer_keeps_existing_media_patch_point(self) -> None:
        from mms_shp_detection.webapp import install_panorama_fastpath

        with mock.patch.object(media, "_resize_panorama") as replacement:
            self.assertIsNone(install_panorama_fastpath())
            self.assertIs(media._resize_panorama, replacement)


if __name__ == "__main__":
    unittest.main()
