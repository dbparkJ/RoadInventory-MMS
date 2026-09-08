"""Run real setup wrappers against isolated, non-installing command fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "POSIX launcher is exercised by Linux CI; Windows uses PowerShell.")
class PosixWebSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "project with spaces"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir(parents=True)
        (self.root / "webui").mkdir()
        self.bin = self.base / "tools"
        self.bin.mkdir()
        self.shell = shutil.which("sh")
        for name in ("sh", "dirname"):
            (self.bin / name).symlink_to(shutil.which(name))
        shutil.copyfile(PROJECT_ROOT / "scripts/setup_web.sh", self.scripts / "setup_web.sh")
        self.executable(self.scripts / "setup.sh", "exit 0\n")
        self.venv = self.root / "custom env"
        self.executable(self.venv / "bin/python", 'printf "%s\\n" "$@" > "$VERIFY_TRACE"\nexit "${VERIFY_EXIT:-0}"\n')
        self.environment = {
            **os.environ, "PATH": str(self.bin), "VERIFY_TRACE": str(self.base / "verify-trace.txt"),
        }

    @staticmethod
    def executable(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def run_setup(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.shell, str(self.scripts / "setup_web.sh"), "--project-root", self.root.name, "--venv-dir", "custom env"],
            cwd=self.base, env=self.environment, capture_output=True, text=True,
        )

    def test_relative_root_custom_venv_remains_executable_after_cd(self) -> None:
        self.executable(self.bin / "npm", '''if [ "$1" = run ]; then
    case "$MMS_BUILD_PYTHON" in /*) ;; *) exit 47 ;; esac
    [ -x "$MMS_BUILD_PYTHON" ] || exit 48
fi
exit 0
''')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.root), (self.base / "verify-trace.txt").read_text())

    def test_node_free_verified_package_and_rejected_index_only_propagate_status(self) -> None:
        self.assertEqual(self.run_setup().returncode, 0)
        self.environment["VERIFY_EXIT"] = "2"
        self.assertEqual(self.run_setup().returncode, 2)

    def test_npm_failure_never_runs_success_verification(self) -> None:
        self.executable(self.bin / "npm", 'if [ "$1" = run ]; then exit 7; fi\nexit 0\n')
        result = self.run_setup()
        self.assertEqual(result.returncode, 7)
        self.assertFalse((self.base / "verify-trace.txt").exists())


@unittest.skipUnless(os.name == "nt", "PowerShell launcher is exercised on Windows.")
class WindowsWebSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.interpreter_temp = tempfile.TemporaryDirectory()
        cls.venv = Path(cls.interpreter_temp.name) / "custom env"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(cls.venv)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.interpreter_temp.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "project with spaces"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir(parents=True)
        (self.root / "webui").mkdir()
        self.bin = self.base / "tools"
        self.bin.mkdir()
        shutil.copyfile(PROJECT_ROOT / "scripts/setup_web.ps1", self.scripts / "setup_web.ps1")
        (self.scripts / "build_web.py").write_text(
            'import os, pathlib, sys\npathlib.Path(os.environ["VERIFY_TRACE"]).write_text("\\n".join(sys.argv))\n'
            'raise SystemExit(int(os.environ.get("VERIFY_EXIT", "0")))\n', encoding="utf-8",
        )
        self.driver = self.base / "run.ps1"
        self.driver.write_text('''param($Launcher, $RootName, $Venv, $Tools)
function powershell {
    param([Parameter(ValueFromRemainingArguments = $true)][object[]] $BootstrapArgs)
    $global:LASTEXITCODE = 0
}
$env:PATH = $Tools
& $Launcher --project-root $RootName --venv-dir $Venv
exit $LASTEXITCODE
''', encoding="utf-8")
        self.environment = {**os.environ, "VERIFY_TRACE": str(self.base / "verify-trace.txt"), "NPM_EXIT": "0"}

    def run_setup(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.driver),
             str(self.scripts / "setup_web.ps1"), self.root.name, str(self.venv), str(self.bin)],
            cwd=self.base, env=self.environment, capture_output=True, text=True,
        )

    def add_npm(self) -> None:
        (self.bin / "npm.cmd").write_text('''@echo off
if "%1"=="run" (
    if not exist "%MMS_BUILD_PYTHON%" exit /b 48
    exit /b %NPM_EXIT%
)
exit /b 0
''', encoding="ascii")

    def test_relative_root_custom_venv_success_and_build_failure(self) -> None:
        self.add_npm()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.base / "verify-trace.txt"
        self.assertIn(str(self.root), trace.read_text())
        trace.unlink()
        self.environment["NPM_EXIT"] = "7"
        result = self.run_setup()
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertFalse(trace.exists())

    def test_node_free_verified_and_index_only_status(self) -> None:
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.environment["VERIFY_EXIT"] = "2"
        result = self.run_setup()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
