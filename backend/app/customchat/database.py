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
            embedded = db.execute("SELECT COUNT(DISTINCT item_id) AS count FROM archive_semantic_chunks").fetchone()
        return {
            "files": int(row["files"] or 0),
            "items": int(row["items"] or 0),
            "posts": int(row["posts"] or 0),
            "comments": int(row["comments"] or 0),
            "semantic_chunks": int(chunks["count"] or 0),
            "embedded_items": int(embedded["count"] or 0),
        }

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
        values.append(subreddit.removeprefix("r/"))
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
    embedding_model_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_id, chunk_index)
);
"""
