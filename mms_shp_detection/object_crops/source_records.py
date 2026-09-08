"""Row-aligned source-record contracts used by object-crop stages.

The legacy point-cloud reader continues to return an array-only mapping.  This
module wraps that mapping with explicit file provenance without changing the
reader API, and supplies helpers that apply every selection to every row array.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

import numpy as np


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_source_file_uri(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("source_file_uri must be a non-empty string")
    if "\\" in value:
        raise ValueError("source_file_uri must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in value.split("/")
    ):
        raise ValueError("source_file_uri must be a safe root-relative POSIX path")
    return value


def _as_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise TypeError(f"{field_name} must be an integer")
    result = int(value)
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _record_point_count(records: Mapping[str, np.ndarray]) -> int:
    xyz = records.get("xyz")
    if not isinstance(xyz, np.ndarray) or xyz.ndim != 2 or xyz.shape[1] != 3:
        raise ValueError("records['xyz'] must have shape (N, 3)")
    return int(xyz.shape[0])


def _normalize_availability(
    value: Mapping[str, bool | np.ndarray] | None,
    point_count: int,
) -> Mapping[str, bool | np.ndarray]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("field_availability must be a mapping")
    normalized: dict[str, bool | np.ndarray] = {}
    for raw_name, raw_available in value.items():
        name = str(raw_name)
        if isinstance(raw_available, (bool, np.bool_)):
            normalized[name] = bool(raw_available)
            continue
        array = np.asarray(raw_available)
        if array.dtype.kind != "b" or array.shape != (point_count,):
            raise ValueError(
                f"field_availability[{name!r}] must be bool or shape (N,) bool"
            )
        normalized[name] = array
    return MappingProxyType(normalized)


def _normalize_metadata(
    value: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Mapping[str, Any]]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("field_metadata must be a mapping")
    normalized: dict[str, Mapping[str, Any]] = {}
    for raw_name, raw_metadata in value.items():
        if not isinstance(raw_metadata, Mapping):
            raise TypeError(f"field_metadata[{raw_name!r}] must be a mapping")
        normalized[str(raw_name)] = MappingProxyType(dict(raw_metadata))
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class SourcePointBatch:
    """One source file/block worth of aligned point records and provenance."""

    records: Mapping[str, np.ndarray]
    source_file_uri: str
    source_file_index: int
    source_file_sha256: str
    block_start: int
    field_availability: Mapping[str, bool | np.ndarray] = field(default_factory=dict)
    field_metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.records, Mapping):
            raise TypeError("records must be a mapping of row-aligned ndarrays")
        records = dict(self.records)
        point_count = _record_point_count(records)
        for name, array in records.items():
            if not isinstance(name, str) or not isinstance(array, np.ndarray):
                raise TypeError("records must map string names to ndarrays")
            if array.ndim == 0 or int(array.shape[0]) != point_count:
                raise ValueError(f"records[{name!r}] is not aligned to N={point_count}")
        source_index = records.get("source_index")
        if source_index is None or source_index.shape != (point_count,):
            raise ValueError("records['source_index'] must have shape (N,)")
        if source_index.dtype.kind not in {"i", "u"}:
            raise ValueError("records['source_index'] must have an integer dtype")
        source_file_index = _as_nonnegative_int(
            self.source_file_index,
            "source_file_index",
        )
        block_start = _as_nonnegative_int(self.block_start, "block_start")
        if not isinstance(self.source_file_sha256, str) or not _SHA256_PATTERN.fullmatch(
            self.source_file_sha256
        ):
            raise ValueError("source_file_sha256 must be a lowercase SHA-256 hex digest")

        object.__setattr__(self, "records", MappingProxyType(records))
        object.__setattr__(self, "source_file_index", source_file_index)
        object.__setattr__(self, "block_start", block_start)
        object.__setattr__(
            self,
            "source_file_uri",
            _validate_source_file_uri(self.source_file_uri),
        )
        object.__setattr__(
            self,
            "field_availability",
            _normalize_availability(self.field_availability, point_count),
        )
        object.__setattr__(
            self,
            "field_metadata",
            _normalize_metadata(self.field_metadata),
        )

        structured = records.get("field_availability")
        if structured is not None:
            if structured.shape != (point_count,) or structured.dtype.names is None:
                raise ValueError(
                    "records['field_availability'] must be a row-aligned structured array"
                )
            for name in structured.dtype.names:
                if structured.dtype[name].kind != "b":
                    raise ValueError(
                        "records['field_availability'] fields must have bool dtype"
                    )

    @property
    def point_count(self) -> int:
        return int(self.records["xyz"].shape[0])

    def availability_for(self, field_name: str) -> np.ndarray:
        """Return a row-level availability mask, defaulting fail-closed."""

        explicit = self.field_availability.get(field_name)
        if explicit is not None:
            if isinstance(explicit, bool):
                return np.full(self.point_count, explicit, dtype=np.bool_)
            return np.asarray(explicit, dtype=np.bool_)
        structured = self.records.get("field_availability")
        if structured is not None and structured.dtype.names is not None:
            if field_name in structured.dtype.names:
                return np.asarray(structured[field_name], dtype=np.bool_)
        return np.zeros(self.point_count, dtype=np.bool_)

    def take(self, mask_or_indices: Any) -> "SourcePointBatch":
        return take_records(self, mask_or_indices)


def _validated_selection(mask_or_indices: Any, point_count: int) -> np.ndarray:
    selection = np.asarray(mask_or_indices)
    if selection.ndim != 1:
        raise ValueError("record selection must be one-dimensional")
    if selection.dtype.kind == "b":
        if selection.shape != (point_count,):
            raise ValueError("boolean record selection must have shape (N,)")
        return selection
    if selection.size == 0:
        return np.asarray([], dtype=np.int64)
    if selection.dtype.kind not in {"i", "u"}:
        raise TypeError("record selection must contain booleans or integer indices")
    indices = selection.astype(np.int64, copy=False)
    if np.any(indices < 0) or np.any(indices >= point_count):
        raise IndexError("record selection index is out of bounds")
    return indices


def take_records(batch: SourcePointBatch, mask_or_indices: Any) -> SourcePointBatch:
    """Apply the same selection to every row-aligned record and validity array."""

    if not isinstance(batch, SourcePointBatch):
        raise TypeError("batch must be a SourcePointBatch")
    selection = _validated_selection(mask_or_indices, batch.point_count)
    records = {name: array[selection] for name, array in batch.records.items()}
    availability = {
        name: value if isinstance(value, bool) else value[selection]
        for name, value in batch.field_availability.items()
    }
    selected_source_indices = np.asarray(records["source_index"], dtype=np.int64)
    nonnegative = selected_source_indices[selected_source_indices >= 0]
    block_start = (
        int(nonnegative.min())
        if nonnegative.size
        else batch.block_start
    )
    return SourcePointBatch(
        records=records,
        source_file_uri=batch.source_file_uri,
        source_file_index=batch.source_file_index,
        source_file_sha256=batch.source_file_sha256,
        block_start=block_start,
        field_availability=availability,
        field_metadata=batch.field_metadata,
    )


def concat_record_batches(batches: Sequence[SourcePointBatch]) -> SourcePointBatch:
    """Concatenate blocks from the same source while preserving every field."""

    if not batches:
        raise ValueError("at least one SourcePointBatch is required")
    first = batches[0]
    if not all(isinstance(batch, SourcePointBatch) for batch in batches):
        raise TypeError("all batches must be SourcePointBatch instances")
    record_names = tuple(first.records)
    record_name_set = set(record_names)
    availability_names = tuple(first.field_availability)
    availability_name_set = set(availability_names)
    for batch in batches[1:]:
        if (
            batch.source_file_uri != first.source_file_uri
            or batch.source_file_index != first.source_file_index
            or batch.source_file_sha256 != first.source_file_sha256
        ):
            raise ValueError("record batches from different source files cannot be concatenated")
        if set(batch.records) != record_name_set:
            raise ValueError("record batches must contain the same fields")
        if set(batch.field_availability) != availability_name_set:
            raise ValueError("record batches must contain the same availability fields")
        if batch.field_metadata != first.field_metadata:
            raise ValueError("record batches must contain the same field metadata")
        for name in record_names:
            if (
                batch.records[name].dtype != first.records[name].dtype
                or batch.records[name].shape[1:] != first.records[name].shape[1:]
            ):
                raise ValueError(f"record field {name!r} has incompatible dtype/shape")

    records = {
        name: np.concatenate([batch.records[name] for batch in batches], axis=0)
        for name in record_names
    }
    availability: dict[str, bool | np.ndarray] = {}
    for name in availability_names:
        values = [batch.field_availability[name] for batch in batches]
        if all(isinstance(value, bool) for value in values):
            if len(set(values)) != 1:
                availability[name] = np.concatenate(
                    [
                        np.full(batch.point_count, bool(value), dtype=np.bool_)
                        for batch, value in zip(batches, values, strict=True)
                    ]
                )
            else:
                availability[name] = bool(values[0])
        else:
            availability[name] = np.concatenate(
                [
                    np.full(batch.point_count, value, dtype=np.bool_)
                    if isinstance(value, bool)
                    else value
                    for batch, value in zip(batches, values, strict=True)
                ]
            )

    return SourcePointBatch(
        records=records,
        source_file_uri=first.source_file_uri,
        source_file_index=first.source_file_index,
        source_file_sha256=first.source_file_sha256,
        block_start=min(batch.block_start for batch in batches),
        field_availability=availability,
        field_metadata=first.field_metadata,
    )


def dedupe_source_records(batch: SourcePointBatch) -> SourcePointBatch:
    """Keep the first row for each available source record index, stably.

    Negative indices denote unavailable source identity (for example PCDB) and
    are intentionally retained instead of being collapsed into one point.
    """

    indices = np.asarray(batch.records["source_index"], dtype=np.int64)
    seen: set[int] = set()
    keep: list[int] = []
    for row, source_index in enumerate(indices.tolist()):
        if source_index < 0 or source_index not in seen:
            keep.append(row)
        if source_index >= 0:
            seen.add(source_index)
    if len(keep) == batch.point_count:
        return batch
    return take_records(batch, np.asarray(keep, dtype=np.int64))


def source_record_keys(batch: SourcePointBatch) -> np.ndarray:
    """Return the authoritative ``(source_file_index, source_index)`` tuples."""

    keys = np.empty((batch.point_count, 2), dtype=np.int64)
    keys[:, 0] = batch.source_file_index
    keys[:, 1] = np.asarray(batch.records["source_index"], dtype=np.int64)
    return keys


__all__ = [
    "SourcePointBatch",
    "concat_record_batches",
    "dedupe_source_records",
    "source_record_keys",
    "take_records",
]
