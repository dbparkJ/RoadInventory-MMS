from __future__ import annotations

import math
import re
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, field_validator

from .datasets import utc_now

router = APIRouter(prefix="/api", tags=["field-survey"])

_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MAX_SURVEY_VERTICES = 5_000


class SurveySegmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "현장조사 필요구간"
    color: str = "#f5c542"
    coordinates: list[tuple[float, float]]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = " ".join(str(value).split())
        if not normalized or len(normalized) > 120:
            raise ValueError("name must contain 1-120 visible characters.")
        return normalized

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if not _COLOR_PATTERN.fullmatch(normalized):
            raise ValueError("color must be a #RRGGBB value.")
        return normalized

    @field_validator("coordinates")
    @classmethod
    def validate_coordinates(
        cls,
        value: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        if not 2 <= len(value) <= _MAX_SURVEY_VERTICES:
            raise ValueError(
                f"coordinates must contain 2-{_MAX_SURVEY_VERTICES} vertices."
            )
        normalized: list[tuple[float, float]] = []
        for longitude, latitude in value:
            longitude = float(longitude)
            latitude = float(latitude)
            if (
                not math.isfinite(longitude)
                or not math.isfinite(latitude)
                or longitude < -180
                or longitude > 180
                or latitude < -90
                or latitude > 90
            ):
                raise ValueError("coordinates must contain finite WGS84 longitude/latitude.")
            normalized.append((longitude, latitude))
        return normalized


def _public_segment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "dataset_id": item["dataset_id"],
        "name": item["name"],
        "color": item["color"],
        "geometry": {
            "type": "LineString",
            "coordinates": item["coordinates"],
        },
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def _require_dataset(request: Request, dataset_id: str) -> None:
    if request.app.state.store.get_dataset(dataset_id) is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")


@router.get("/datasets/{dataset_id}/survey-segments")
def list_survey_segments(dataset_id: str, request: Request) -> dict[str, Any]:
    _require_dataset(request, dataset_id)
    items = [
        _public_segment(item)
        for item in request.app.state.store.list_survey_segments(dataset_id)
    ]
    return {"items": items}


@router.post(
    "/datasets/{dataset_id}/survey-segments",
    status_code=status.HTTP_201_CREATED,
)
def create_survey_segment(
    dataset_id: str,
    payload: SurveySegmentCreate,
    request: Request,
) -> dict[str, Any]:
    _require_dataset(request, dataset_id)
    now = utc_now()
    segment = request.app.state.store.create_survey_segment(
        {
            "id": f"survey_{uuid.uuid4().hex}",
            "dataset_id": dataset_id,
            "name": payload.name,
            "color": payload.color,
            "coordinates": [list(coordinate) for coordinate in payload.coordinates],
            "created_at": now,
            "updated_at": now,
        }
    )
    return {"segment": _public_segment(segment)}


@router.delete("/datasets/{dataset_id}/survey-segments/{segment_id}")
def delete_survey_segment(
    dataset_id: str,
    segment_id: str,
    request: Request,
) -> dict[str, Any]:
    _require_dataset(request, dataset_id)
    if not request.app.state.store.delete_survey_segment(dataset_id, segment_id):
        raise HTTPException(status_code=404, detail="Survey segment not found.")
    return {"id": segment_id, "deleted": True}
