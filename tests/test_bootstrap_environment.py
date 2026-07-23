from __future__ import annotations

import argparse
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import bootstrap_environment as bootstrap


def _python_info(path: Path) -> bootstrap.PythonInfo:
    return bootstrap.PythonInfo(
        executable=path,
        implementation="CPython",
        version=(3, 12, 10),
        bits=64,
    )


def _cuda_info() -> bootstrap.NvidiaInfo:
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
        max_cuda_version=(13, 1),
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

    def test_cpu_fallback_is_never_implicit(self) -> None:
        missing = bootstrap.NvidiaInfo(None, (), None, "nvidia-smi was not found")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "--allow-cpu"):
            bootstrap.select_execution_mode(missing, allow_cpu=False)
        self.assertEqual(
            bootstrap.select_execution_mode(missing, allow_cpu=True),
            "cpu-fallback",
        )

        old_driver = bootstrap.NvidiaInfo(
            Path("nvidia-smi"),
            (bootstrap.GpuInfo(0, "Old GPU", "390.0", 4096),),
            (10, 2),
        )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "below required CUDA 11.8"):
            bootstrap.select_execution_mode(old_driver, allow_cpu=False)


class PythonDiscoveryTests(unittest.TestCase):
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


class CommandPlanTests(unittest.TestCase):
    def test_plan_uses_venv_interpreter_and_existing_verifier(self) -> None:
        plan = bootstrap.build_command_plan(
            creator_python=Path("/usr/bin/python3.12"),
            venv_dir=Path("/repo/.venv"),
            venv_python=Path("/repo/.venv/bin/python"),
            requirements_path=Path("/repo/requirements.txt"),
            verify_script=Path("/repo/verify_environment.py"),
            create_venv=True,
            allow_cpu=True,
        )
        self.assertEqual(plan[0].argv[-2:], ("venv", str(Path("/repo/.venv"))))
        self.assertTrue(
            all(command.argv[0] == str(Path("/repo/.venv/bin/python")) for command in plan[1:])
        )
        self.assertEqual(plan[-1].argv[-1], "--allow-cpu")
        requirements_command = plan[2].argv
        self.assertIn("--no-input", requirements_command)
        self.assertIn(str(Path("/repo/requirements.txt")), requirements_command)

    def test_dry_run_does_not_create_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
            (root / "verify_environment.py").write_text("print('ok')\n", encoding="utf-8")
            args = argparse.Namespace(
                project_root=root,
                venv_dir=Path(".venv"),
                requirements=Path("requirements.txt"),
                verify_script=Path("verify_environment.py"),
                python=None,
                nvidia_smi=None,
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

    def test_existing_incomplete_venv_is_not_deleted_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
            (root / "verify_environment.py").write_text("print('ok')\n", encoding="utf-8")
            venv = root / ".venv"
            venv.mkdir()
            sentinel = venv / "user-file.txt"
            sentinel.write_text("keep", encoding="utf-8")
            args = argparse.Namespace(
                project_root=root,
                venv_dir=Path(".venv"),
                requirements=Path("requirements.txt"),
                verify_script=Path("verify_environment.py"),
                python=None,
                nvidia_smi=None,
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
            (root / "verify_environment.py").write_text("print('ok')\n", encoding="utf-8")
            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("placeholder", encoding="utf-8")
            args = argparse.Namespace(
                project_root=root,
                venv_dir=Path(".venv"),
                requirements=Path("requirements.txt"),
                verify_script=Path("verify_environment.py"),
                python=None,
                nvidia_smi=None,
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
            self.assertIn("PLAN install locked requirements", output.getvalue())


if __name__ == "__main__":
    unittest.main()
