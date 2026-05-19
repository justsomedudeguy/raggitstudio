from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


CLASSIFIER_SCHEMA_FIELDS = (
    "document_type",
    "source_type",
    "title",
    "summary",
    "topics",
    "tags",
    "entities",
    "people",
    "organizations",
    "places",
    "dates",
    "language",
    "domain_or_collection",
    "sensitivity",
    "quality_score",
    "confidence",
)


class SourceMetadata(BaseModel):
    document_type: str = "unknown"
    source_type: str = "unknown"
    title: str = "Untitled"
    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    places: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    language: str = "unknown"
    domain_or_collection: str = ""
    sensitivity: str = "unknown"
    quality_score: float = 0.0
    confidence: float = 0.0

    @field_validator(
        "topics",
        "tags",
        "entities",
        "people",
        "organizations",
        "places",
        "dates",
        mode="before",
    )
    @classmethod
    def normalize_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    @field_validator("quality_score", "confidence", mode="before")
    @classmethod
    def normalize_score(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, parsed))


def normalize_metadata(raw: dict[str, Any] | SourceMetadata | None) -> SourceMetadata:
    if isinstance(raw, SourceMetadata):
        return raw
    raw = raw or {}
    normalized = {field: raw.get(field) for field in CLASSIFIER_SCHEMA_FIELDS if field in raw}
    return SourceMetadata(**normalized)


def classifier_json_schema() -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "name": "source_metadata",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "document_type": {"type": "string"},
                "source_type": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "topics": string_array,
                "tags": string_array,
                "entities": string_array,
                "people": string_array,
                "organizations": string_array,
                "places": string_array,
                "dates": string_array,
                "language": {"type": "string"},
                "domain_or_collection": {"type": "string"},
                "sensitivity": {"type": "string"},
                "quality_score": {"type": "number"},
                "confidence": {"type": "number"},
            },
            "required": list(CLASSIFIER_SCHEMA_FIELDS),
        },
    }

