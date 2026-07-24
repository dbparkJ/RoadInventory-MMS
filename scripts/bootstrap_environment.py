from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence


REQUIRED_PYTHON = (3, 12)
REQUIRED_CUDA_RUNTIME = (11, 8)
PYTHON_PROBE = (
    "import json,platform,struct,sys;"
    "print(json.dumps({"
    "'executable':sys.executable,"
    "'implementation':platform.python_implementation(),"
    "'major':sys.version_info[0],"
    "'minor':sys.version_info[1],"
    "'micro':sys.version_info[2],"
    "'bits':struct.calcsize('P')*8"
    "}))"
)


class BootstrapError(RuntimeError):
    """Expected environment/bootstrap failure with a concise user-facing message."""


@dataclass(frozen=True)
class PythonInfo:
    executable: Path
    implementation: str
    version: tuple[int, int, int]
    bits: int

    @property
    def version_text(self) -> str:
        return ".".join(str(value) for value in self.version)


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    driver_version: str
    memory_mib: Optional[int]
    compute_capability: Optional[str] = None


@dataclass(frozen=True)
class NvidiaInfo:
    executable: Optional[Path]
    gpus: tuple[GpuInfo, ...]
    max_cuda_version: Optional[tuple[int, int]]
    error: Optional[str] = None


@dataclass(frozen=True)
class PlannedCommand:
    label: str
    argv: tuple[str, ...]


RunCallable = Callable[..., subprocess.CompletedProcess[str]]


def _capture_command(
    argv: Sequence[str],
    *,
    runner: RunCallable = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _parse_python_probe(stdout: str) -> PythonInfo:
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
        return PythonInfo(
            executable=Path(str(payload["executable"])).resolve(),
            implementation=str(payload["implementation"]),
            version=(int(payload["major"]), int(payload["minor"]), int(payload["micro"])),
            bits=int(payload["bits"]),
        )
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Could not parse Python probe output: {stdout!r}") from exc


def probe_python(
    command: Sequence[str],
    *,
    runner: RunCallable = subprocess.run,
) -> Optional[PythonInfo]:
    result = _capture_command([*command, "-c", PYTHON_PROBE], runner=runner)
    if result.returncode != 0:
        return None
    return _parse_python_probe(result.stdout)


def _deduplicate_commands(commands: Iterable[Sequence[str]]) -> list[tuple[str, ...]]:
    unique: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for command in commands:
        normalized = tuple(str(value) for value in command)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def python_candidate_commands(
    *,
    system_name: str,
    current_executable: str,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> list[tuple[str, ...]]:
    candidates: list[Sequence[str]] = []
    if system_name == "Windows":
        if which("py"):
            candidates.append(("py", f"-{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}"))
        if which("python3.12"):
            candidates.append(("python3.12",))
        candidates.append((current_executable,))
        if which("python"):
            candidates.append(("python",))
    else:
        if which("python3.12"):
            candidates.append(("python3.12",))
        candidates.append((current_executable,))
        if which("python3"):
            candidates.append(("python3",))
        if which("python"):
            candidates.append(("python",))
    return _deduplicate_commands(candidates)


def validate_python(info: PythonInfo, *, context: str) -> None:
    if info.implementation != "CPython":
        raise BootstrapError(
            f"{context} must use CPython {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}, "
            f"but found {info.implementation} {info.version_text}."
        )
    if info.version[:2] != REQUIRED_PYTHON:
        raise BootstrapError(
            f"{context} must use Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}.x, "
            f"but found {info.version_text} at {info.executable}."
        )
    if info.bits != 64:
        raise BootstrapError(
            f"{context} must use 64-bit Python for PyTorch CUDA, but found {info.bits}-bit."
        )


def discover_python(
    explicit_python: Optional[Path] = None,
    *,
    system_name: Optional[str] = None,
    current_executable: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    runner: RunCallable = subprocess.run,
) -> PythonInfo:
    if explicit_python is not None:
        info = probe_python((str(explicit_python),), runner=runner)
        if info is None:
            raise BootstrapError(f"Could not execute the requested Python: {explicit_python}")
        validate_python(info, context="Requested interpreter")
        return info

    system_name = system_name or platform.system()
    current_executable = current_executable or sys.executable
    found: list[str] = []
    for command in python_candidate_commands(
        system_name=system_name,
        current_executable=current_executable,
        which=which,
    ):
        info = probe_python(command, runner=runner)
        if info is None:
            continue
        found.append(f"{info.version_text} ({info.executable})")
        if (
            info.implementation == "CPython"
            and info.version[:2] == REQUIRED_PYTHON
            and info.bits == 64
        ):
            return info

    found_text = ", ".join(found) if found else "no runnable Python interpreters"
    raise BootstrapError(
        f"64-bit CPython {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} was not found; "
        f"probed: {found_text}. Install Python 3.12 and run this command again."
    )


def find_nvidia_smi(
    explicit_path: Optional[Path] = None,
    *,
    system_name: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    environ: Optional[dict[str, str]] = None,
) -> Optional[Path]:
    if explicit_path is not None:
        return explicit_path.resolve()

    located = which("nvidia-smi")
    if located:
        return Path(located).resolve()

    system_name = system_name or platform.system()
    environ = environ or os.environ
    if system_name == "Windows":
        system_root = environ.get("SystemRoot", r"C:\Windows")
        candidate = Path(system_root) / "System32" / "nvidia-smi.exe"
        if candidate.is_file():
            return candidate.resolve()
    else:
        for candidate in (Path("/usr/bin/nvidia-smi"), Path("/usr/local/bin/nvidia-smi")):
            if candidate.is_file():
                return candidate.resolve()
    return None


def parse_nvidia_gpu_csv(
    stdout: str,
    compute_capabilities: Optional[dict[int, str]] = None,
) -> tuple[GpuInfo, ...]:
    compute_capabilities = compute_capabilities or {}
    gpus: list[GpuInfo] = []
    for row in csv.reader(line for line in stdout.splitlines() if line.strip()):
        if len(row) < 4:
            raise BootstrapError(f"Unexpected nvidia-smi GPU row: {row!r}")
        try:
            index = int(row[0].strip())
        except ValueError as exc:
            raise BootstrapError(f"Invalid nvidia-smi GPU index: {row[0]!r}") from exc
        memory_text = row[3].strip()
        try:
            memory_mib = int(memory_text) if memory_text not in {"", "N/A", "[N/A]"} else None
        except ValueError:
            memory_mib = None
        gpus.append(
            GpuInfo(
                index=index,
                name=row[1].strip(),
                driver_version=row[2].strip(),
                memory_mib=memory_mib,
                compute_capability=compute_capabilities.get(index),
            )
        )
    return tuple(gpus)


def parse_max_cuda_version(stdout: str) -> Optional[tuple[int, int]]:
    match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", stdout, flags=re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def detect_nvidia(
    nvidia_smi: Optional[Path],
    *,
    runner: RunCallable = subprocess.run,
) -> NvidiaInfo:
    if nvidia_smi is None:
        return NvidiaInfo(None, (), None, "nvidia-smi was not found")
    if not nvidia_smi.is_file() and nvidia_smi.name not in {"nvidia-smi", "nvidia-smi.exe"}:
        return NvidiaInfo(nvidia_smi, (), None, f"nvidia-smi does not exist: {nvidia_smi}")

    base_query = _capture_command(
        (
            str(nvidia_smi),
            "--query-gpu=index,name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ),
        runner=runner,
    )
    if base_query.returncode != 0:
        detail = (base_query.stderr or base_query.stdout).strip()
        return NvidiaInfo(nvidia_smi, (), None, detail or "nvidia-smi GPU query failed")

    capabilities: dict[int, str] = {}
    capability_query = _capture_command(
        (
            str(nvidia_smi),
            "--query-gpu=index,compute_cap",
            "--format=csv,noheader,nounits",
        ),
        runner=runner,
    )
    if capability_query.returncode == 0:
        for row in csv.reader(line for line in capability_query.stdout.splitlines() if line.strip()):
            if len(row) >= 2:
                try:
                    capabilities[int(row[0].strip())] = row[1].strip()
                except ValueError:
                    continue

    try:
        gpus = parse_nvidia_gpu_csv(base_query.stdout, capabilities)
    except BootstrapError as exc:
        return NvidiaInfo(nvidia_smi, (), None, str(exc))

    banner = _capture_command((str(nvidia_smi),), runner=runner)
    max_cuda = parse_max_cuda_version(f"{banner.stdout}\n{banner.stderr}")
    error = None if banner.returncode == 0 else "nvidia-smi banner query failed"
    return NvidiaInfo(nvidia_smi, gpus, max_cuda, error)


def detect_nvcc_version(
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    runner: RunCallable = subprocess.run,
) -> Optional[str]:
    nvcc = which("nvcc")
    if not nvcc:
        return None
    result = _capture_command((nvcc, "--version"), runner=runner)
    if result.returncode != 0:
        return None
    match = re.search(r"release\s+(\d+\.\d+)", f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else "present (version not parsed)"


def select_execution_mode(nvidia: NvidiaInfo, *, allow_cpu: bool) -> str:
    reason: Optional[str] = None
    if not nvidia.gpus:
        reason = nvidia.error or "no NVIDIA GPU was reported"
    elif nvidia.max_cuda_version is None:
        reason = "nvidia-smi did not report its driver-supported CUDA version"
    elif nvidia.max_cuda_version < REQUIRED_CUDA_RUNTIME:
        required = ".".join(str(value) for value in REQUIRED_CUDA_RUNTIME)
        actual = ".".join(str(value) for value in nvidia.max_cuda_version)
        reason = f"the NVIDIA driver supports CUDA {actual}, below required CUDA {required}"

    if reason is None:
        return "cuda"
    if allow_cpu:
        return "cpu-fallback"
    raise BootstrapError(
        f"CUDA preflight failed: {reason}. CPU fallback is disabled by default; "
        "pass --allow-cpu explicitly to create/verify a CPU-capable environment."
    )


def venv_python_path(venv_dir: Path, *, system_name: Optional[str] = None) -> Path:
    system_name = system_name or platform.system()
    if system_name == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def requirements_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_command_plan(
    *,
    creator_python: Path,
    venv_dir: Path,
    venv_python: Path,
    requirements_path: Path,
    verify_script: Path,
    create_venv: bool,
    allow_cpu: bool,
) -> list[PlannedCommand]:
    plan: list[PlannedCommand] = []
    if create_venv:
        plan.extend(
            (
                PlannedCommand(
                    "create virtual environment",
                    (str(creator_python), "-m", "venv", str(venv_dir)),
                ),
                PlannedCommand(
                    "upgrade packaging tools",
                    (
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        "--upgrade",
                        "pip",
                        "setuptools",
                        "wheel",
                    ),
                ),
            )
        )
    plan.extend(
        (
            PlannedCommand(
                "install locked requirements",
                (
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--requirement",
                    str(requirements_path),
                ),
            ),
            PlannedCommand(
                "check dependency consistency",
                (str(venv_python), "-m", "pip", "check"),
            ),
            PlannedCommand(
                "run PyTorch/CUDA smoke verification",
                (
                    str(venv_python),
                    str(verify_script),
                    *(("--allow-cpu",) if allow_cpu else ()),
                ),
            ),
        )
    )
    return plan


def format_command(argv: Sequence[str], *, system_name: Optional[str] = None) -> str:
    if (system_name or platform.system()) == "Windows":
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


def _run_planned_command(
    command: PlannedCommand,
    *,
    cwd: Path,
    environ: dict[str, str],
    runner: RunCallable = subprocess.run,
) -> None:
    print(f"[bootstrap] {command.label}", flush=True)
    print(f"[bootstrap] $ {format_command(command.argv)}", flush=True)
    try:
        result = runner(
            list(command.argv),
            cwd=str(cwd),
            env=environ,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise BootstrapError(f"Could not run {command.label}: {exc}") from exc
    if result.returncode != 0:
        raise BootstrapError(
            f"{command.label} failed with exit code {result.returncode}: "
            f"{format_command(command.argv)}"
        )


def _print_host_summary(
    python_info: PythonInfo,
    nvidia: NvidiaInfo,
    *,
    execution_mode: str,
    nvcc_version: Optional[str],
) -> None:
    print(f"[bootstrap] host_os={platform.system()} {platform.release()}", flush=True)
    print(
        f"[bootstrap] machine={platform.machine()} process_bits={struct.calcsize('P') * 8}",
        flush=True,
    )
    print(
        f"[bootstrap] creator_python={python_info.version_text} "
        f"({python_info.bits}-bit) path={python_info.executable}",
        flush=True,
    )
    if nvidia.executable is None:
        print("[bootstrap] nvidia_smi=not-found", flush=True)
    else:
        print(f"[bootstrap] nvidia_smi={nvidia.executable}", flush=True)
    for gpu in nvidia.gpus:
        memory = "unknown" if gpu.memory_mib is None else f"{gpu.memory_mib} MiB"
        capability = gpu.compute_capability or "unknown"
        print(
            f"[bootstrap] gpu[{gpu.index}]={gpu.name} driver={gpu.driver_version} "
            f"memory={memory} compute_capability={capability}",
            flush=True,
        )
    max_cuda = (
        "unknown"
        if nvidia.max_cuda_version is None
        else ".".join(str(value) for value in nvidia.max_cuda_version)
    )
    print(f"[bootstrap] driver_cuda_max={max_cuda}", flush=True)
    print(
        f"[bootstrap] local_cuda_toolkit={nvcc_version or 'not-found (not required)'}",
        flush=True,
    )
    print("[bootstrap] locked_torch_cuda_runtime=11.8", flush=True)
    print(f"[bootstrap] execution_mode={execution_mode}", flush=True)


def bootstrap(args: argparse.Namespace, *, runner: RunCallable = subprocess.run) -> None:
    project_root = args.project_root.resolve()
    requirements_path = (
        args.requirements.resolve()
        if args.requirements.is_absolute()
        else (project_root / args.requirements).resolve()
    )
    verify_script = (
        args.verify_script.resolve()
        if args.verify_script.is_absolute()
        else (project_root / args.verify_script).resolve()
    )
    venv_dir = (
        args.venv_dir.resolve()
        if args.venv_dir.is_absolute()
        else (project_root / args.venv_dir).resolve()
    )
    if not project_root.is_dir():
        raise BootstrapError(f"Project root does not exist: {project_root}")
    if not requirements_path.is_file():
        raise BootstrapError(f"Requirements file does not exist: {requirements_path}")
    if not verify_script.is_file():
        raise BootstrapError(f"Environment verifier does not exist: {verify_script}")

    system_name = platform.system()
    python_info = discover_python(
        args.python,
        system_name=system_name,
        runner=runner,
    )
    nvidia_smi = find_nvidia_smi(args.nvidia_smi, system_name=system_name)
    nvidia = detect_nvidia(nvidia_smi, runner=runner)
    execution_mode = select_execution_mode(nvidia, allow_cpu=args.allow_cpu)
    nvcc_version = detect_nvcc_version(runner=runner)
    _print_host_summary(
        python_info,
        nvidia,
        execution_mode=execution_mode,
        nvcc_version=nvcc_version,
    )

    venv_python = venv_python_path(venv_dir, system_name=system_name)
    create_venv = not venv_dir.exists()
    if not create_venv:
        if not venv_dir.is_dir():
            raise BootstrapError(f"Virtual environment path is not a directory: {venv_dir}")
        if not venv_python.is_file():
            raise BootstrapError(
                f"Existing virtual environment is incomplete: {venv_python} is missing. "
                "Move the directory aside and run bootstrap again; it will not delete it automatically."
            )
        existing_info = probe_python((str(venv_python),), runner=runner)
        if existing_info is None:
            raise BootstrapError(f"Could not execute existing virtual environment: {venv_python}")
        validate_python(existing_info, context="Existing virtual environment")
        print(f"[bootstrap] reusing_venv={venv_dir}", flush=True)
    else:
        print(f"[bootstrap] creating_venv={venv_dir}", flush=True)

    print(
        f"[bootstrap] requirements_sha256={requirements_sha256(requirements_path)}",
        flush=True,
    )
    plan = build_command_plan(
        creator_python=python_info.executable,
        venv_dir=venv_dir,
        venv_python=venv_python,
        requirements_path=requirements_path,
        verify_script=verify_script,
        create_venv=create_venv,
        allow_cpu=args.allow_cpu,
    )
    if args.dry_run:
        print(
            "[bootstrap] dry_run=True; no filesystem or package changes will be made.",
            flush=True,
        )
        for command in plan:
            print(
                f"[bootstrap] PLAN {command.label}: "
                f"{format_command(command.argv, system_name=system_name)}",
                flush=True,
            )
        return

    child_environment = os.environ.copy()
    child_environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    for command in plan:
        _run_planned_command(
            command,
            cwd=project_root,
            environ=child_environment,
            runner=runner,
        )

    print(f"[bootstrap] environment_ready={venv_dir}", flush=True)
    print(f"[bootstrap] interpreter={venv_python}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create/reuse the project .venv, install locked dependencies, and verify "
            "the PyTorch CUDA runtime on Windows or Linux."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project directory. Defaults to the repository containing scripts/.",
    )
    parser.add_argument(
        "--venv-dir",
        type=Path,
        default=Path(".venv"),
        help="Virtual environment path, relative to project-root by default.",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=Path("requirements.txt"),
        help="Locked requirements file, relative to project-root by default.",
    )
    parser.add_argument(
        "--verify-script",
        type=Path,
        default=Path("scripts/verify_environment.py"),
        help="Post-install smoke verifier, relative to project-root by default.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        help="Explicit 64-bit CPython 3.12 interpreter. Auto-detected when omitted.",
    )
    parser.add_argument(
        "--nvidia-smi",
        type=Path,
        help="Explicit nvidia-smi path. Auto-detected when omitted.",
    )
    parser.add_argument(
        "--allow-cpu",
        action="store_true",
        help=(
            "Explicitly allow setup and verification when a usable NVIDIA CUDA device is unavailable. "
            "The locked cu118 wheel remains installed and can execute on CPU."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run read-only host detection and print all planned commands without changing files.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bootstrap(args)
    except BootstrapError as exc:
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[bootstrap] ERROR: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
