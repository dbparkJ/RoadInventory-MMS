"""Frozen vocabulary for the record-preserving object-crop schema.

Only vocabulary needed to describe the P0 contract lives here.  Later stages
may consume these values, but must not silently rename an existing value.
"""

from __future__ import annotations

from enum import Enum, IntFlag


class _StringEnum(str, Enum):
    """A JSON-friendly string enum with stable ``str()`` behaviour."""

    def __str__(self) -> str:
        return self.value


class PointRole(_StringEnum):
    """Fitted L0 role assigned to a selected source point."""

    PANEL = "panel"
    POLE = "pole"
    GROUND = "ground"
    CONTEXT = "context"
    CLUTTER = "clutter"
    UNKNOWN = "unknown"


class GeometryFamily(_StringEnum):
    """Minimum geometry routing families required by the P0 specification."""

    DIRECT_SINGLE_POLE_SINGLE_PANEL = "direct_single_pole_single_panel"
    REMOTE_MOUNTED_PANEL = "remote_mounted_panel"
    CANTILEVER = "cantilever"
    MULTI_PANEL = "multi_panel"
    MULTI_POLE = "multi_pole"
    FALSE_POSITIVE = "false_positive"
    UNKNOWN = "unknown"


class SampleStatus(_StringEnum):
    """Publication/review disposition for one object-crop sample."""

    ACCEPTED = "accepted"
    REVIEW = "review"
    REJECTED = "rejected"


class ReasonCode(_StringEnum):
    """Stable machine-readable reasons shared by later object-crop stages."""

    SOURCE_SCOPE_EMPTY = "SOURCE_SCOPE_EMPTY"
    SOURCE_IDENTITY_UNAVAILABLE = "SOURCE_IDENTITY_UNAVAILABLE"
    SOURCE_OUT_OF_SCOPE = "SOURCE_OUT_OF_SCOPE"
    SOURCE_MUTATED = "SOURCE_MUTATED"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    PCDB_PROVENANCE_INCOMPLETE = "PCDB_PROVENANCE_INCOMPLETE"
    RAW_ATTRIBUTE_UNAVAILABLE = "RAW_ATTRIBUTE_UNAVAILABLE"
    SENSOR_RAYS_UNAVAILABLE = "SENSOR_RAYS_UNAVAILABLE"
    CANONICALIZATION_INSUFFICIENT = "CANONICALIZATION_INSUFFICIENT"
    UNSUPPORTED_GEOMETRY_FAMILY = "UNSUPPORTED_GEOMETRY_FAMILY"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class AvailabilityBit(IntFlag):
    """Bit assignments for raw LAS record-field availability summaries."""

    NONE = 0
    XYZ = 1 << 0
    RGB = 1 << 1
    RGB_RAW = 1 << 2
    INTENSITY = 1 << 3
    CLASSIFICATION = 1 << 4
    GPS_TIME = 1 << 5
    GPS_TIME_TYPE = 1 << 6
    RETURN_NUMBER = 1 << 7
    NUMBER_OF_RETURNS = 1 << 8
    POINT_SOURCE_ID = 1 << 9
    SCAN_ANGLE = 1 << 10
    SCAN_ANGLE_RANK = 1 << 11
    USER_DATA = 1 << 12
    EDGE_OF_FLIGHT_LINE = 1 << 13
    SCAN_DIRECTION_FLAG = 1 << 14
    SOURCE_INDEX = 1 << 15


POINT_ROLE_VALUES = tuple(item.value for item in PointRole)
GEOMETRY_FAMILY_VALUES = tuple(item.value for item in GeometryFamily)
SAMPLE_STATUS_VALUES = tuple(item.value for item in SampleStatus)
REASON_CODE_VALUES = tuple(item.value for item in ReasonCode)


__all__ = [
    "AvailabilityBit",
    "GEOMETRY_FAMILY_VALUES",
    "GeometryFamily",
    "POINT_ROLE_VALUES",
    "PointRole",
    "REASON_CODE_VALUES",
    "ReasonCode",
    "SAMPLE_STATUS_VALUES",
    "SampleStatus",
]
