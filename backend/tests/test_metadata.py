import pytest

from customchat.metadata import CLASSIFIER_SCHEMA_FIELDS, SourceMetadata, normalize_metadata


def test_normalize_metadata_preserves_hybrid_fields():
    raw = {
        "document_type": "court docket",
        "source_type": "web",
        "title": "Smith v Jones docket",
        "summary": "A docket page with case filings.",
        "topics": ["civil litigation", "filings"],
        "tags": ["docket", "case"],
        "entities": ["Smith v Jones"],
        "people": ["Smith", "Jones"],
        "organizations": ["Superior Court"],
        "places": ["Massachusetts"],
        "dates": ["2026-01-04"],
        "language": "en",
        "domain_or_collection": "courts",
        "sensitivity": "public",
        "quality_score": 0.83,
        "confidence": 0.91,
    }

    metadata = normalize_metadata(raw)

    assert isinstance(metadata, SourceMetadata)
    assert metadata.document_type == "court docket"
    assert metadata.topics == ["civil litigation", "filings"]
    assert metadata.entities == ["Smith v Jones"]
    assert metadata.quality_score == pytest.approx(0.83)
    assert metadata.confidence == pytest.approx(0.91)
    assert set(CLASSIFIER_SCHEMA_FIELDS).issubset(metadata.model_dump().keys())


def test_normalize_metadata_defaults_missing_fields():
    metadata = normalize_metadata({"title": "Untitled note"})

    assert metadata.title == "Untitled note"
    assert metadata.document_type == "unknown"
    assert metadata.source_type == "unknown"
    assert metadata.topics == []
    assert metadata.sensitivity == "unknown"
    assert metadata.quality_score == 0.0
    assert metadata.confidence == 0.0

