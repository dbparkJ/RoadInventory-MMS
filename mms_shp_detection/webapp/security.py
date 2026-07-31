from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS_INVALID_COMPONENT = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_RESERVED_COMPONENT = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)",
    re.IGNORECASE,
)


class UnsafePath(ValueError):
    """Raised when an API path could escape its configured storage root."""


def opaque_id(prefix: str, *parts: object, length: int = 24) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8", "surrogatepass")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:length]}"


def normalize_relative_path(value: str | None, *, allow_empty: bool = True) -> str:
    """Return a portable relative POSIX path or reject ambiguous input.

    Backslashes are intentionally rejected instead of treated as separators.
    This makes traversal behavior identical on Windows and Linux.
    """

    raw_text = "" if value is None else str(value)
    if raw_text != raw_text.strip():
        raise UnsafePath("The path cannot start or end with whitespace.")
    text = raw_text
    if "\x00" in text or "\\" in text or _WINDOWS_DRIVE.match(text):
        raise UnsafePath("The path must be a portable relative path.")
    if not text:
        if allow_empty:
            return ""
        raise UnsafePath("A relative path is required.")
    raw_parts = text.split("/")
    if any(
        _WINDOWS_INVALID_COMPONENT.search(part)
        or _WINDOWS_RESERVED_COMPONENT.match(part)
        or part.endswith((" ", "."))
        for part in raw_parts
        if part
    ):
        raise UnsafePath(
            "The path contains a Windows-reserved name or character."
        )
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise UnsafePath("The path must stay inside the selected storage root.")
    normalized = pure.as_posix()
    if normalized.startswith("../") or normalized == "..":
        raise UnsafePath("The path must stay inside the selected storage root.")
    return normalized


def assert_no_symlink_descendants(root: Path) -> None:
    """Reject symlinks, junctions, and resolved descendants outside ``root``."""

    resolved_root = root.expanduser().resolve(strict=True)
    if not resolved_root.is_dir():
        raise NotADirectoryError("The selected path is not a directory.")
    pending = [resolved_root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    try:
                        is_junction = bool(
                            getattr(child, "is_junction", lambda: False)()
                        )
                        if entry.is_symlink() or is_junction:
                            raise UnsafePath(
                                "Symbolic links and junctions are not allowed "
                                "inside MMS dataset folders."
                            )
                        if entry.is_dir(follow_symlinks=False):
                            resolved_child = child.resolve(strict=True)
                            try:
                                resolved_child.relative_to(resolved_root)
                            except ValueError as exc:
                                raise UnsafePath(
                                    "A dataset folder escaped its storage root."
                                ) from exc
                            pending.append(resolved_child)
                    except OSError as exc:
                        raise UnsafePath(
                            "A dataset entry could not be inspected safely."
                        ) from exc
        except UnsafePath:
            raise
        except OSError as exc:
            raise UnsafePath(
                "The selected dataset folder could not be inspected safely."
            ) from exc


def _assert_no_symlink_components(root: Path, candidate: Path) -> None:
    relative = candidate.relative_to(root)
    current = root
    if current.is_symlink():
        raise UnsafePath("Symbolic-link storage roots are not allowed.")
    for part in relative.parts:
        current = current / part
        # Missing leaf/parents are permitted for upload staging, but every
        # existing component must be a real directory/file.
        if current.exists() and current.is_symlink():
            raise UnsafePath("Symbolic links are not allowed in storage paths.")


def resolve_under_root(
    root: Path,
    relative_path: str | None,
    *,
    must_exist: bool = True,
    expect_directory: bool | None = None,
    reject_symlinks: bool = True,
) -> Path:
    normalized = normalize_relative_path(relative_path)
    resolved_root = root.expanduser().resolve(strict=True)
    candidate = resolved_root.joinpath(*PurePosixPath(normalized).parts)
    # ``resolve(strict=False)`` resolves all existing symlinks and normalizes
    # missing suffixes, allowing a reliable containment check before creation.
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafePath("The path must stay inside the selected storage root.") from exc
    if reject_symlinks:
        _assert_no_symlink_components(resolved_root, candidate)
    if must_exist and not resolved.exists():
        raise FileNotFoundError("The selected path does not exist.")
    if expect_directory is True and resolved.exists() and not resolved.is_dir():
        raise NotADirectoryError("The selected path is not a directory.")
    if expect_directory is False and resolved.exists() and not resolved.is_file():
        raise IsADirectoryError("The selected path is not a file.")
    return resolved


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def safe_upload_name(value: str) -> str:
    text = str(value).strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise UnsafePath("Upload names must be a single folder name.")
    sanitized = re.sub(r"[^0-9A-Za-z가-힣._ -]+", "_", text).strip(" .")
    if not sanitized:
        raise UnsafePath("Upload name does not contain a usable character.")
    # Keep room for an opaque suffix and avoid platform path limits.
    return sanitized[:120]


def atomic_replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
