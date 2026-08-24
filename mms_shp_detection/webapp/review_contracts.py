"""P1 review-workspace domain contracts.

These models intentionally have no router or persistence side effects.  They
freeze the Step 0 JSON shape so the later API and SQLite migrations can be
implemented behind ``capabilities.review_workspace`` without changing P0.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OpaqueId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
ReasonCode = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$"),
]
NormalizedCoordinate = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]
FiniteCoordinate = Annotated[float, Field(allow_inf_nan=False)]


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReviewSessionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ReviewTaskType(str, Enum):
    MANUAL_SCAN = "MANUAL_SCAN"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    PROJECTION_FAILED = "PROJECTION_FAILED"
    GEOMETRY_REVIEW = "GEOMETRY_REVIEW"
    POLE_BASE_REVIEW = "POLE_BASE_REVIEW"
    SPACING_ANOMALY = "SPACING_ANOMALY"
    UNREVIEWED_INTERVAL = "UNREVIEWED_INTERVAL"
    MANUAL_FLAG = "MANUAL_FLAG"


class ReviewTaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    MANUAL_ADDED = "manual_added"
    FALSE_POSITIVE = "false_positive"
    SKIPPED = "skipped"
    FIELD_SURVEY = "field_survey"


class ReviewTaskResolution(str, Enum):
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    MANUAL_ADDED = "manual_added"
    FALSE_POSITIVE = "false_positive"
    SKIPPED = "skipped"
    FIELD_SURVEY = "field_survey"


class ProposalStatus(str, Enum):
    FAILED = "failed"
    REVIEW = "review"
    AUTO = "auto"


class FeatureOrigin(str, Enum):
    AI = "AI"
    MANUAL = "MANUAL"
    CORRECTED = "CORRECTED"


class QaIssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class QaIssueStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class FeatureReviewStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    MANUAL_ADDED = "manual_added"
    FALSE_POSITIVE = "false_positive"
    SKIPPED = "skipped"
    FIELD_SURVEY = "field_survey"


class ReviewSession(_ContractModel):
    id: OpaqueId
    dataset_id: OpaqueId
    source_run_ids: list[OpaqueId] = Field(default_factory=list)
    target_layer_ids: list[OpaqueId] = Field(default_factory=list)
    track_ids: list[str] = Field(default_factory=list)
    frame_range: tuple[int, int] | None
    class_filters: list[ReasonCode] = Field(default_factory=list)
    status: ReviewSessionStatus
    created_by: str = Field(min_length=1, max_length=160)
    created_at: datetime
    updated_at: datetime
    last_task_id: OpaqueId | None = None
    qa_layer_revisions: dict[str, int] | None = None
    qa_ran_at: datetime | None = None

    @field_validator("frame_range")
    @classmethod
    def _ordered_frame_range(
        cls, value: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        if value is None:
            return None
        if value[0] < 0 or value[1] < value[0]:
            raise ValueError("frame_range must be an ordered, non-negative pair.")
        return value


class ReviewSessionCreate(_ContractModel):
    source_run_ids: list[OpaqueId] = Field(default_factory=list, max_length=100)
    target_layer_ids: list[OpaqueId] = Field(default_factory=list, max_length=100)
    track_ids: list[str] = Field(default_factory=list, max_length=100)
    frame_range: tuple[int, int] | None = None
    class_filters: list[ReasonCode] = Field(default_factory=list, max_length=100)
    status: ReviewSessionStatus = ReviewSessionStatus.DRAFT
    created_by: str = Field(default="operator-local", min_length=1, max_length=160)

    @field_validator("frame_range")
    @classmethod
    def _ordered_frame_range(
        cls, value: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        if value is None:
            return None
        if value[0] < 0 or value[1] < value[0]:
            raise ValueError("frame_range must be an ordered, non-negative pair.")
        return value


class ReviewSessionPatch(_ContractModel):
    source_run_ids: list[OpaqueId] | None = Field(None, max_length=100)
    target_layer_ids: list[OpaqueId] | None = Field(None, max_length=100)
    track_ids: list[str] | None = Field(None, max_length=100)
    frame_range: tuple[int, int] | None = None
    class_filters: list[ReasonCode] | None = Field(None, max_length=100)
    status: ReviewSessionStatus | None = None
    last_task_id: OpaqueId | None = None

    @field_validator("frame_range")
    @classmethod
    def _ordered_frame_range(
        cls, value: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        if value is None:
            return None
        if value[0] < 0 or value[1] < value[0]:
            raise ValueError("frame_range must be an ordered, non-negative pair.")
        return value


class ReviewTask(_ContractModel):
    id: OpaqueId
    session_id: OpaqueId
    dataset_id: OpaqueId
    task_type: ReviewTaskType
    status: ReviewTaskStatus
    priority: float = Field(ge=0.0, allow_inf_nan=False)
    frame_id: OpaqueId | None = None
    track_id: str | None = Field(None, min_length=1, max_length=160)
    frame_start: int | None = Field(None, ge=0)
    frame_end: int | None = Field(None, ge=0)
    source_run_id: OpaqueId | None = None
    source_detection_id: OpaqueId | None = None
    target_layer_id: OpaqueId | None = None
    class_hint: ReasonCode | None = None
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    location_hint: (
        tuple[FiniteCoordinate, FiniteCoordinate, FiniteCoordinate] | None
    ) = None
    source_fingerprint: OpaqueId | None = None
    priority_evidence: dict[str, Any] = Field(default_factory=dict)
    claimed_by: str | None = Field(None, min_length=1, max_length=160)
    resolved_feature_ids: list[OpaqueId] = Field(default_factory=list)
    resolution: ReviewTaskResolution | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _ordered_frame_span(self) -> ReviewTask:
        if (self.frame_start is None) != (self.frame_end is None):
            raise ValueError("frame_start and frame_end must be supplied together.")
        if (
            self.frame_start is not None
            and self.frame_end is not None
            and self.frame_end < self.frame_start
        ):
            raise ValueError("frame_start and frame_end must be an ordered pair.")
        return self


class ReviewTaskCreate(_ContractModel):
    task_type: ReviewTaskType = ReviewTaskType.MANUAL_SCAN
    priority: float = Field(default=50.0, ge=0.0, allow_inf_nan=False)
    frame_id: OpaqueId | None = None
    track_id: str | None = Field(None, min_length=1, max_length=160)
    frame_start: int | None = Field(None, ge=0)
    frame_end: int | None = Field(None, ge=0)
    source_run_id: OpaqueId | None = None
    source_detection_id: OpaqueId | None = None
    target_layer_id: OpaqueId | None = None
    class_hint: ReasonCode | None = None
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=100)
    location_hint: (
        tuple[
            FiniteCoordinate,
            FiniteCoordinate,
            FiniteCoordinate,
        ]
        | None
    ) = None
    claimed_by: str | None = Field(None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def _ordered_frame_span(self) -> ReviewTaskCreate:
        if (self.frame_start is None) != (self.frame_end is None):
            raise ValueError("frame_start and frame_end must be supplied together.")
        if (
            self.frame_start is not None
            and self.frame_end is not None
            and self.frame_end < self.frame_start
        ):
            raise ValueError("frame_start and frame_end must be an ordered pair.")
        return self


class ReviewCandidateSources(_ContractModel):
    low_confidence: bool = True
    projection_failed: bool = True
    geometry_review: bool = True
    pole_base_review: bool = True
    unreviewed_interval: bool = True
    spacing_anomaly: bool = True


class ReviewTaskGenerateRequest(_ContractModel):
    tasks: list[ReviewTaskCreate] = Field(default_factory=list, max_length=500)
    sources: ReviewCandidateSources = Field(default_factory=ReviewCandidateSources)
    low_confidence_threshold: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    unreviewed_interval_frames: int = Field(default=50, ge=1, le=500)


class ReviewTaskPatch(_ContractModel):
    status: ReviewTaskStatus | None = None
    priority: float | None = Field(None, ge=0.0, allow_inf_nan=False)
    claimed_by: str | None = Field(None, min_length=1, max_length=160)


class ReviewTaskResolve(_ContractModel):
    resolution: ReviewTaskResolution
    resolved_feature_ids: list[OpaqueId] = Field(default_factory=list, max_length=500)


class EquirectangularBbox(_ContractModel):
    type: Literal["equirectangular_bbox"]
    u_intervals: list[tuple[NormalizedCoordinate, NormalizedCoordinate]] = Field(
        min_length=1,
        max_length=2,
    )
    v_min: NormalizedCoordinate
    v_max: NormalizedCoordinate
    image_width: int = Field(gt=0, le=100_000)
    image_height: int = Field(gt=0, le=100_000)

    @model_validator(mode="after")
    def _valid_region(self) -> EquirectangularBbox:
        if self.v_min >= self.v_max:
            raise ValueError("v_min must be less than v_max.")
        ordered = sorted(self.u_intervals)
        for left, right in ordered:
            if left >= right:
                raise ValueError("Each U interval must have positive width.")
        for previous, current in pairwise(ordered):
            if current[0] < previous[1]:
                raise ValueError("U intervals must not overlap.")
        if len(ordered) == 2 and not (ordered[0][0] == 0.0 and ordered[1][1] == 1.0):
            raise ValueError(
                "Two U intervals are reserved for a bbox crossing the panorama seam."
            )
        return self


class ManualObservation(_ContractModel):
    observation_id: OpaqueId
    dataset_id: OpaqueId
    frame_id: OpaqueId
    view_type: Literal["panorama"]
    class_name: ReasonCode
    geometry_2d: EquirectangularBbox
    created_by: str = Field(min_length=1, max_length=160)


class PointGeometry(_ContractModel):
    type: Literal["Point"]
    coordinates: tuple[FiniteCoordinate, FiniteCoordinate, FiniteCoordinate]


class ProposalQuality(_ContractModel):
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    support_point_count: int | None = Field(None, ge=0)
    depth_spread_m: float | None = Field(None, ge=0.0, allow_inf_nan=False)
    reprojection_error_px: float | None = Field(None, ge=0.0, allow_inf_nan=False)


class ProposalEvidence(_ContractModel):
    frame_id: OpaqueId
    observation_id: OpaqueId | None = None
    seed_position: (
        tuple[FiniteCoordinate, FiniteCoordinate, FiniteCoordinate] | None
    ) = None


class GeometryProposal(_ContractModel):
    proposal_id: OpaqueId
    tool_id: OpaqueId
    status: ProposalStatus
    coordinate_space: Literal["dataset"]
    geometry: PointGeometry | None
    property_patch: dict[str, Any] = Field(default_factory=dict)
    quality: ProposalQuality
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    evidence: ProposalEvidence

    @model_validator(mode="after")
    def _geometry_matches_status(self) -> GeometryProposal:
        if self.status is ProposalStatus.FAILED and self.geometry is not None:
            raise ValueError("A failed proposal must not expose committable geometry.")
        if self.status is not ProposalStatus.FAILED and self.geometry is None:
            raise ValueError("A review or auto proposal requires geometry.")
        return self


class FeatureProvenance(_ContractModel):
    layer_id: OpaqueId
    feature_id: OpaqueId
    origin: FeatureOrigin
    source_run_id: OpaqueId | None = None
    source_frame_ids: list[OpaqueId] = Field(default_factory=list)
    source_detection_ids: list[OpaqueId] = Field(default_factory=list)
    manual_observation_ids: list[OpaqueId] = Field(default_factory=list)
    creation_tool: OpaqueId
    proposal_quality: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    review_status: FeatureReviewStatus
    created_by: str = Field(min_length=1, max_length=160)
    created_at: datetime
    updated_at: datetime


class QaIssue(_ContractModel):
    id: OpaqueId
    session_id: OpaqueId
    layer_id: OpaqueId
    feature_id: OpaqueId | None = None
    rule_id: ReasonCode
    severity: QaIssueSeverity
    message: str = Field(min_length=1, max_length=2_000)
    related_feature_ids: list[OpaqueId] = Field(default_factory=list)
    status: QaIssueStatus


__all__ = [
    "EquirectangularBbox",
    "FeatureOrigin",
    "FeatureProvenance",
    "FeatureReviewStatus",
    "GeometryProposal",
    "ManualObservation",
    "PointGeometry",
    "ProposalEvidence",
    "ProposalQuality",
    "ProposalStatus",
    "QaIssue",
    "QaIssueSeverity",
    "QaIssueStatus",
    "ReviewCandidateSources",
    "ReviewSession",
    "ReviewSessionCreate",
    "ReviewSessionPatch",
    "ReviewSessionStatus",
    "ReviewTask",
    "ReviewTaskCreate",
    "ReviewTaskGenerateRequest",
    "ReviewTaskPatch",
    "ReviewTaskResolution",
    "ReviewTaskResolve",
    "ReviewTaskStatus",
    "ReviewTaskType",
]
