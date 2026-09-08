"""OS-observed child identity and a fail-closed launch/cancellation handshake.

No operation in this module kills a process discovered by PID after a restart.
Windows termination uses the retained Job Object handle. Linux cancellation uses
the original child's private stdin pipe; the child signals its own process group.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class OwnershipUnknown(RuntimeError):
    pass


def _kernel():
    from ctypes import wintypes
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    dll.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.WaitForSingleObject.restype = wintypes.DWORD
    return dll


def process_identity(pid: int) -> dict[str, Any] | None:
    if type(pid) is not int or pid <= 0:
        raise OwnershipUnknown("invalid process identity")
    if os.name == "nt":
        from ctypes import wintypes
        dll = _kernel()
        handle = dll.OpenProcess(0x1000 | 0x100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
                return None
            raise OwnershipUnknown("process access is unavailable")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            wait = dll.WaitForSingleObject(handle, 0)
            if wait == 0:  # Process handle signalled, including a real exit code of 259.
                return None
            if wait != 258 or not dll.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                raise OwnershipUnknown("process identity query failed")
            created = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
            return {"pid": pid, "platform": "windows", "birth": str(created)}
        finally:
            dll.CloseHandle(handle)
    if not Path("/proc/sys/kernel/random/boot_id").is_file():
        raise OwnershipUnknown("only Linux procfs and Windows are supported")
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] in {"Z", "X"}:
            return None
        return {
            "pid": pid, "platform": "linux", "birth": fields[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "group": int(fields[2]),
        }
    except FileNotFoundError:
        return None
    except (OSError, IndexError, ValueError) as exc:
        raise OwnershipUnknown("process identity query failed") from exc


def _linux_group_alive(group: int) -> bool:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise OwnershipUnknown("process group inspection unavailable") from exc
    for entry in entries:
        if entry.name.isdecimal():
            try:
                identity = process_identity(int(entry.name))
            except OwnershipUnknown:
                # An unreadable process could belong to the group.
                raise
            if identity is not None and identity.get("group") == group:
                return True
    return False


def windows_parent_pid(pid: int) -> int | None:
    """Account for CPython's Windows venv redirector without guessing a child PID."""
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD), ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
    dll = _kernel()
    dll.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    dll.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    dll.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    snapshot = dll.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OwnershipUnknown("process ancestry query unavailable")
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        present = dll.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.pid == pid:
                return int(entry.parent)
            present = dll.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        dll.CloseHandle(snapshot)


def inspect_owner(record: dict[str, Any] | None) -> str:
    """Return exited/live/unknown/reused; PID reuse is evidence, never authority."""
    if not isinstance(record, dict) or record.get("schema") != 1:
        return "unknown"
    if record.get("phase") == "exited":
        return "exited"
    identity = record.get("identity")
    if not isinstance(identity, dict):
        return "unknown"
    try:
        current = process_identity(identity.get("pid"))
        if current is not None:
            return "live" if current == identity else "reused"
        if identity.get("platform") == "linux":
            if identity.get("boot") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                return "exited"
            return "live" if _linux_group_alive(int(identity["group"])) else "exited"
        if identity.get("platform") == "windows":
            # Job absent means the parent's last kill-on-close handle was closed.
            return WindowsJob.inspect(str(record["launcher"]))
    except (OSError, ValueError, TypeError, KeyError, OwnershipUnknown):
        pass
    return "unknown"


def new_intent(run_id: str, attempt: int) -> dict[str, Any]:
    return {"schema": 1, "launcher": uuid.uuid4().hex, "run_id": run_id, "attempt": attempt, "phase": "intent"}


def write_receipt(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class WindowsJob:
    """A retained kernel handle, not a PID snapshot, owns the descendant tree."""
    def __init__(self, launcher: str) -> None:
        from ctypes import wintypes
        self.dll = _kernel()
        self.dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.dll.CreateJobObjectW.restype = wintypes.HANDLE
        self.dll.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.dll.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.handle = self.dll.CreateJobObjectW(None, "Local\\MMS-" + launcher)
        if not self.handle or ctypes.get_last_error() == 183:
            self.close()
            raise OwnershipUnknown("isolated job could not be created")
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.dll.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OwnershipUnknown("job isolation policy unavailable")

    def assign(self, identity: dict[str, Any]) -> None:
        handle = self.dll.OpenProcess(0x0100 | 0x0001 | 0x1000, False, identity["pid"])
        if not handle:
            raise OwnershipUnknown("child assignment handle unavailable")
        try:
            if process_identity(identity["pid"]) != identity:
                raise OwnershipUnknown("child identity changed before assignment")
            if not self.dll.AssignProcessToJobObject(self.handle, handle):
                raise OwnershipUnknown("child job assignment failed")
        finally:
            self.dll.CloseHandle(handle)

    def terminate(self) -> None:
        if self.handle and not self.dll.TerminateJobObject(self.handle, 1):
            raise OwnershipUnknown("owned job termination failed")

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.dll.CloseHandle(self.handle)
            self.handle = None

    @staticmethod
    def inspect(launcher: str) -> str:
        from ctypes import wintypes
        dll = _kernel()
        dll.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        dll.OpenJobObjectW.restype = wintypes.HANDLE
        handle = dll.OpenJobObjectW(0x0004, False, "Local\\MMS-" + launcher)
        if not handle:
            return "exited" if ctypes.get_last_error() == 2 else "unknown"
        try:
            # A job still present might retain descendants. Do not reopen it for kill.
            return "live"
        finally:
            dll.CloseHandle(handle)


class OwnedExecution:
    def __init__(self, record: dict[str, Any], directory: Path) -> None:
        self.record = dict(record)
        self.directory = directory
        self.receipt = directory / (record["launcher"] + ".json")
        self.job = WindowsJob(record["launcher"]) if os.name == "nt" else None

    def environment(self) -> dict[str, str]:
        return {"MMS_OWNED_LAUNCH": json.dumps({**self.record, "receipt": str(self.receipt)})}

    async def attach(self, process: Any) -> dict[str, Any]:
        import asyncio
        for _ in range(200):
            if self.receipt.is_file():
                document = json.loads(self.receipt.read_text(encoding="utf-8"))
                identity = document.get("identity") if isinstance(document, dict) else None
                if not isinstance(identity, dict) or process_identity(identity.get("pid")) != identity:
                    raise OwnershipUnknown("child exited or identity changed before acknowledgement")
                if identity["pid"] != process.pid and not (os.name == "nt" and windows_parent_pid(identity["pid"]) == process.pid):
                    raise OwnershipUnknown("acknowledgement does not belong to the spawned process tree")
                if document != {**self.record, "identity": identity, "phase": "acknowledged"}:
                    raise OwnershipUnknown("child acknowledgement differs from launch intent")
                if self.job is not None:
                    self.job.assign(identity)
                elif identity.get("group") != process.pid:
                    raise OwnershipUnknown("child is not an isolated process group")
                self.record = document
                return document
            if process.returncode is not None:
                break
            await asyncio.sleep(0.05)
        raise OwnershipUnknown("child acknowledgement unavailable")

    def authorize(self, process: Any) -> None:
        if process.stdin is None:
            raise OwnershipUnknown("private child control channel unavailable")
        process.stdin.write(("start:" + self.record["launcher"] + "\n").encode("ascii"))

    def observe(self) -> dict[str, Any]:
        """Timestamp an OS birth-identity observation, not pipeline progress."""
        if inspect_owner(self.record) != "live":
            raise OwnershipUnknown("child identity is no longer confirmed live")
        self.record = {**self.record, "last_observed_alive_at": datetime.now(timezone.utc).isoformat()}
        return dict(self.record)

    async def terminate(self, process: Any) -> None:
        import asyncio
        if self.record.get("phase") == "intent" and getattr(process, "stdin", None) is not None:
            process.stdin.close()  # The child is still gated; EOF prevents all imports.
        if self.job is not None:
            self.job.terminate()
        elif process.returncode is None and process.stdin is not None:
            try:
                process.stdin.write(b"cancel\n")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise OwnershipUnknown("owned child did not acknowledge cancellation") from exc

    def close(self) -> None:
        if self.job is not None:
            self.job.close()


def owned_child_handshake() -> bool:
    """Called before pipeline imports; private pipe EOF means the parent exited."""
    import sys
    raw = os.environ.pop("MMS_OWNED_LAUNCH", None)
    if raw is None:
        return False
    launch = json.loads(raw)
    receipt = Path(launch.pop("receipt"))
    identity = process_identity(os.getpid())
    if identity is None or launch["run_id"] != os.environ.get("MMS_PIPELINE_JOB_ID"):
        raise OwnershipUnknown("child cannot confirm launch identity")
    write_receipt(receipt, {**launch, "identity": identity, "phase": "acknowledged"})
    # Before this exact capability arrives, no model/point-cloud module is imported.
    def control_line() -> str:
        data = bytearray()
        while len(data) < 256:
            part = os.read(0, 1)
            if not part or part == b"\n":
                break
            data.extend(part)
        return data.decode("ascii").strip()
    if control_line() != "start:" + launch["launcher"]:
        raise SystemExit(125)
    owner_pid = os.getpid()
    if os.name != "nt":
        def cleanup(_signal=None, _frame=None):
            if os.getpid() != owner_pid:
                os._exit(143)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(os.getpgrp(), signal.SIGTERM)
            time.sleep(1)
            os.killpg(os.getpgrp(), signal.SIGKILL)
        signal.signal(signal.SIGTERM, cleanup)
        def watch_parent():
            control_line()  # cancel command or EOF, both revoke this launch
            os.kill(owner_pid, signal.SIGTERM)
        threading.Thread(target=watch_parent, name="mms-launch-owner", daemon=True).start()
    return True


def owned_child_finish(owned: bool) -> None:
    if owned and os.name != "nt":
        # The supervisor remains group leader during cleanup, preventing PGID reuse.
        own_group = os.getpgrp()
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal() and int(entry.name) != os.getpid():
                identity = process_identity(int(entry.name))
                if identity is not None and identity.get("group") == own_group:
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
