"""Deterministic, exact-scope source inventory and SHA-256 memoization."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .contracts import (
    OBJECT_CROP_SCHEMA_VERSION,
    SOURCE_INVENTORY_SCHEMA_NAME,
    SourceScope,
    canonical_json_bytes,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_AUTHORITATIVE_SOURCE_TYPES = frozenset({"las", "laz", "copc"})
_SUPPORTED_SOURCE_TYPES = _AUTHORITATIVE_SOURCE_TYPES | {"pcdb"}
_HASH_CHUNK_SIZE = 4 * 1024 * 1024


class SourceInventoryError(ValueError):
    """Base exception for a source-inventory contract violation."""


class SourceScopeError(SourceInventoryError):
    """Raised when an exact source scope cannot be proven."""


class SourceMutationError(SourceInventoryError):
    """Raised when a source changes while its run inventory is being built."""


@dataclass(frozen=True)
class _SourceStat:
    file_size: int
    mtime_ns: int
    device: int
    inode: int

    @classmethod
    def from_stat_result(cls, value: os.stat_result) -> "_SourceStat":
        return cls(
            file_size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            device=int(value.st_dev),
            inode=int(value.st_ino),
        )

    def cache_matches(self, value: Mapping[str, Any]) -> bool:
        return bool(
            value.get("file_size") == self.file_size
            and value.get("mtime_ns") == self.mtime_ns
            and value.get("device", self.device) == self.device
            and value.get("inode", self.inode) == self.inode
            and isinstance(value.get("sha256"), str)
            and _SHA256_PATTERN.fullmatch(str(value["sha256"]))
        )

    def to_cache_dict(self, sha256: str) -> dict[str, Any]:
        return {
            "file_size": self.file_size,
            "mtime_ns": self.mtime_ns,
            "device": self.device,
            "inode": self.inode,
            "sha256": sha256,
        }


class SourceHashCache:
    """Thread-safe run-level cache keyed by resolved source path and stat."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._hash_count = 0

    @property
    def hash_count(self) -> int:
        with self._lock:
            return self._hash_count

    def digest_for(self, path: Path, snapshot: _SourceStat) -> str:
        """Return one digest, serializing cache misses across worker threads."""

        key = _normalized_cache_key(path)
        # Hashing under the run-cache lock is intentional: duplicate hashing of
        # a multi-gigabyte source is costlier than serializing rare cache misses.
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and snapshot.cache_matches(cached):
                return str(cached["sha256"])
            digest, final_snapshot = _hash_source(path, snapshot)
            self._entries[key] = final_snapshot.to_cache_dict(digest)
            self._hash_count += 1
            return digest

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hash_count = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


HashCache = SourceHashCache | MutableMapping[str, Any]


def _normalized_cache_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _mapping_cache_get(
    cache: MutableMapping[str, Any],
    key: str,
    snapshot: _SourceStat,
) -> str | None:
    cached = cache.get(key)
    if isinstance(cached, Mapping) and snapshot.cache_matches(cached):
        return str(cached["sha256"])
    return None


def _mapping_cache_put(
    cache: MutableMapping[str, Any],
    key: str,
    snapshot: _SourceStat,
    sha256: str,
) -> None:
    cache[key] = snapshot.to_cache_dict(sha256)


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(_HASH_CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _hash_source(path: Path, expected: _SourceStat) -> tuple[str, _SourceStat]:
    """Hash one source and fail if its identity/stat changes during the read."""

    with path.open("rb") as stream:
        opened = _SourceStat.from_stat_result(os.fstat(stream.fileno()))
        if opened != expected:
            raise SourceMutationError(f"source changed before hashing: {path}")
        digest = _sha256_stream(stream)
        after_read = _SourceStat.from_stat_result(os.fstat(stream.fileno()))
    after_path = _SourceStat.from_stat_result(path.stat())
    if after_read != expected or after_path != expected:
        raise SourceMutationError(f"source changed while hashing: {path}")
    return digest, expected


def _source_digest(
    path: Path,
    snapshot: _SourceStat,
    cache: HashCache | None,
) -> str:
    key = _normalized_cache_key(path)
    if isinstance(cache, SourceHashCache):
        return cache.digest_for(path, snapshot)
    elif cache is not None:
        cached = _mapping_cache_get(cache, key, snapshot)
    else:
        cached = None
    if cached is not None:
        return cached

    digest, final_snapshot = _hash_source(path, snapshot)
    if cache is not None:
        _mapping_cache_put(cache, key, final_snapshot, digest)
    return digest


def _resolve_source(path_value: Any, data_root: Path) -> tuple[Path, str]:
    if not isinstance(path_value, (str, os.PathLike)):
        raise SourceInventoryError("catalog source path must be a string or path")
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = data_root / candidate
    lexical = Path(os.path.abspath(candidate))
    try:
        lexical_relative = lexical.relative_to(data_root)
    except ValueError:
        lexical_relative = None
    if lexical_relative is not None:
        boundary = data_root
        for part in lexical_relative.parts:
            boundary /= part
            if boundary.is_symlink() or bool(
                getattr(boundary, "is_junction", lambda: False)()
            ):
                raise SourceInventoryError(
                    "symbolic-link/junction point source is not authoritative: "
                    f"{candidate}"
                )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SourceInventoryError(f"point source does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise SourceInventoryError(f"point source is not a regular file: {candidate}")
    try:
        relative = resolved.relative_to(data_root)
    except ValueError as exc:
        raise SourceInventoryError(
            f"point source escaped the configured data root: {candidate}"
        ) from exc
    uri = relative.as_posix()
    if not uri or uri.startswith("/") or "\\" in uri or ".." in PurePosixPath(uri).parts:
        raise SourceInventoryError(f"unsafe source_file_uri derived for: {candidate}")
    return resolved, uri


def _normalize_source_type(value: Any) -> str:
    if not isinstance(value, str):
        raise SourceInventoryError("source_type must be a string")
    normalized = value.strip().casefold()
    if normalized == "copc.laz":
        normalized = "copc"
    if normalized not in _SUPPORTED_SOURCE_TYPES:
        raise SourceInventoryError(f"unsupported source_type: {value!r}")
    return normalized


def _path_source_type(path: Path) -> str:
    lower_name = path.name.casefold()
    if lower_name.endswith(".copc.laz"):
        return "copc"
    source_type = path.suffix.casefold().removeprefix(".")
    if source_type not in _SUPPORTED_SOURCE_TYPES:
        raise SourceInventoryError(
            f"unsupported point-source extension for inventory: {path.name}"
        )
    return source_type


def _source_type(item: Mapping[str, Any], path: Path) -> str:
    actual = _path_source_type(path)
    declared_values = [
        (name, item[name])
        for name in ("source_type", "format")
        if name in item and item[name] is not None
    ]
    if not declared_values:
        return actual
    for field_name, raw in declared_values:
        declared = _normalize_source_type(raw)
        if declared != actual:
            raise SourceInventoryError(
                f"catalog {field_name} does not match source extension: "
                f"declared={declared!r}, actual={actual!r}, path={path.name!r}"
            )
    return actual


def _catalog_identity(item: Mapping[str, Any]) -> tuple[Any, Any]:
    return (
        item.get("job_id", item.get("job_name")),
        item.get("track_id", item.get("track_name")),
    )


def _catalog_files(catalog: Mapping[str, Any]) -> Sequence[Any]:
    if not isinstance(catalog, Mapping):
        raise TypeError("catalog must be a mapping")
    files = catalog.get("files")
    if isinstance(files, (str, bytes)) or not isinstance(files, Sequence):
        raise SourceInventoryError("catalog['files'] must be a list")
    return files


def build_source_inventory(
    catalog: Mapping[str, Any],
    data_root: str | os.PathLike[str],
    scope: SourceScope | Mapping[str, Any],
    hash_cache: HashCache | None = None,
) -> dict[str, Any]:
    """Build a deterministic inventory for sources admitted by an exact scope.

    Parsed-but-out-of-scope files are discarded before their paths are opened.
    A strict scope rejects missing identity.  A non-strict empty scope means
    "all parsed sources"; unparsed sources still fail closed and are skipped.
    """

    try:
        source_scope = SourceScope.from_value(scope)
    except (TypeError, ValueError) as exc:
        raise SourceScopeError(f"invalid source_scope: {exc}") from exc
    if source_scope.strict and not source_scope.jobs:
        raise SourceScopeError("strict source_scope requires at least one job")
    root = Path(data_root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"point-cloud data root is not a directory: {root}")

    candidates: list[dict[str, Any]] = []
    logical_uris: set[str] = set()
    parsed_identity_scope = SourceScope(strict=False, jobs=())
    for row_index, raw_item in enumerate(_catalog_files(catalog)):
        if not isinstance(raw_item, Mapping):
            raise SourceInventoryError(f"catalog files[{row_index}] must be a mapping")
        job_value, track_value = _catalog_identity(raw_item)
        parsed_identity = parsed_identity_scope.canonical_identity(
            job_value,
            track_value,
        )
        if parsed_identity is None:
            if source_scope.strict:
                raise SourceScopeError(
                    f"catalog source at index {row_index} has invalid job/track identity"
                )
            continue
        canonical_identity = source_scope.canonical_identity(job_value, track_value)
        if canonical_identity is None:
            # Parsed, known out-of-scope sources are excluded before resolving
            # or opening the file. Invalid identity was handled above.
            continue

        path, source_file_uri = _resolve_source(raw_item.get("path"), root)
        if source_file_uri in logical_uris:
            raise SourceInventoryError(
                f"duplicate source_file_uri in catalog: {source_file_uri}"
            )
        logical_uris.add(source_file_uri)
        candidates.append(
            {
                "path": path,
                "source_file_uri": source_file_uri,
                "source_type": _source_type(raw_item, path),
                "job_id": canonical_identity[0],
                "track_id": canonical_identity[1],
                "catalog_provenance_complete": raw_item.get("provenance_complete"),
                "catalog_training": raw_item.get("training"),
            }
        )

    candidates.sort(key=lambda item: (item["source_file_uri"].casefold(), item["source_file_uri"]))
    if source_scope.strict and not candidates:
        raise SourceScopeError("strict source_scope selected no point-cloud sources")
    sources: list[dict[str, Any]] = []
    snapshots: list[tuple[Path, _SourceStat]] = []
    for source_file_index, candidate in enumerate(candidates):
        path = candidate["path"]
        snapshot = _SourceStat.from_stat_result(path.stat())
        digest = _source_digest(path, snapshot, hash_cache)
        source_type = str(candidate["source_type"])
        authoritative_type = source_type in _AUTHORITATIVE_SOURCE_TYPES
        catalog_complete = candidate["catalog_provenance_complete"]
        provenance_complete = bool(
            authoritative_type
            and (True if catalog_complete is None else catalog_complete is True)
        )
        catalog_training = candidate["catalog_training"]
        training = bool(
            provenance_complete
            and (True if catalog_training is None else catalog_training is True)
        )
        if source_type == "pcdb":
            provenance_complete = False
            training = False
        sources.append(
            {
                "source_file_index": source_file_index,
                "source_file_uri": candidate["source_file_uri"],
                "source_file_sha256": digest,
                "file_size": snapshot.file_size,
                "mtime_ns": snapshot.mtime_ns,
                "source_type": source_type,
                "job_id": candidate["job_id"],
                "track_id": candidate["track_id"],
                "provenance_complete": provenance_complete,
                "training": training,
            }
        )
        snapshots.append((path, snapshot))

    # Catch a source that changes after it was hashed while a later file is
    # still being processed.  No final inventory may mix two source revisions.
    for path, expected in snapshots:
        current = _SourceStat.from_stat_result(path.stat())
        if current != expected:
            raise SourceMutationError(
                f"source changed before inventory completion: {path}"
            )

    inventory = {
        "schema": SOURCE_INVENTORY_SCHEMA_NAME,
        "schema_version": OBJECT_CROP_SCHEMA_VERSION,
        "scope_id": source_scope.scope_id,
        "scope": source_scope.to_dict(),
        "source_count": len(sources),
        "sources": sources,
    }
    validate_source_inventory(inventory)
    return inventory


def _validate_source_uri(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SourceInventoryError("source_file_uri must be root-relative POSIX")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in value.split("/")
    ):
        raise SourceInventoryError("source_file_uri must be root-relative POSIX")
    return value


def validate_source_inventory(inventory: Mapping[str, Any]) -> None:
    """Validate the deterministic P0-A inventory payload before publication."""

    if not isinstance(inventory, Mapping):
        raise TypeError("inventory must be a mapping")
    expected_keys = {
        "schema",
        "schema_version",
        "scope_id",
        "scope",
        "source_count",
        "sources",
    }
    if set(inventory) != expected_keys:
        unknown = sorted(set(inventory) - expected_keys)
        missing = sorted(expected_keys - set(inventory))
        raise SourceInventoryError(
            f"invalid inventory keys; missing={missing}, unknown={unknown}"
        )
    if inventory["schema"] != SOURCE_INVENTORY_SCHEMA_NAME:
        raise SourceInventoryError("invalid source inventory schema")
    if inventory["schema_version"] != OBJECT_CROP_SCHEMA_VERSION:
        raise SourceInventoryError("invalid source inventory schema_version")
    scope = SourceScope.from_value(inventory["scope"])
    if inventory["scope_id"] != scope.scope_id:
        raise SourceInventoryError("source inventory scope_id does not match scope")
    sources = inventory["sources"]
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise SourceInventoryError("inventory sources must be a list")
    source_count = inventory["source_count"]
    if type(source_count) is not int or source_count != len(sources):
        raise SourceInventoryError("source_count does not match sources")
    if scope.strict and not scope.jobs:
        raise SourceInventoryError("strict inventory scope requires at least one job")
    if scope.strict and not sources:
        raise SourceInventoryError("strict inventory must contain at least one source")
    previous_sort_key: tuple[str, str] | None = None
    seen_uris: set[str] = set()
    expected_source_keys = {
        "source_file_index",
        "source_file_uri",
        "source_file_sha256",
        "file_size",
        "mtime_ns",
        "source_type",
        "job_id",
        "track_id",
        "provenance_complete",
        "training",
    }
    for index, raw_source in enumerate(sources):
        if not isinstance(raw_source, Mapping) or set(raw_source) != expected_source_keys:
            raise SourceInventoryError(f"invalid source inventory entry at index {index}")
        if (
            isinstance(raw_source["source_file_index"], bool)
            or not isinstance(raw_source["source_file_index"], int)
            or raw_source["source_file_index"] != index
        ):
            raise SourceInventoryError("source_file_index must be contiguous and zero-based")
        uri = _validate_source_uri(raw_source["source_file_uri"])
        sort_key = (uri.casefold(), uri)
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise SourceInventoryError("sources must be ordered by source_file_uri")
        previous_sort_key = sort_key
        if uri in seen_uris:
            raise SourceInventoryError("source_file_uri values must be unique")
        seen_uris.add(uri)
        digest = raw_source["source_file_sha256"]
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise SourceInventoryError("source_file_sha256 is not a lowercase SHA-256")
        for name in ("file_size", "mtime_ns"):
            value = raw_source[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SourceInventoryError(f"{name} must be a non-negative integer")
        for name in ("provenance_complete", "training"):
            if not isinstance(raw_source[name], bool):
                raise SourceInventoryError(f"{name} must be boolean")
        source_type = _normalize_source_type(raw_source["source_type"])
        uri_source_type = _path_source_type(Path(uri))
        if source_type != uri_source_type:
            raise SourceInventoryError(
                "inventory source_type does not match source_file_uri extension"
            )
        for name in ("job_id", "track_id"):
            if not isinstance(raw_source[name], str) or not raw_source[name]:
                raise SourceInventoryError(f"{name} must be a non-empty string")
        if not scope.allows(raw_source["job_id"], raw_source["track_id"]):
            raise SourceInventoryError("source identity is outside the declared scope")
        if raw_source["training"] and not raw_source["provenance_complete"]:
            raise SourceInventoryError("training source must have complete provenance")
        if raw_source["training"] and source_type not in _AUTHORITATIVE_SOURCE_TYPES:
            raise SourceInventoryError("training source type is not authoritative")
        if source_type == "pcdb" and (
            raw_source["provenance_complete"] or raw_source["training"]
        ):
            raise SourceInventoryError("PCDB provenance/training must be false")


def write_source_inventory(
    inventory: Mapping[str, Any],
    output_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate and atomically write canonical inventory JSON.

    The returned digest covers the exact persisted bytes, including the final
    newline, so callers can place it directly in a run manifest.
    """

    validate_source_inventory(inventory)
    payload = canonical_json_bytes(dict(inventory)) + b"\n"
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        if os.name != "nt":
            try:
                parent_descriptor = os.open(output.parent, os.O_RDONLY)
            except OSError:
                parent_descriptor = -1
            if parent_descriptor >= 0:
                try:
                    os.fsync(parent_descriptor)
                finally:
                    os.close(parent_descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(output),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


__all__ = [
    "HashCache",
    "SourceHashCache",
    "SourceInventoryError",
    "SourceMutationError",
    "SourceScopeError",
    "build_source_inventory",
    "validate_source_inventory",
    "write_source_inventory",
]
