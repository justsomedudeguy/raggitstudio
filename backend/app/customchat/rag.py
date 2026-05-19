from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from customchat.metadata import SourceMetadata, normalize_metadata


@dataclass(slots=True)
class RagChunk:
    document_id: int
    source_id: int
    chunk_index: int
    text: str
    citation_id: str
    metadata: SourceMetadata


@dataclass(slots=True)
class RankedChunk:
    chunk_id: int
    score: float


def citation_id(document_id: int, chunk_index: int) -> str:
    return f"D{document_id}-C{chunk_index}"


def chunk_text(
    document_id: int,
    source_id: int,
    text: str,
    metadata: SourceMetadata | dict | None,
    chunk_tokens: int = 800,
    overlap_tokens: int = 120,
) -> list[RagChunk]:
    words = text.split()
    if not words:
        return []
    step = max(1, chunk_tokens - overlap_tokens)
    chunks: list[RagChunk] = []
    normalized_metadata = normalize_metadata(metadata)
    for chunk_index, start in enumerate(range(0, len(words), step)):
        end = min(start + chunk_tokens, len(words))
        chunk_words = words[start:end]
        if not chunk_words:
            continue
        chunks.append(
            RagChunk(
                document_id=document_id,
                source_id=source_id,
                chunk_index=chunk_index,
                text=" ".join(chunk_words),
                citation_id=citation_id(document_id, chunk_index),
                metadata=normalized_metadata,
            )
        )
        if end == len(words):
            break
    return chunks


def vector_to_blob(vector: Iterable[float]) -> bytes:
    return np.asarray(list(vector), dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine_rank(query: np.ndarray, candidates: list[tuple[int, np.ndarray]], top_k: int = 20) -> list[RankedChunk]:
    query_norm = float(np.linalg.norm(query))
    ranked: list[RankedChunk] = []
    if query_norm == 0.0:
        return []
    for chunk_id, vector in candidates:
        vector_norm = float(np.linalg.norm(vector))
        if vector_norm == 0.0:
            score = 0.0
        else:
            score = float(np.dot(query, vector) / (query_norm * vector_norm))
        ranked.append(RankedChunk(chunk_id=chunk_id, score=score))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:top_k]


def pack_context(chunks: list[RagChunk | dict], max_chars: int = 16000) -> str:
    parts: list[str] = []
    total = 0
    for chunk in chunks:
        if isinstance(chunk, dict):
            marker = chunk["citation_id"]
            title = chunk.get("metadata", {}).get("title") or chunk.get("title") or "Untitled"
            text = chunk["text"]
        else:
            marker = chunk.citation_id
            title = chunk.metadata.title
            text = chunk.text
        entry = f"[{marker}] {title}\n{text.strip()}"
        if total + len(entry) > max_chars and parts:
            break
        parts.append(entry)
        total += len(entry)
    return "\n\n".join(parts)

