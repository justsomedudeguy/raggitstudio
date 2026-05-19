from customchat.database import Database
from customchat.metadata import SourceMetadata


def test_database_initializes_required_tables(tmp_path):
    db = Database(tmp_path / "rag.db")
    db.initialize()

    tables = set(db.table_names())

    assert {
        "sources",
        "source_artifacts",
        "documents",
        "chunks",
        "embeddings",
        "metadata_tags",
        "collections",
        "ingestion_jobs",
        "retrieval_runs",
        "conversations",
        "messages",
        "tool_runs",
        "attachments",
    }.issubset(tables)


def test_database_persists_source_document_chunk_and_embedding(tmp_path):
    db = Database(tmp_path / "rag.db")
    db.initialize()

    source_id = db.upsert_source(
        source_type="text",
        uri="memory://note",
        title="Note",
        content_hash_value="abc123",
        status="ready",
    )
    document_id = db.insert_document(
        source_id=source_id,
        title="Note",
        text="Useful local note.",
        metadata=SourceMetadata(title="Note", tags=["local"]),
    )
    chunk_id = db.insert_chunk(
        document_id=document_id,
        source_id=source_id,
        chunk_index=0,
        text="Useful local note.",
        citation_id="D1-C0",
        metadata=SourceMetadata(title="Note", tags=["local"]),
    )
    db.insert_embedding(chunk_id=chunk_id, model_id="embedder", vector=[1.0, 2.0, 3.0])

    stored = db.get_chunk(chunk_id)

    assert stored["text"] == "Useful local note."
    assert stored["metadata"]["tags"] == ["local"]
    assert db.count_rows("embeddings") == 1

