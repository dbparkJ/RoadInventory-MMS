"""Record-preserving object-crop backend.

P0-A freezes contracts, source scope/inventory, and row-preserving helpers.
The remaining modules are intentionally import-safe stage boundaries until
their gated implementation phases.
"""

from .contracts import (
    OBJECT_CROP_SCHEMA_NAME,
    OBJECT_CROP_SCHEMA_VERSION,
    SOURCE_INVENTORY_SCHEMA_NAME,
    JobTrackScope,
    ObjectCropCanonicalizationConfig,
    ObjectCropConfig,
    ObjectCropOutputConfig,
    ObjectCropRayConfig,
    ObjectCropRoutingConfig,
    ObjectCropSelectionConfig,
    SourceScope,
    canonical_json_bytes,
    parse_object_crop_config,
)
from .source_inventory import (
    SourceHashCache,
    SourceInventoryError,
    SourceMutationError,
    SourceScopeError,
    build_source_inventory,
    validate_source_inventory,
    write_source_inventory,
)
from .source_records import (
    SourcePointBatch,
    concat_record_batches,
    dedupe_source_records,
    source_record_keys,
    take_records,
)
from .vocabulary import (
    AvailabilityBit,
    GeometryFamily,
    PointRole,
    ReasonCode,
    SampleStatus,
)


__all__ = [
    "AvailabilityBit",
    "GeometryFamily",
    "JobTrackScope",
    "OBJECT_CROP_SCHEMA_NAME",
    "OBJECT_CROP_SCHEMA_VERSION",
    "ObjectCropCanonicalizationConfig",
    "ObjectCropConfig",
    "ObjectCropOutputConfig",
    "ObjectCropRayConfig",
    "ObjectCropRoutingConfig",
    "ObjectCropSelectionConfig",
    "PointRole",
    "ReasonCode",
    "SOURCE_INVENTORY_SCHEMA_NAME",
    "SampleStatus",
    "SourceHashCache",
    "SourceInventoryError",
    "SourceMutationError",
    "SourcePointBatch",
    "SourceScope",
    "SourceScopeError",
    "build_source_inventory",
    "canonical_json_bytes",
    "concat_record_batches",
    "dedupe_source_records",
    "parse_object_crop_config",
    "source_record_keys",
    "take_records",
    "validate_source_inventory",
    "write_source_inventory",
]
