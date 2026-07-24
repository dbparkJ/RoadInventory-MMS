from __future__ import annotations

import argparse
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import bootstrap_environment as bootstrap
from scripts import verify_environment as verifier


def _python_info(path: Path) -> bootstrap.PythonInfo:
    return bootstrap.PythonInfo(
        executable=path,
        implementation="CPython",
        version=(3, 12, 10),
        bits=64,
    )


def _cuda_info(
    max_cuda_version: tuple[int, int] = (13, 1),
) -> bootstrap.NvidiaInfo:
    return bootstrap.NvidiaInfo(
        executable=Path("nvidia-smi"),
        gpus=(
            bootstrap.GpuInfo(
                index=0,
                name="NVIDIA Test GPU",
                driver_version="591.74",
                memory_mib=8192,
                compute_capability="8.9",
            ),
        ),
        max_cuda_version=max_cuda_version,
    )


class DetectionParsingTests(unittest.TestCase):
    def test_parses_nvidia_gpu_rows_and_driver_cuda_version(self) -> None:
        rows = bootstrap.parse_nvidia_gpu_csv(
            "0, NVIDIA GeForce RTX 4070 Laptop GPU, 591.74, 8188\n",
            {0: "8.9"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "NVIDIA GeForce RTX 4070 Laptop GPU")
        self.assertEqual(rows[0].driver_version, "591.74")
        self.assertEqual(rows[0].memory_mib, 8188)
        self.assertEqual(rows[0].compute_capability, "8.9")
        self.assertEqual(
            bootstrap.parse_max_cuda_version(
                "NVIDIA-SMI 591.74 Driver Version: 591.74 CUDA Version: 13.1"
            ),
            (13, 1),
        )

    def test_detect_nvidia_tolerates_optional_compute_capability_query(self) -> None:
        def fake_runner(argv, **_kwargs):
            command = " ".join(str(value) for value in argv)
            if "index,name,driver_version,memory.total" in command:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "0, NVIDIA Test GPU, 550.54, 12288\n",
                    "",
                )
            if "index,compute_cap" in command:
                return subprocess.CompletedProcess(argv, 1, "", "unsupported field")
            return subprocess.CompletedProcess(
                argv,
                0,
                "NVIDIA-SMI 550.54 Driver Version: 550.54 CUDA Version: 12.4\n",
                "",
            )

        detected = bootstrap.detect_nvidia(Path("nvidia-smi"), runner=fake_runner)
        self.assertEqual(len(detected.gpus), 1)
        self.assertIsNone(detected.gpus[0].compute_capability)
        self.assertEqual(detected.max_cuda_version, (12, 4))

    def test_auto_runtime_uses_newest_driver_compatible_wheel(self) -> None:
        cases = (
            ((13, 1), "cu128"),
            ((12, 8), "cu128"),
            ((12, 7), "cu126"),
            ((12, 6), "cu126"),
            ((12, 5), "cu118"),
            ((11, 8), "cu118"),
        )
        for driver_cuda, expected_runtime in cases:
            with self.subTest(driver_cuda=driver_cuda):
                selected = bootstrap.select_torch_runtime(
                    _cuda_info(driver_cuda),
                    requested_runtime="auto",
                    allow_cpu=False,
                )
                self.assertEqual(selected.tag, expected_runtime)

    def test_cpu_fallback_is_never_implicit(self) -> None:
        missing = bootstrap.NvidiaInfo(None, (), None, "nvidia-smi was not found")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "--allow-cpu"):
            bootstrap.select_torch_runtime(
                missing,
                requested_runtime="auto",
                allow_cpu=False,
            )
        self.assertEqual(
            bootstrap.select_torch_runtime(
                missing,
                requested_runtime="auto",
                allow_cpu=True,
            ).tag,
            "cpu",
        )

        old_driver = bootstrap.NvidiaInfo(
            Path("nvidia-smi"),
            (bootstrap.GpuInfo(0, "Old GPU", "390.0", 4096),),
            (11, 7),
        )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "below required CUDA 11.8"):
            bootstrap.select_torch_runtime(
                old_driver,
                requested_runtime="auto",
                allow_cpu=False,
            )
        self.assertEqual(
            bootstrap.select_torch_runtime(
                old_driver,
                requested_runtime="auto",
                allow_cpu=True,
            ).tag,
            "cpu",
        )

    def test_explicit_runtime_does_not_silently_downgrade(self) -> None:
        self.assertEqual(
            bootstrap.select_torch_runtime(
                _cuda_info((13, 1)),
                requested_runtime="cu126",
                allow_cpu=False,
            ).tag,
            "cu126",
        )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "cu128 is incompatible"):
            bootstrap.select_torch_runtime(
                _cuda_info((12, 7)),
                requested_runtime="cu128",
                allow_cpu=True,
            )
        self.assertEqual(
            bootstrap.select_torch_runtime(
                bootstrap.NvidiaInfo(None, (), None, "missing"),
                requested_runtime="cpu",
                allow_cpu=False,
            ).tag,
            "cpu",
        )


class PythonDiscoveryTests(unittest.TestCase):
    def test_host_platform_rejects_unsupported_os_and_arm64_early(self) -> None:
        bootstrap.validate_host_platform(system_name="Windows", machine="AMD64")
        bootstrap.validate_host_platform(system_name="Linux", machine="x86_64")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "Unsupported operating system"):
            bootstrap.validate_host_platform(system_name="Darwin", machine="x86_64")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "Unsupported host architecture"):
            bootstrap.validate_host_platform(system_name="Linux", machine="aarch64")

    def test_windows_and_linux_candidates_prefer_python_312(self) -> None:
        available = {
            "py": "C:/Windows/py.exe",
            "python": "C:/Python/python.exe",
            "python3": "/usr/bin/python3",
            "python3.12": "/usr/bin/python3.12",
        }

        def which(name: str):
            return available.get(name)

        windows = bootstrap.python_candidate_commands(
            system_name="Windows",
            current_executable="C:/current/python.exe",
            which=which,
        )
        linux = bootstrap.python_candidate_commands(
            system_name="Linux",
            current_executable="/usr/bin/python3.11",
            which=which,
        )
        self.assertEqual(windows[0], ("py", "-3.12"))
        self.assertEqual(linux[0], ("python3.12",))

    def test_virtual_environment_python_path_is_cross_platform(self) -> None:
        root = Path("project") / ".venv"
        self.assertEqual(
            bootstrap.venv_python_path(root, system_name="Windows"),
            root / "Scripts" / "python.exe",
        )
        self.assertEqual(
            bootstrap.venv_python_path(root, system_name="Linux"),
            root / "bin" / "python",
        )

    def test_relocated_parser_defaults_point_back_to_repository(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        args = bootstrap.build_parser().parse_args([])
        self.assertEqual(args.project_root, repository_root)
        self.assertEqual(args.verify_script, Path("scripts/verify_environment.py"))
        self.assertEqual(args.torch_runtime, "auto")
        self.assertTrue((repository_root / "scripts" / "setup.ps1").is_file())
        self.assertTrue((repository_root / "scripts" / "setup.sh").is_file())


class EnvironmentVerifierTests(unittest.TestCase):
    def test_bootstrap_and_verifier_share_the_same_locked_matrix(self) -> None:
        self.assertEqual(
            verifier.LOCKED_PACKAGE_VERSIONS,
            dict(bootstrap.PYTORCH_PACKAGES),
        )
        self.assertEqual(
            verifier.SUPPORTED_TORCH_RUNTIMES,
            {
                runtime.tag: (
                    runtime.cuda_version_text
                    if runtime.cuda_version is not None
                    else None
                )
                for runtime in bootstrap.TORCH_RUNTIME_BY_TAG.values()
            },
        )

    def test_maps_supported_bundled_cuda_versions_to_runtime_tags(self) -> None:
        self.assertEqual(verifier._runtime_from_cuda_version("12.8"), "cu128")
        self.assertEqual(verifier._runtime_from_cuda_version("12.6"), "cu126")
        self.assertEqual(verifier._runtime_from_cuda_version("11.8"), "cu118")
        self.assertEqual(verifier._runtime_from_cuda_version(None), "cpu")
        with self.assertRaisesRegex(RuntimeError, "Unsupported bundled"):
            verifier._runtime_from_cuda_version("12.4")
        with self.assertRaisesRegex(RuntimeError, "wheel runtime cu128"):
            verifier._verify_version("torch", "2.7.1", "cu128")

    def test_explicit_cpu_fallback_prints_success_marker(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(verifier.torch.cuda, "is_available", return_value=False),
            redirect_stdout(output),
        ):
            verifier.verify_environment(allow_cpu=True)
        rendered = output.getvalue()
        self.assertIn("CUDA smoke test skipped (--allow-cpu).", rendered)
        self.assertIn("environment_check=OK", rendered)

    def test_cpu_wheel_verification_prints_success_marker(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(verifier.torch, "__version__", "2.7.1+cpu"),
            mock.patch.object(verifier.torchvision, "__version__", "0.22.1+cpu"),
            mock.patch.object(verifier.torchaudio, "__version__", "2.7.1+cpu"),
            mock.patch.object(verifier.torch.version, "cuda", None),
            mock.patch.object(verifier.torch.cuda, "is_available", return_value=False),
            redirect_stdout(output),
        ):
            verifier.verify_environment(
                allow_cpu=True,
                expected_torch_runtime="cpu",
            )
        rendered = output.getvalue()
        self.assertIn("CPU-only PyTorch smoke verification selected.", rendered)
        self.assertIn("environment_check=OK", rendered)


class CommandPlanTests(unittest.TestCase):
    def test_requirements_keep_framework_versions_but_not_a_fixed_cuda_index(self) -> None:
        requirements = (
            Path(__file__).resolve().parents[1] / "requirements.txt"
        ).read_text(encoding="utf-8")
        self.assertNotIn("download.pytorch.org/whl/cu", requirements)
        self.assertIn("torch==2.7.1", requirements)
        self.assertIn("torchvision==0.22.1", requirements)
        self.assertIn("torchaudio==2.7.1", requirements)

    def test_plan_uses_venv_interpreter_and_existing_verifier(self) -> None:
        plan = bootstrap.build_command_plan(
            creator_python=Path("/usr/bin/python3.12"),
            venv_dir=Path("/repo/.venv"),
            venv_python=Path("/repo/.venv/bin/python"),
            requirements_path=Path("/repo/requirements.txt"),
            verify_script=Path("/repo/scripts/verify_environment.py"),
            create_venv=True,
            torch_runtime=bootstrap.TORCH_RUNTIME_BY_TAG["cpu"],
        )
        self.assertEqual(plan[0].argv[-2:], ("venv", str(Path("/repo/.venv"))))
        self.assertTrue(
            all(command.argv[0] == str(Path("/repo/.venv/bin/python")) for command in plan[1:])
        )
        self.assertIn("--allow-cpu", plan[-1].argv)
        self.assertEqual(
            plan[-1].argv[plan[-1].argv.index("--expected-torch-runtime") + 1],
            "cpu",
        )
        torch_command = next(
            command.argv for command in plan if "PyTorch stack" in command.label
        )
        self.assertIn("https://download.pytorch.org/whl/cpu", torch_command)
        self.assertIn("torch==2.7.1+cpu", torch_command)
        self.assertIn("torchvision==0.22.1+cpu", torch_command)
        self.assertIn("torchaudio==2.7.1+cpu", torch_command)
        requirements_command = next(
            command.argv for command in plan if command.label == "install locked project requirements"
        )
        self.assertIn("--no-input", requirements_command)
        self.assertIn(str(Path("/repo/requirements.txt")), requirements_command)

    def test_dry_run_does_not_create_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
            verifier_path = root / "scripts" / "verify_environment.py"
            verifier_path.parent.mkdir()
            verifier_path.write_text("print('ok')\n", encoding="utf-8")
            args = argparse.Namespace(
                project_root=root,
                venv_dir=Path(".venv"),
                requirements=Path("requirements.txt"),
                verify_script=Path("scripts/verify_environment.py"),
                python=None,
                nvidia_smi=None,
                torch_runtime="auto",
                allow_cpu=False,
                dry_run=True,
            )
            output = io.StringIO()
            with (
                mock.patch.object(
                    bootstrap,
                    "discover_python",
                    return_value=_python_info(Path("/usr/bin/python3.12")),
                ),
                mock.patch.object(bootstrap, "find_nvidia_smi", return_value=Path("nvidia-smi")),
                mock.patch.object(bootstrap, "detect_nvidia", return_value=_cuda_info()),
                mock.patch.object(bootstrap, "detect_nvcc_version", return_value=None),
                redirect_stdout(output),
            ):
                bootstrap.bootstrap(args)

            self.assertFalse((root / ".venv").exists())
            rendered = output.getvalue()
            self.assertIn("dry_run=True", rendered)
            self.assertIn("create virtual environment", rendered)
            self.assertIn("verify_environment.py", rendered)
            self.assertIn("selected_torch_runtime=cu128", rendered)
            self.assertIn("https://download.pytorch.org/whl/cu128", rendered)
            self.assertIn("torch==2.7.1+cu128", rendered)
            self.assertIn("torchvision==0.22.1+cu128", rendered)
            self.assertIn("torchaudio==2.7.1+cu128", rendered)

    def test_existing_incomplete_venv_is_not_deleted_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
            verifier_path = root / "scripts" / "verify_environment.py"
            verifier_path.parent.mkdir()
            verifier_path.write_text("print('ok')\n", encoding="utf-8")
            venv = root / ".venv"
            venv.mkdir()
            sentinel = venv / "user-file.txt"
            sentinel.write_text("keep", encoding="utf-8")
            args = argparse.Namespace(
                project_root=root,
                venv_dir=Path(".venv"),
                requirements=Path("requirements.txt"),
                verify_script=Path("scripts/verify_environment.py"),
                python=None,
                nvidia_smi=None,
                torch_runtime="auto",
                allow_cpu=False,
                dry_run=True,
            )
            with (
                mock.patch.object(
                    bootstrap,
                    "discover_python",
                    return_value=_python_info(Path("/usr/bin/python3.12")),
                ),
                mock.patch.object(bootstrap, "find_nvidia_smi", return_value=Path("nvidia-smi")),
                mock.patch.object(bootstrap, "detect_nvidia", return_value=_cuda_info()),
                mock.patch.object(bootstrap, "detect_nvcc_version", return_value=None),
                self.assertRaisesRegex(bootstrap.BootstrapError, "will not delete it automatically"),
            ):
                bootstrap.bootstrap(args)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_rerun_reuses_compatible_environment_without_venv_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
            verifier_path = root / "scripts" / "verify_environment.py"
            verifier_path.parent.mkdir()
            verifier_path.write_text("print('ok')\n", encoding="utf-8")
            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("placeholder", encoding="utf-8")
            args = argparse.Namespace(
                project_root=root,
                venv_dir=Path(".venv"),
                requirements=Path("requirements.txt"),
                verify_script=Path("scripts/verify_environment.py"),
                python=None,
                nvidia_smi=None,
                torch_runtime="auto",
                allow_cpu=False,
                dry_run=True,
            )
            output = io.StringIO()
            with (
                mock.patch.object(bootstrap.platform, "system", return_value="Linux"),
                mock.patch.object(
                    bootstrap,
                    "discover_python",
                    return_value=_python_info(Path("/usr/bin/python3.12")),
                ),
                mock.patch.object(bootstrap, "find_nvidia_smi", return_value=Path("nvidia-smi")),
                mock.patch.object(bootstrap, "detect_nvidia", return_value=_cuda_info()),
                mock.patch.object(bootstrap, "detect_nvcc_version", return_value=None),
                mock.patch.object(
                    bootstrap,
                    "probe_python",
                    return_value=_python_info(venv_python),
                ),
                redirect_stdout(output),
            ):
                bootstrap.bootstrap(args)
            self.assertIn("reusing_venv", output.getvalue())
            self.assertNotIn("PLAN create virtual environment", output.getvalue())
            self.assertNotIn("PLAN upgrade packaging tools", output.getvalue())
            self.assertIn("PLAN install locked PyTorch stack (cu128)", output.getvalue())
            self.assertIn("PLAN install locked project requirements", output.getvalue())


if __name__ == "__main__":
    unittest.main()
