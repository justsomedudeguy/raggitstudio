from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from customchat.metadata import SourceMetadata, normalize_metadata
from customchat.rag import vector_to_blob


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(SCHEMA)
            self._run_migrations(db)

    def table_names(self) -> list[str]:
        with self.connect() as db:
            rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def count_rows(self, table: str) -> int:
        if table not in set(self.table_names()):
            raise ValueError(f"Unknown table: {table}")
        with self.connect() as db:
            return int(db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])

    def upsert_source(self, source_type: str, uri: str, title: str, content_hash_value: str, status: str) -> int:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO sources(source_type, uri, title, content_hash, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    title=excluded.title,
                    status=excluded.status,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (source_type, uri, title, content_hash_value, status),
            )
            row = db.execute("SELECT id FROM sources WHERE content_hash = ?", (content_hash_value,)).fetchone()
            return int(row["id"])

    def insert_source_artifact(
        self,
        source_id: int,
        artifact_type: str,
        path: str,
        extracted_text_path: str | None = None,
        raw_json: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO source_artifacts(source_id, artifact_type, path, extracted_text_path, raw_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, artifact_type, path, extracted_text_path, _json(raw_json or {})),
            )
            return int(cursor.lastrowid)

    def insert_document(self, source_id: int, title: str, text: str, metadata: SourceMetadata | dict) -> int:
        normalized = normalize_metadata(metadata)
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO documents(source_id, title, text, metadata_json, summary, document_type, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    title,
                    text,
                    _json(normalized.model_dump()),
                    normalized.summary,
                    normalized.document_type,
                    normalized.source_type,
                ),
            )
            document_id = int(cursor.lastrowid)
            self._insert_metadata_tags(db, document_id, normalized)
            return document_id

    def insert_chunk(
        self,
        document_id: int,
        source_id: int,
        chunk_index: int,
        text: str,
        citation_id: str,
        metadata: SourceMetadata | dict,
    ) -> int:
        normalized = normalize_metadata(metadata)
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO chunks(document_id, source_id, chunk_index, text, citation_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (document_id, source_id, chunk_index, text, citation_id, _json(normalized.model_dump())),
            )
            return int(cursor.lastrowid)

    def insert_embedding(self, chunk_id: int, model_id: str, vector: Iterable[float]) -> int:
        values = list(vector)
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO embeddings(chunk_id, model_id, dimensions, vector_blob)
                VALUES (?, ?, ?, ?)
                """,
                (chunk_id, model_id, len(values), vector_to_blob(values)),
            )
            return int(cursor.lastrowid)

    def get_chunk(self, chunk_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is None:
            raise KeyError(f"Chunk not found: {chunk_id}")
        return _row_to_dict(row)

    def get_chunks(self, chunk_ids: list[int]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM chunks WHERE id IN ({placeholders})", chunk_ids).fetchall()
        by_id = {int(row["id"]): _row_to_dict(row) for row in rows}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]

    def all_embeddings(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT embeddings.*, chunks.text, chunks.citation_id, chunks.metadata_json
                FROM embeddings
                JOIN chunks ON chunks.id = embeddings.chunk_id
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM sources ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM documents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def create_ingestion_job(self, job_type: str, payload: dict[str, Any]) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO ingestion_jobs(job_type, status, payload_json) VALUES (?, 'running', ?)",
                (job_type, _json(payload)),
            )
            return int(cursor.lastrowid)

    def update_ingestion_job(
        self,
        job_id: int,
        status: str,
        processed_count: int = 0,
        failed_count: int = 0,
        log: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE ingestion_jobs
                SET status=?, processed_count=?, failed_count=?, log=?, finished_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status, processed_count, failed_count, log, job_id),
            )

    def create_archive_file(self, path: str, file_format: str, fallback_kind: str | None) -> int:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO archive_files(path, file_format, fallback_kind, status)
                VALUES (?, ?, ?, 'running')
                ON CONFLICT(path) DO UPDATE SET
                    file_format=excluded.file_format,
                    fallback_kind=excluded.fallback_kind,
                    status='running',
                    indexed_count=0,
                    failed_count=0,
                    log='',
                    updated_at=CURRENT_TIMESTAMP,
                    finished_at=NULL
                """,
                (path, file_format, fallback_kind),
            )
            row = db.execute("SELECT id FROM archive_files WHERE path = ?", (path,)).fetchone()
            return int(row["id"])

    def update_archive_file(
        self,
        file_id: int,
        status: str,
        indexed_count: int,
        failed_count: int,
        log: str,
        finished: bool = False,
    ) -> None:
        finished_sql = ", finished_at=CURRENT_TIMESTAMP" if finished else ""
        with self.connect() as db:
            db.execute(
                f"""
                UPDATE archive_files
                SET status=?, indexed_count=?, row_count=?, failed_count=?, log=?, updated_at=CURRENT_TIMESTAMP
                    {finished_sql}
                WHERE id=?
                """,
                (status, indexed_count, indexed_count + failed_count, failed_count, log, file_id),
            )

    def list_archive_files(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM archive_files ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def upsert_reddit_item(self, item: dict[str, Any]) -> int:
        self.upsert_reddit_items([item])
        with self.connect() as db:
            row = db.execute("SELECT id FROM reddit_items WHERE item_key = ?", (item["item_key"],)).fetchone()
            return int(row["id"])

    def upsert_reddit_items(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO reddit_items(
                    archive_file_id, line_number, item_key, reddit_id, kind, subreddit, author,
                    created_utc, score, title, selftext, body, url, permalink, link_id, parent_id,
                    text, raw_json, meta_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    archive_file_id=excluded.archive_file_id,
                    line_number=excluded.line_number,
                    reddit_id=excluded.reddit_id,
                    kind=excluded.kind,
                    subreddit=excluded.subreddit,
                    author=excluded.author,
                    created_utc=excluded.created_utc,
                    score=excluded.score,
                    title=excluded.title,
                    selftext=excluded.selftext,
                    body=excluded.body,
                    url=excluded.url,
                    permalink=excluded.permalink,
                    link_id=excluded.link_id,
                    parent_id=excluded.parent_id,
                    text=excluded.text,
                    raw_json=excluded.raw_json,
                    meta_json=excluded.meta_json
                """,
                [_reddit_item_values(item) for item in items],
            )

    def iter_reddit_item_texts(self, kind: str | None = None, subreddit: str | None = None) -> Iterable[dict[str, Any]]:
        where, values = _reddit_filters(kind=kind, subreddit=subreddit)
        with self.connect() as db:
            rows = db.execute(f"SELECT id, kind, text FROM reddit_items {where}", values)
            for row in rows:
                yield _row_to_dict(row)

    def search_reddit_items(
        self,
        query: str,
        limit: int = 20,
        kind: str | None = None,
        subreddit: str | None = None,
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        limit = max(1, min(int(limit), 100))
        where, values = _reddit_filters(kind=kind, subreddit=subreddit, after=after, before=before, prefix="ri")
        with self.connect() as db:
            if query:
                try:
                    rows = db.execute(
                        f"""
                        SELECT ri.*, bm25(reddit_items_fts) AS rank
                        FROM reddit_items_fts
                        JOIN reddit_items ri ON ri.id = reddit_items_fts.rowid
                        {where} {"AND" if where else "WHERE"} reddit_items_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        [*values, _fts_query(query), limit],
                    ).fetchall()
                    return [_row_to_dict(row) for row in rows]
                except sqlite3.OperationalError:
                    like_where = f"{where} {'AND' if where else 'WHERE'} ri.text LIKE ?"
                    rows = db.execute(
                        f"SELECT ri.* FROM reddit_items ri {like_where} ORDER BY ri.created_utc ASC LIMIT ?",
                        [*values, f"%{query}%", limit],
                    ).fetchall()
                    return [_row_to_dict(row) for row in rows]
            rows = db.execute(
                f"SELECT ri.* FROM reddit_items ri {where} ORDER BY ri.created_utc ASC LIMIT ?",
                [*values, limit],
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def archive_coverage(self) -> dict[str, int]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT
                    COUNT(*) AS items,
                    SUM(CASE WHEN kind='post' THEN 1 ELSE 0 END) AS posts,
                    SUM(CASE WHEN kind='comment' THEN 1 ELSE 0 END) AS comments,
                    COUNT(DISTINCT archive_file_id) AS files
                FROM reddit_items
                """
            ).fetchone()
            chunks = db.execute("SELECT COUNT(*) AS count FROM archive_semantic_chunks").fetchone()
            embeddings = db.execute("SELECT COUNT(*) AS count FROM archive_semantic_embeddings").fetchone()
            embedded = db.execute(
                """
                SELECT COUNT(
                    DISTINCT CASE
                        WHEN ase.id IS NOT NULL
                            OR (sc.embedding_model_id IS NOT NULL AND sc.embedding_model_id != '')
                        THEN sc.item_id
                    END
                ) AS count
                FROM archive_semantic_chunks sc
                LEFT JOIN archive_semantic_embeddings ase ON ase.semantic_chunk_id = sc.id
                """
            ).fetchone()
            metadata = db.execute(
                """
                SELECT
                    SUM(CASE WHEN meta_json IS NOT NULL AND meta_json != '{}' THEN 1 ELSE 0 END) AS metadata_items,
                    SUM(CASE WHEN json_extract(meta_json, '$.classifier') IS NOT NULL THEN 1 ELSE 0 END) AS classifier_items,
                    SUM(CASE WHEN json_extract(meta_json, '$.classifier.status') = 'unavailable' THEN 1 ELSE 0 END)
                        AS classifier_unavailable_items
                FROM reddit_items
             """
            ).fetchone()
            jobs = db.execute(
                """
                SELECT
                    SUM(CASE WHEN status='running' AND finished_at IS NULL THEN 1 ELSE 0 END) AS stale_running_jobs,
                    SUM(CASE WHEN status='interrupted' AND finished_at IS NULL THEN 1 ELSE 0 END) AS resumable_import_jobs
                FROM reddit_import_jobs
                """
            ).fetchone()
            stale_running_jobs = int(jobs["stale_running_jobs"] or 0)
            resumable_import_jobs = int(jobs["resumable_import_jobs"] or 0)
        return {
            "files": int(row["files"] or 0),
            "items": int(row["items"] or 0),
            "posts": int(row["posts"] or 0),
            "comments": int(row["comments"] or 0),
            "metadata_items": int(metadata["metadata_items"] or 0),
            "classifier_items": int(metadata["classifier_items"] or 0),
            "classifier_unavailable_items": int(metadata["classifier_unavailable_items"] or 0),
            "semantic_chunks": int(chunks["count"] or 0),
            "semantic_embeddings": int(embeddings["count"] or 0),
            "embedded_items": int(embedded["count"] or 0),
            "stale_running_jobs": stale_running_jobs,
            "resumable_import_jobs": resumable_import_jobs,
        }

    def create_reddit_import_job(self, payload: dict[str, Any]) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO reddit_import_jobs(
                    target_type, target_name, status, current_stage, payload_json, stage_counts_json,
                    progress_percent, eta_label
                )
                VALUES (?, ?, 'queued', 'queued', ?, '{}', 0, 'estimating')
                """,
                (
                    str(payload.get("target_type") or ""),
                    str(payload.get("target_name") or ""),
                    _json(payload),
                ),
            )
            return int(cursor.lastrowid)

    def update_reddit_import_job(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        stage_counts: dict[str, Any] | None = None,
        progress_percent: int | float | None = None,
        eta_seconds: int | None = None,
        eta_label: str | None = None,
        log: str | None = None,
        finished: bool = False,
    ) -> None:
        assignments = [
            "status=?",
            "current_stage=?",
            "updated_at=CURRENT_TIMESTAMP",
        ]
        values: list[Any] = [status, current_stage]
        if stage_counts is not None:
            assignments.append("stage_counts_json=?")
            values.append(_json(stage_counts))
        if progress_percent is not None:
            assignments.append("progress_percent=?")
            values.append(max(0, min(100, float(progress_percent))))
        if eta_seconds is not None:
            assignments.append("eta_seconds=?")
            values.append(eta_seconds)
        if eta_label is not None:
            assignments.append("eta_label=?")
            values.append(eta_label)
        if log is not None:
            assignments.append("log=?")
            values.append(log)
        if finished:
            assignments.append("finished_at=CURRENT_TIMESTAMP")
        values.append(job_id)
        with self.connect() as db:
            db.execute(f"UPDATE reddit_import_jobs SET {', '.join(assignments)} WHERE id=?", values)

    def get_reddit_import_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM reddit_import_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def find_active_reddit_import_job(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        target_type = (payload.get("target_type") or "").strip().lower()
        target_name = (payload.get("target_name") or "").strip()
        if not target_name:
            return None
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM reddit_import_jobs
                WHERE LOWER(target_type) = ?
                    AND LOWER(target_name) = LOWER(?)
                    AND status IN ('queued', 'running', 'interrupted')
                    AND finished_at IS NULL
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (target_type, target_name),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def reconcile_interrupted_reddit_import_jobs(self) -> int:
        count = 0
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id, status, current_stage FROM reddit_import_jobs
                WHERE status IN ('queued', 'running')
                    AND finished_at IS NULL
                """
            ).fetchall()
            for row in rows:
                job_id = row["id"]
                db.execute(
                    """
                    UPDATE reddit_import_jobs
                    SET status = 'interrupted',
                        log = COALESCE(log, '') || 'Interrupted at app startup: unfinished job.',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (job_id,),
                )
                count += 1
        return count

    def archive_subreddits_for_paths(self, paths: list[str]) -> list[str]:
        if not paths:
            return []
        placeholders = ",".join("?" for _ in paths)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT DISTINCT LOWER(ri.subreddit) AS subreddit
                FROM reddit_items ri
                JOIN archive_files af ON af.id = ri.archive_file_id
                WHERE af.path IN ({placeholders}) AND TRIM(ri.subreddit) != ''
                ORDER BY LOWER(ri.subreddit)
                """,
                paths,
            ).fetchall()
        return [str(row["subreddit"]) for row in rows]

    def archive_subreddits_for_import_target(self, target_type: str, target_name: str) -> list[str]:
        target_type = (target_type or "").strip().lower()
        target_name = (target_name or "").strip()
        if not target_name:
            return []
        if target_type == "subreddit":
            where = "LOWER(ri.subreddit) = LOWER(?)"
        elif target_type == "user":
            where = "LOWER(ri.author) = LOWER(?)"
        else:
            return []
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT DISTINCT LOWER(ri.subreddit) AS subreddit
                FROM reddit_items ri
                WHERE {where} AND TRIM(ri.subreddit) != ''
                ORDER BY LOWER(ri.subreddit)
                """,
                (target_name,),
            ).fetchall()
        return [str(row["subreddit"]) for row in rows]

    def reddit_item_kind_counts_for_import_target(self, target_type: str, target_name: str) -> dict[str, int]:
        target_type = (target_type or "").strip().lower()
        target_name = (target_name or "").strip()
        if not target_name:
            return {}
        if target_type == "subreddit":
            where = "LOWER(subreddit) = LOWER(?)"
        elif target_type == "user":
            where = "LOWER(author) = LOWER(?)"
        else:
            return {}
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT kind, COUNT(*) AS count
                FROM reddit_items
                WHERE {where}
                GROUP BY kind
                """,
                (target_name,),
            ).fetchall()
        return {str(row["kind"]): int(row["count"] or 0) for row in rows}

    def list_reddit_items_for_subreddit(self, subreddit: str) -> list[dict[str, Any]]:
        normalized = _normalize_subreddit(subreddit)
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT *
                FROM reddit_items
                WHERE LOWER(subreddit) = LOWER(?)
                ORDER BY kind DESC, created_utc IS NULL, created_utc ASC, id ASC
                """,
                (normalized,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_unembedded_archive_semantic_chunk_ids(self, subreddit: str, model_id: str) -> list[int]:
        normalized = _normalize_subreddit(subreddit)
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT sc.id
                FROM archive_semantic_chunks sc
                JOIN reddit_items ri ON ri.id = sc.item_id
                LEFT JOIN archive_semantic_embeddings ase
                    ON ase.semantic_chunk_id = sc.id AND ase.model_id = ?
                WHERE LOWER(ri.subreddit) = LOWER(?) AND ase.id IS NULL
                ORDER BY sc.id
                """,
                (model_id, normalized),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def archive_semantic_counts_for_subreddit(self, subreddit: str, model_id: str) -> dict[str, int]:
        normalized = _normalize_subreddit(subreddit)
        with self.connect() as db:
            row = db.execute(
                """
                SELECT
                    COUNT(sc.id) AS semantic_chunks,
                    COUNT(DISTINCT CASE WHEN ase.id IS NOT NULL THEN sc.id END) AS embedded_chunks
                FROM archive_semantic_chunks sc
                JOIN reddit_items ri ON ri.id = sc.item_id
                LEFT JOIN archive_semantic_embeddings ase
                    ON ase.semantic_chunk_id = sc.id AND ase.model_id = ?
                WHERE LOWER(ri.subreddit) = LOWER(?)
                """,
                (model_id, normalized),
            ).fetchone()
        return {
            "semantic_chunks": int(row["semantic_chunks"] or 0),
            "embedded_chunks": int(row["embedded_chunks"] or 0),
        }

    def update_reddit_item_meta(self, item_id: int, meta: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("UPDATE reddit_items SET meta_json=? WHERE id=?", (_json(meta), item_id))

    def delete_archive_semantic_for_subreddit(self, subreddit: str) -> None:
        normalized = _normalize_subreddit(subreddit)
        with self.connect() as db:
            db.execute(
                """
                DELETE FROM archive_semantic_chunks
                WHERE item_id IN (
                    SELECT id FROM reddit_items WHERE LOWER(subreddit) = LOWER(?)
                )
                """,
                (normalized,),
            )

    def upsert_archive_semantic_chunk(
        self,
        item_id: int,
        chunk_index: int,
        citation_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> int:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO archive_semantic_chunks(item_id, chunk_index, citation_id, text, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(item_id, chunk_index) DO UPDATE SET
                    citation_id=excluded.citation_id,
                    text=excluded.text,
                    metadata_json=excluded.metadata_json
                """,
                (item_id, chunk_index, citation_id, text, _json(metadata)),
            )
            row = db.execute(
                "SELECT id FROM archive_semantic_chunks WHERE item_id=? AND chunk_index=?",
                (item_id, chunk_index),
            ).fetchone()
            return int(row["id"])

    def insert_archive_embedding(self, semantic_chunk_id: int, model_id: str, vector: Iterable[float]) -> int:
        values = list(vector)
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO archive_semantic_embeddings(semantic_chunk_id, model_id, dimensions, vector_blob)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(semantic_chunk_id, model_id) DO UPDATE SET
                    dimensions=excluded.dimensions,
                    vector_blob=excluded.vector_blob,
                    created_at=CURRENT_TIMESTAMP
                """,
                (semantic_chunk_id, model_id, len(values), vector_to_blob(values)),
            )
            db.execute(
                """
                UPDATE archive_semantic_chunks
                SET embedding_model_id=?, embedding_dimensions=?
                WHERE id=?
                """,
                (model_id, len(values), semantic_chunk_id),
            )
            return int(cursor.lastrowid or semantic_chunk_id)

    def all_archive_embeddings(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT
                    ase.semantic_chunk_id,
                    ase.model_id,
                    ase.dimensions,
                    ase.vector_blob,
                    sc.citation_id,
                    sc.text,
                    sc.metadata_json,
                    ri.id AS item_id,
                    ri.kind,
                    ri.subreddit,
                    ri.author,
                    ri.score,
                    ri.title,
                    ri.permalink
                FROM archive_semantic_embeddings ase
                JOIN archive_semantic_chunks sc ON sc.id = ase.semantic_chunk_id
                JOIN reddit_items ri ON ri.id = sc.item_id
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_archive_semantic_chunks(self, chunk_ids: list[int]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT
                    sc.*,
                    ri.kind,
                    ri.subreddit,
                    ri.author,
                    ri.score,
                    ri.title,
                    ri.permalink
                FROM archive_semantic_chunks sc
                JOIN reddit_items ri ON ri.id = sc.item_id
                WHERE sc.id IN ({placeholders})
                """,
                chunk_ids,
            ).fetchall()
        by_id = {int(row["id"]): _row_to_dict(row) for row in rows}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]

    def purge_subreddit_data(self, subreddit: str) -> dict[str, Any]:
        normalized = _normalize_subreddit(subreddit)
        with self.connect() as db:
            path_rows = db.execute(
                """
                SELECT DISTINCT af.path
                FROM archive_files af
                JOIN reddit_items ri ON ri.archive_file_id = af.id
                WHERE LOWER(ri.subreddit) = LOWER(?)
                ORDER BY af.path
                """,
                (normalized,),
            ).fetchall()
            paths = [str(row["path"]) for row in path_rows]

            source_ids: set[int] = set()
            for path in paths:
                rows = db.execute(
                    """
                    SELECT DISTINCT s.id
                    FROM sources s
                    LEFT JOIN source_artifacts sa ON sa.source_id = s.id
                    WHERE s.uri = ?
                        OR s.uri LIKE ?
                        OR sa.path = ?
                        OR sa.path LIKE ?
                    """,
                    (path, f"{path}#%", path, f"{path}#%"),
                ).fetchall()
                source_ids.update(int(row["id"]) for row in rows)

            counts = {
                "archive_files": 0,
                "reddit_items": int(
                    db.execute(
                        "SELECT COUNT(*) FROM reddit_items WHERE LOWER(subreddit) = LOWER(?)",
                        (normalized,),
                    ).fetchone()[0]
                ),
                "archive_semantic_chunks": int(
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM archive_semantic_chunks sc
                        JOIN reddit_items ri ON ri.id = sc.item_id
                        WHERE LOWER(ri.subreddit) = LOWER(?)
                        """,
                        (normalized,),
                    ).fetchone()[0]
                ),
                "archive_embeddings": int(
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM archive_semantic_embeddings ase
                        JOIN archive_semantic_chunks sc ON sc.id = ase.semantic_chunk_id
                        JOIN reddit_items ri ON ri.id = sc.item_id
                        WHERE LOWER(ri.subreddit) = LOWER(?)
                        """,
                        (normalized,),
                    ).fetchone()[0]
                ),
                "corpus_sources": len(source_ids),
                "corpus_documents": 0,
                "corpus_chunks": 0,
                "corpus_embeddings": 0,
            }
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                ids = list(source_ids)
                counts["corpus_documents"] = int(
                    db.execute(f"SELECT COUNT(*) FROM documents WHERE source_id IN ({placeholders})", ids).fetchone()[0]
                )
                counts["corpus_chunks"] = int(
                    db.execute(f"SELECT COUNT(*) FROM chunks WHERE source_id IN ({placeholders})", ids).fetchone()[0]
                )
                counts["corpus_embeddings"] = int(
                    db.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM embeddings e
                        JOIN chunks c ON c.id = e.chunk_id
                        WHERE c.source_id IN ({placeholders})
                        """,
                        ids,
                    ).fetchone()[0]
                )
                db.execute(f"DELETE FROM sources WHERE id IN ({placeholders})", ids)

            db.execute("DELETE FROM reddit_items WHERE LOWER(subreddit) = LOWER(?)", (normalized,))
            empty_file_ids = db.execute(
                """
                SELECT af.id
                FROM archive_files af
                LEFT JOIN reddit_items ri ON ri.archive_file_id = af.id
                WHERE ri.id IS NULL
                """
            ).fetchall()
            if empty_file_ids:
                ids = [int(row["id"]) for row in empty_file_ids]
                placeholders = ",".join("?" for _ in ids)
                counts["archive_files"] = len(ids)
                db.execute(f"DELETE FROM archive_files WHERE id IN ({placeholders})", ids)
            db.execute("INSERT INTO reddit_items_fts(reddit_items_fts) VALUES('rebuild')")
            return {"deleted": counts, "source_files": paths}

    def purge_loaded_data(self) -> dict[str, int]:
        with self.connect() as db:
            counts = {
                "archive_files": int(db.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0]),
                "reddit_items": int(db.execute("SELECT COUNT(*) FROM reddit_items").fetchone()[0]),
                "archive_semantic_chunks": int(db.execute("SELECT COUNT(*) FROM archive_semantic_chunks").fetchone()[0]),
                "archive_embeddings": int(db.execute("SELECT COUNT(*) FROM archive_semantic_embeddings").fetchone()[0]),
                "corpus_sources": int(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]),
                "corpus_documents": int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
                "corpus_chunks": int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
                "corpus_embeddings": int(db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]),
                "corpus_source_artifacts": int(db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]),
                "ingestion_jobs": int(db.execute("SELECT COUNT(*) FROM ingestion_jobs").fetchone()[0]),
                "reddit_import_jobs": int(db.execute("SELECT COUNT(*) FROM reddit_import_jobs").fetchone()[0]),
                "retrieval_runs": int(db.execute("SELECT COUNT(*) FROM retrieval_runs").fetchone()[0]),
            }
            db.execute("DELETE FROM sources")
            db.execute("DELETE FROM archive_semantic_embeddings")
            db.execute("DELETE FROM archive_semantic_chunks")
            db.execute("DELETE FROM reddit_items")
            db.execute("DELETE FROM archive_files")
            db.execute("DELETE FROM ingestion_jobs")
            db.execute("DELETE FROM reddit_import_jobs")
            db.execute("DELETE FROM retrieval_runs")
            db.execute("INSERT INTO reddit_items_fts(reddit_items_fts) VALUES('rebuild')")
            return counts

    def list_archive_subreddit_summaries(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT
                    LOWER(subreddit) AS subreddit_key,
                    COUNT(*) AS items,
                    SUM(CASE WHEN kind='post' THEN 1 ELSE 0 END) AS posts,
                    SUM(CASE WHEN kind='comment' THEN 1 ELSE 0 END) AS comments,
                    MIN(created_utc) AS min_created_utc,
                    MAX(created_utc) AS max_created_utc
                FROM reddit_items
                WHERE TRIM(subreddit) != ''
                GROUP BY LOWER(subreddit)
                ORDER BY LOWER(subreddit)
                """
            ).fetchall()
            return [self._archive_subreddit_summary_from_row(db, row) for row in rows]

    def get_archive_subreddit_summary(self, subreddit: str) -> dict[str, Any] | None:
        normalized = _normalize_subreddit(subreddit)
        if not normalized:
            return None
        with self.connect() as db:
            row = db.execute(
                """
                SELECT
                    LOWER(subreddit) AS subreddit_key,
                    COUNT(*) AS items,
                    SUM(CASE WHEN kind='post' THEN 1 ELSE 0 END) AS posts,
                    SUM(CASE WHEN kind='comment' THEN 1 ELSE 0 END) AS comments,
                    MIN(created_utc) AS min_created_utc,
                    MAX(created_utc) AS max_created_utc
                FROM reddit_items
                WHERE LOWER(subreddit) = LOWER(?)
                GROUP BY LOWER(subreddit)
                """,
                (normalized,),
            ).fetchone()
            return self._archive_subreddit_summary_from_row(db, row) if row else None

    def list_reddit_posts_for_subreddit(self, subreddit: str) -> list[dict[str, Any]]:
        normalized = _normalize_subreddit(subreddit)
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT *
                FROM reddit_items
                WHERE kind='post' AND LOWER(subreddit) = LOWER(?)
                ORDER BY created_utc IS NULL, created_utc ASC, id ASC
                """,
                (normalized,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_reddit_comments_for_subreddit(self, subreddit: str) -> list[dict[str, Any]]:
        normalized = _normalize_subreddit(subreddit)
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT *
                FROM reddit_items
                WHERE kind='comment' AND LOWER(subreddit) = LOWER(?)
                ORDER BY created_utc IS NULL, created_utc ASC, id ASC
                """,
                (normalized,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_ingestion_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def insert_retrieval_run(self, query: str, filters: dict[str, Any], results: list[dict[str, Any]], packed_context: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO retrieval_runs(query, filters_json, results_json, packed_context)
                VALUES (?, ?, ?, ?)
                """,
                (query, _json(filters), _json(results), packed_context),
            )
            return int(cursor.lastrowid)

    def create_conversation(self, title: str = "New chat", system_prompt: str = "") -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO conversations(title, system_prompt) VALUES (?, ?)",
                (title or "New chat", system_prompt or ""),
            )
            return int(cursor.lastrowid)

    def update_conversation(self, conversation_id: int, title: str | None = None, system_prompt: str | None = None) -> None:
        assignments: list[str] = ["updated_at=CURRENT_TIMESTAMP"]
        values: list[Any] = []
        if title is not None:
            assignments.append("title=?")
            values.append(title)
        if system_prompt is not None:
            assignments.append("system_prompt=?")
            values.append(system_prompt)
        values.append(conversation_id)
        with self.connect() as db:
            db.execute(f"UPDATE conversations SET {', '.join(assignments)} WHERE id=?", values)

    def get_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def insert_message(
        self,
        role: str,
        content: str,
        reasoning: str = "",
        conversation_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO messages(conversation_id, role, content, reasoning, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, reasoning or "", _json(metadata or {})),
            )
            if conversation_id is not None:
                db.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (conversation_id,))
            return int(cursor.lastrowid)

    def list_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, id ASC",
                (conversation_id,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def insert_attachment(self, attachment_type: str, path: str, metadata: dict[str, Any]) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO attachments(attachment_type, path, metadata_json) VALUES (?, ?, ?)",
                (attachment_type, path, _json(metadata)),
            )
            return int(cursor.lastrowid)

    def _insert_metadata_tags(self, db: sqlite3.Connection, document_id: int, metadata: SourceMetadata) -> None:
        fields = ("topics", "tags", "entities", "people", "organizations", "places", "dates")
        for field in fields:
            for value in getattr(metadata, field):
                db.execute(
                    "INSERT INTO metadata_tags(document_id, tag_type, tag_value) VALUES (?, ?, ?)",
                    (document_id, field, value),
                )

    def _run_migrations(self, db: sqlite3.Connection) -> None:
        _ensure_column(db, "conversations", "system_prompt", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "archive_semantic_chunks", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "archive_semantic_chunks", "embedding_dimensions", "INTEGER")

    def _archive_subreddit_summary_from_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        subreddit_key = str(row["subreddit_key"] or "")
        files = db.execute(
            """
            SELECT DISTINCT af.path
            FROM reddit_items ri
            JOIN archive_files af ON af.id = ri.archive_file_id
            WHERE LOWER(ri.subreddit) = ?
            ORDER BY af.path
            """,
            (subreddit_key,),
        ).fetchall()
        latest = db.execute(
            """
            SELECT af.status, COALESCE(af.finished_at, af.updated_at, af.created_at) AS import_at
            FROM reddit_items ri
            JOIN archive_files af ON af.id = ri.archive_file_id
            WHERE LOWER(ri.subreddit) = ?
            ORDER BY import_at DESC, af.id DESC
            LIMIT 1
            """,
            (subreddit_key,),
        ).fetchone()
        rag = db.execute(
            """
            SELECT
                COUNT(sc.id) AS semantic_chunks,
                COUNT(
                    DISTINCT CASE
                        WHEN ase.id IS NOT NULL
                            OR (sc.embedding_model_id IS NOT NULL AND sc.embedding_model_id != '')
                        THEN sc.item_id
                    END
                ) AS embedded_items
            FROM reddit_items ri
            LEFT JOIN archive_semantic_chunks sc ON sc.item_id = ri.id
            LEFT JOIN archive_semantic_embeddings ase ON ase.semantic_chunk_id = sc.id
            WHERE LOWER(ri.subreddit) = ?
            """,
            (subreddit_key,),
        ).fetchone()
        model_rows = db.execute(
            """
            SELECT DISTINCT sc.embedding_model_id
            FROM reddit_items ri
            JOIN archive_semantic_chunks sc ON sc.item_id = ri.id
            WHERE LOWER(ri.subreddit) = ?
                AND sc.embedding_model_id IS NOT NULL
                AND sc.embedding_model_id != ''
            ORDER BY sc.embedding_model_id
            """,
            (subreddit_key,),
        ).fetchall()
        dimension_rows = db.execute(
            """
            SELECT DISTINCT sc.embedding_dimensions
            FROM reddit_items ri
            JOIN archive_semantic_chunks sc ON sc.item_id = ri.id
            WHERE LOWER(ri.subreddit) = ?
                AND sc.embedding_dimensions IS NOT NULL
                AND sc.embedding_model_id IS NOT NULL
                AND sc.embedding_model_id != ''
            ORDER BY sc.embedding_dimensions
            """,
            (subreddit_key,),
        ).fetchall()
        metadata_rows = db.execute(
            """
            SELECT meta_json
            FROM reddit_items
            WHERE LOWER(subreddit) = ?
            """,
            (subreddit_key,),
        ).fetchall()
        metadata_fields: set[str] = set()
        metadata_items = 0
        classifier_items = 0
        classifier_unavailable_items = 0
        for metadata_row in metadata_rows:
            try:
                metadata = json.loads(metadata_row["meta_json"])
            except json.JSONDecodeError:
                metadata = {}
            if isinstance(metadata, dict):
                metadata_fields.update(str(key) for key in metadata.keys())
                if metadata:
                    metadata_items += 1
                classifier = metadata.get("classifier")
                if classifier is not None:
                    classifier_items += 1
                    if isinstance(classifier, dict) and classifier.get("status") == "unavailable":
                        classifier_unavailable_items += 1
        active_job = db.execute(
            """
            SELECT *
            FROM reddit_import_jobs
            WHERE target_type = 'subreddit'
                AND LOWER(target_name) = ?
                AND status IN ('queued', 'running', 'interrupted')
                AND finished_at IS NULL
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (subreddit_key,),
        ).fetchone()
        active_import_status = None
        resumable_import = False
        if active_job:
            active_import_status = str(active_job["status"])
            resumable_import = active_import_status == "interrupted"
        semantic_chunks = int(rag["semantic_chunks"] or 0)
        embedded_items = int(rag["embedded_items"] or 0)
        item_count = int(row["items"] or 0)
        if semantic_chunks == 0:
            semantic_index_state = "not_built" if item_count else "empty"
        elif embedded_items < item_count:
            semantic_index_state = "partial"
        else:
            semantic_index_state = "ready"

        return {
            "subreddit": subreddit_key,
            "items": item_count,
            "posts": int(row["posts"] or 0),
            "comments": int(row["comments"] or 0),
            "min_created_utc": row["min_created_utc"],
            "max_created_utc": row["max_created_utc"],
            "source_files": [file_row["path"] for file_row in files],
            "latest_import_at": latest["import_at"] if latest else None,
            "latest_import_status": latest["status"] if latest else None,
            "active_import_status": active_import_status,
            "resumable_import": resumable_import,
            "semantic_index_state": semantic_index_state,
            "metadata_items": metadata_items,
            "classifier_items": classifier_items,
            "classifier_unavailable_items": classifier_unavailable_items,
            "semantic_chunks": semantic_chunks,
            "embedded_items": embedded_items,
            "embedding_model_ids": [model_row["embedding_model_id"] for model_row in model_rows],
            "embedding_dimensions": [int(dimension_row["embedding_dimensions"]) for dimension_row in dimension_rows],
            "metadata_fields": sorted(metadata_fields),
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _reddit_item_values(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["archive_file_id"],
        item["line_number"],
        item["item_key"],
        item["reddit_id"],
        item["kind"],
        item["subreddit"],
        item["author"],
        item["created_utc"],
        item["score"],
        item["title"],
        item["selftext"],
        item["body"],
        item["url"],
        item["permalink"],
        item["link_id"],
        item["parent_id"],
        item["text"],
        _json(item["raw"]),
        _json(item["meta"]),
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in list(data.keys()):
        if key.endswith("_json") and isinstance(data[key], str):
            out_key = key.removesuffix("_json")
            try:
                data[out_key] = json.loads(data[key])
            except json.JSONDecodeError:
                data[out_key] = data[key]
    if "metadata_json" in data and "metadata" not in data:
        try:
            data["metadata"] = json.loads(data["metadata_json"])
        except json.JSONDecodeError:
            data["metadata"] = {}
    return data


def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _normalize_subreddit(value: str | None) -> str:
    text = (value or "").strip()
    if text.lower().startswith("r/"):
        text = text[2:]
    return text


def _reddit_filters(
    kind: str | None = None,
    subreddit: str | None = None,
    after: int | None = None,
    before: int | None = None,
    prefix: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    table_prefix = f"{prefix}." if prefix else ""
    if kind:
        clauses.append(f"{table_prefix}kind = ?")
        values.append(kind)
    if subreddit:
        clauses.append(f"LOWER({table_prefix}subreddit) = LOWER(?)")
        values.append(_normalize_subreddit(subreddit))
    if after is not None:
        clauses.append(f"{table_prefix}created_utc >= ?")
        values.append(after)
    if before is not None:
        clauses.append(f"{table_prefix}created_utc <= ?")
        values.append(before)
    return ("WHERE " + " AND ".join(clauses), values) if clauses else ("", values)


def _fts_query(query: str) -> str:
    terms = re.findall(r"[\w#@./:-]+", query)
    if not terms:
        return '""'
    quoted_terms = []
    for term in terms[:12]:
        escaped = term.replace('"', '""')
        quoted_terms.append(f'"{escaped}"')
    return " AND ".join(quoted_terms)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    uri TEXT NOT NULL,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_artifacts (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    extracted_text_path TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT 'unknown',
    source_type TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    citation_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_blob BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metadata_tags (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_type TEXT NOT NULL,
    tag_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id INTEGER PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    processed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS reddit_import_jobs (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_name TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    stage_counts_json TEXT NOT NULL DEFAULT '{}',
    progress_percent REAL NOT NULL DEFAULT 0,
    eta_seconds INTEGER,
    eta_label TEXT NOT NULL DEFAULT 'estimating',
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS retrieval_runs (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    results_json TEXT NOT NULL DEFAULT '[]',
    packed_context TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    system_prompt TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    reasoning TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tool_runs (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    attachment_type TEXT NOT NULL,
    path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archive_files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    file_format TEXT NOT NULL,
    fallback_kind TEXT,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS reddit_items (
    id INTEGER PRIMARY KEY,
    archive_file_id INTEGER NOT NULL REFERENCES archive_files(id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    item_key TEXT NOT NULL UNIQUE,
    reddit_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    subreddit TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    created_utc INTEGER,
    score INTEGER,
    title TEXT NOT NULL DEFAULT '',
    selftext TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    permalink TEXT NOT NULL DEFAULT '',
    link_id TEXT NOT NULL DEFAULT '',
    parent_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reddit_items_kind ON reddit_items(kind);
CREATE INDEX IF NOT EXISTS idx_reddit_items_subreddit ON reddit_items(subreddit);
CREATE INDEX IF NOT EXISTS idx_reddit_items_created ON reddit_items(created_utc);
CREATE INDEX IF NOT EXISTS idx_reddit_items_archive_file ON reddit_items(archive_file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS reddit_items_fts USING fts5(
    title,
    selftext,
    body,
    text,
    content='reddit_items',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS reddit_items_ai AFTER INSERT ON reddit_items BEGIN
    INSERT INTO reddit_items_fts(rowid, title, selftext, body, text)
    VALUES (new.id, new.title, new.selftext, new.body, new.text);
END;

CREATE TRIGGER IF NOT EXISTS reddit_items_ad AFTER DELETE ON reddit_items BEGIN
    INSERT INTO reddit_items_fts(reddit_items_fts, rowid, title, selftext, body, text)
    VALUES('delete', old.id, old.title, old.selftext, old.body, old.text);
END;

CREATE TRIGGER IF NOT EXISTS reddit_items_au AFTER UPDATE ON reddit_items BEGIN
    INSERT INTO reddit_items_fts(reddit_items_fts, rowid, title, selftext, body, text)
    VALUES('delete', old.id, old.title, old.selftext, old.body, old.text);
    INSERT INTO reddit_items_fts(rowid, title, selftext, body, text)
    VALUES (new.id, new.title, new.selftext, new.body, new.text);
END;

CREATE TABLE IF NOT EXISTS archive_semantic_chunks (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES reddit_items(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    citation_id TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    embedding_model_id TEXT,
    embedding_dimensions INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS archive_semantic_embeddings (
    id INTEGER PRIMARY KEY,
    semantic_chunk_id INTEGER NOT NULL REFERENCES archive_semantic_chunks(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_blob BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(semantic_chunk_id, model_id)
);
"""
