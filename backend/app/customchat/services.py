from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import numpy as np

from customchat.config import Settings
from customchat.database import Database
from customchat.ingestion import (
    SourcePayload,
    canonicalize_url,
    content_hash,
    extract_text_from_file,
    normalize_html,
    parse_scraper_jsonl,
)
from customchat.lemonade_client import LemonadeClient
from customchat.metadata import SourceMetadata
from customchat.rag import blob_to_vector, chunk_text, cosine_rank, pack_context


@dataclass(slots=True)
class IngestionResult:
    job_id: int
    status: str
    processed_count: int
    failed_count: int
    log: str


class IngestionService:
    def __init__(self, settings: Settings, database: Database, lemonade: LemonadeClient):
        self.settings = settings
        self.database = database
        self.lemonade = lemonade

    async def ingest(self, request: dict[str, Any]) -> IngestionResult:
        job_id = self.database.create_ingestion_job(request.get("kind", "unknown"), request)
        try:
            payloads = await self._payloads_from_request(request)
            processed = 0
            failures: list[str] = []
            for payload in payloads:
                try:
                    await self._persist_payload(payload)
                    processed += 1
                except Exception as exc:  # noqa: BLE001 - job log should keep going
                    failures.append(f"{payload.uri}: {exc}")
            status = "completed" if not failures else "partial"
            log = "\n".join(failures)
            self.database.update_ingestion_job(job_id, status, processed, len(failures), log)
            return IngestionResult(job_id, status, processed, len(failures), log)
        except Exception as exc:  # noqa: BLE001
            self.database.update_ingestion_job(job_id, "failed", 0, 1, str(exc))
            return IngestionResult(job_id, "failed", 0, 1, str(exc))

    async def _payloads_from_request(self, request: dict[str, Any]) -> list[SourcePayload]:
        kind = request.get("kind")
        if kind == "text":
            return [
                SourcePayload(
                    source_type="text",
                    uri=request.get("uri") or f"memory://{content_hash(request.get('text', ''))[:12]}",
                    title=request.get("title") or "Pasted text",
                    text=request.get("text") or "",
                )
            ]
        if kind == "file":
            return extract_text_from_file(Path(request["path"]))
        if kind == "folder":
            root = Path(request["path"])
            payloads: list[SourcePayload] = []
            for path in root.rglob("*"):
                if path.is_file():
                    payloads.extend(extract_text_from_file(path))
            return payloads
        if kind == "jsonl":
            return parse_scraper_jsonl(Path(request["path"]))
        if kind == "url":
            return [await self._fetch_url(request["url"])]
        if kind == "crawl":
            return await self._crawl(request["url"], int(request.get("max_pages") or self.settings.crawl_max_pages))
        raise ValueError(f"Unsupported ingestion kind: {kind}")

    async def _fetch_url(self, url: str) -> SourcePayload:
        canonical = canonicalize_url(url)
        async with httpx.AsyncClient(timeout=self.settings.crawl_timeout_seconds, follow_redirects=True) as client:
            response = await client.get(canonical)
            response.raise_for_status()
        title, text = normalize_html(response.text)
        return SourcePayload(
            source_type="web",
            uri=canonical,
            url=canonical,
            title=title,
            text=text,
            raw={"status_code": response.status_code, "headers": dict(response.headers), "html": response.text},
        )

    async def _crawl(self, seed_url: str, max_pages: int) -> list[SourcePayload]:
        seed = canonicalize_url(seed_url)
        seed_host = urlsplit(seed).netloc
        seen: set[str] = set()
        queue = [seed]
        payloads: list[SourcePayload] = []
        async with httpx.AsyncClient(timeout=self.settings.crawl_timeout_seconds, follow_redirects=True) as client:
            while queue and len(payloads) < max_pages:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                response = await client.get(url)
                if "text/html" not in response.headers.get("content-type", ""):
                    continue
                title, text = normalize_html(response.text)
                payloads.append(
                    SourcePayload(
                        source_type="web",
                        uri=url,
                        url=url,
                        title=title,
                        text=text,
                        raw={"status_code": response.status_code, "html": response.text},
                    )
                )
                links = _same_domain_links(response.text, url, seed_host)
                queue.extend(link for link in links if link not in seen and link not in queue)
                await asyncio.sleep(self.settings.crawl_delay_seconds)
        return payloads

    async def _persist_payload(self, payload: SourcePayload) -> None:
        text = payload.text.strip()
        if not text:
            raise ValueError("No extractable text")
        hash_value = content_hash(text)
        source_id = self.database.upsert_source(payload.source_type, payload.uri, payload.title, hash_value, "ready")
        artifact_path = self._write_artifact(source_id, payload)
        self.database.insert_source_artifact(source_id, payload.source_type, artifact_path, raw_json=payload.raw)
        try:
            metadata = await self.lemonade.classify(
                self.settings.classifier_model_id,
                payload.title,
                text,
                payload.source_type,
            )
        except Exception:
            metadata = SourceMetadata(title=payload.title, source_type=payload.source_type, summary=text[:300])
        document_id = self.database.insert_document(source_id, payload.title, text, metadata)
        chunks = chunk_text(
            document_id,
            source_id,
            text,
            metadata,
            chunk_tokens=self.settings.chunk_tokens,
            overlap_tokens=self.settings.overlap_tokens,
        )
        if not chunks:
            return
        try:
            embeddings = await self.lemonade.embed(self.settings.embedding_model_id, [chunk.text for chunk in chunks])
        except Exception:
            raise ValueError(f"Embedding model '{self.settings.embedding_model_id}' is not available on the configured endpoint") from None
        for chunk, vector in zip(chunks, embeddings, strict=False):
            chunk_id = self.database.insert_chunk(
                document_id,
                source_id,
                chunk.chunk_index,
                chunk.text,
                chunk.citation_id,
                chunk.metadata,
            )
            self.database.insert_embedding(chunk_id, self.settings.embedding_model_id, vector)

    def _write_artifact(self, source_id: int, payload: SourcePayload) -> str:
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.artifacts_dir / f"source-{source_id}.json"
        path.write_text(
            json.dumps(
                {
                    "uri": payload.uri,
                    "title": payload.title,
                    "text": payload.text,
                    "raw": payload.raw,
                    "metadata": payload.metadata,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(path)


class RagService:
    def __init__(self, settings: Settings, database: Database, lemonade: LemonadeClient):
        self.settings = settings
        self.database = database
        self.lemonade = lemonade

    async def search(self, query: str, top_k: int = 8, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        embeddings = self.database.all_embeddings()
        archive_embeddings = self.database.all_archive_embeddings()
        if not embeddings and not archive_embeddings:
            return {"results": [], "packed_context": "", "retrieval_run_id": None}
        try:
            query_vector = np.asarray((await self.lemonade.embed(self.settings.embedding_model_id, [query]))[0], dtype=np.float32)
        except Exception:
            raise ValueError(f"Embedding model '{self.settings.embedding_model_id}' is not available on the configured endpoint") from None
        ordered_chunks = self._rank_generic_chunks(query_vector, embeddings)
        ordered_chunks.extend(self._rank_archive_chunks(query_vector, archive_embeddings))
        ordered_chunks.sort(key=lambda chunk: float(chunk.get("score", 0.0)), reverse=True)
        reranked_chunks = await self._rerank(query, ordered_chunks[:40], top_k)
        packed = pack_context(reranked_chunks, max_chars=18000)
        results = [
            {
                "chunk_id": chunk["id"],
                "citation_id": chunk["citation_id"],
                "text": chunk["text"],
                "metadata": chunk.get("metadata", {}),
                "score": chunk.get("score", 0.0),
                "source": chunk.get("source", "corpus"),
            }
            for chunk in reranked_chunks
        ]
        run_id = self.database.insert_retrieval_run(query, filters or {}, results, packed)
        return {"results": results, "packed_context": packed, "retrieval_run_id": run_id}

    def _rank_generic_chunks(self, query_vector: np.ndarray, embeddings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not embeddings:
            return []
        candidates = [(int(row["chunk_id"]), blob_to_vector(row["vector_blob"])) for row in embeddings]
        ranked = cosine_rank(query_vector, candidates, top_k=120)
        chunk_ids = [item.chunk_id for item in ranked]
        chunks = self.database.get_chunks(chunk_ids)
        chunks_by_id = {int(chunk["id"]): chunk for chunk in chunks}
        ordered = []
        for item in ranked:
            chunk = chunks_by_id.get(item.chunk_id)
            if chunk:
                next_chunk = dict(chunk)
                next_chunk["score"] = item.score
                next_chunk["source"] = "corpus"
                ordered.append(next_chunk)
        return ordered

    def _rank_archive_chunks(self, query_vector: np.ndarray, embeddings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not embeddings:
            return []
        candidates = [(int(row["semantic_chunk_id"]), blob_to_vector(row["vector_blob"])) for row in embeddings]
        ranked = cosine_rank(query_vector, candidates, top_k=120)
        chunk_ids = [item.chunk_id for item in ranked]
        chunks = self.database.get_archive_semantic_chunks(chunk_ids)
        chunks_by_id = {int(chunk["id"]): chunk for chunk in chunks}
        ordered = []
        for item in ranked:
            chunk = chunks_by_id.get(item.chunk_id)
            if not chunk:
                continue
            metadata = dict(chunk.get("metadata") or {})
            metadata.setdefault("title", chunk.get("title") or f"r/{chunk.get('subreddit', 'unknown')}")
            metadata.setdefault("source_type", "reddit_archive")
            metadata.setdefault("subreddit", chunk.get("subreddit"))
            metadata.setdefault("author", chunk.get("author"))
            metadata.setdefault("score", chunk.get("score"))
            ordered.append(
                {
                    "id": int(chunk["id"]),
                    "citation_id": chunk["citation_id"],
                    "text": chunk["text"],
                    "metadata": metadata,
                    "title": metadata.get("title"),
                    "score": item.score,
                    "source": "reddit_archive",
                }
            )
        return ordered

    async def _rerank(self, query: str, chunks: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not chunks:
            return []
        try:
            rerank = await self.lemonade.rerank(self.settings.reranker_model_id, query, [chunk["text"] for chunk in chunks])
            ordered = []
            for item in sorted(rerank, key=lambda row: row.get("relevance_score", 0), reverse=True):
                index = int(item["index"])
                if 0 <= index < len(chunks):
                    chunk = dict(chunks[index])
                    chunk["score"] = float(item.get("relevance_score", 0.0))
                    ordered.append(chunk)
            return ordered[:top_k]
        except Exception:
            return chunks[:top_k]


def _same_domain_links(html: str, base_url: str, host: str) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        url = canonicalize_url(urljoin(base_url, anchor["href"]))
        if urlsplit(url).netloc == host:
            links.append(url)
    return links
