from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from customchat.archive import ArchiveService
from customchat.config import Settings
from customchat.database import Database
from customchat.lemonade_client import LemonadeClient


DOWNLOAD_BASE_URL = "https://arctic-shift.photon-reddit.com/api"


@dataclass(slots=True)
class RedditImportPayload:
    target_type: str
    target_name: str
    start_date: str = "2005-01-01"
    end_date: str = "now"
    include_posts: bool = True
    include_comments: bool = True

    def normalized(self) -> dict[str, Any]:
        target_type = self.target_type.strip().lower()
        if target_type in {"r", "sub", "subreddit"}:
            target_type = "subreddit"
        elif target_type in {"u", "user", "author"}:
            target_type = "user"
        target_name = _clean_target_name(self.target_name)
        if target_type not in {"subreddit", "user"}:
            raise ValueError("Choose subreddit or user.")
        if not target_name:
            raise ValueError("Enter a subreddit or user name.")
        if not self.include_posts and not self.include_comments:
            raise ValueError("Choose posts, comments, or both.")
        return {
            "target_type": target_type,
            "target_name": target_name,
            "start_date": self.start_date.strip() or "2005-01-01",
            "end_date": self.end_date.strip() or "now",
            "include_posts": bool(self.include_posts),
            "include_comments": bool(self.include_comments),
        }


class ArcticShiftDownloader:
    def __init__(self, base_url: str = DOWNLOAD_BASE_URL, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def download(
        self,
        *,
        target_type: str,
        target_name: str,
        kind: str,
        start_date: str,
        end_date: str,
        output_path: Path,
        progress: Callable[[int, int | None, int], None] | None = None,
    ) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        endpoint = f"{self.base_url}/{'posts' if kind == 'post' else 'comments'}/search"
        params: dict[str, Any] = {
            "limit": 100,
            "sort": "asc",
        }
        if target_type == "subreddit":
            params["subreddit"] = target_name
        else:
            params["author"] = target_name
        after = _date_param(start_date)
        before = _date_param(end_date)
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before

        count = 0
        seen_ids: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            with output_path.open("w", encoding="utf-8") as handle:
                while True:
                    response = await client.get(endpoint, params=params)
                    response.raise_for_status()
                    rows = _extract_response_rows(response.json())
                    if not rows:
                        break
                    latest_created: int | None = None
                    wrote_any = False
                    for row in rows:
                        item_key = str(row.get("name") or row.get("id") or "")
                        if item_key and item_key in seen_ids:
                            continue
                        if item_key:
                            seen_ids.add(item_key)
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                        count += 1
                        wrote_any = True
                        created = _int_or_none(row.get("created_utc"))
                        if created is not None:
                            latest_created = created if latest_created is None else max(latest_created, created)
                    handle.flush()
                    if progress:
                        progress(count, None, output_path.stat().st_size)
                    if len(rows) < int(params["limit"]) or latest_created is None or not wrote_any:
                        break
                    before_seconds = _date_to_utc_seconds(end_date)
                    if before_seconds is not None and latest_created >= before_seconds:
                        break
                    params["after"] = latest_created + 1
        return {"path": str(output_path), "count": count, "bytes": output_path.stat().st_size if output_path.exists() else 0}


class RedditImportService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        archive: ArchiveService,
        lemonade: LemonadeClient,
        downloader: Any | None = None,
    ):
        self.settings = settings
        self.database = database
        self.archive = archive
        self.lemonade = lemonade
        self.downloader = downloader or ArcticShiftDownloader()

    def create_job(self, payload: RedditImportPayload) -> dict[str, Any]:
        normalized = payload.normalized()
        job_id = self.database.create_reddit_import_job(normalized)
        return self.database.get_reddit_import_job(job_id) or {"id": job_id}

    async def run_job(self, job_id: int) -> None:
        job = self.database.get_reddit_import_job(job_id)
        if job is None:
            raise KeyError(f"Reddit import job not found: {job_id}")
        payload = dict(job.get("payload") or {})
        started = time.monotonic()
        counts: dict[str, Any] = {
            "downloaded_items": 0,
            "downloaded_bytes": 0,
            "imported_rows": 0,
            "metadata_rows": 0,
            "semantic_chunks": 0,
            "embedded_chunks": 0,
        }

        def update(stage: str, status: str = "running", progress: int = 0, eta_total: int | None = None, log: str = "") -> None:
            eta_seconds, eta_label = _eta(started, _completed_for_eta(counts, stage), eta_total, status)
            self.database.update_reddit_import_job(
                job_id,
                status=status,
                current_stage=stage,
                stage_counts=counts,
                progress_percent=progress,
                eta_seconds=eta_seconds,
                eta_label=eta_label,
                log=log,
                finished=status in {"completed", "failed", "partial"},
            )

        downloaded_paths: list[Path] = []
        try:
            update("downloading", progress=5)
            for kind in _selected_kinds(payload):
                path = self._download_path(payload, kind)
                base_downloaded = int(counts["downloaded_items"])
                base_bytes = int(counts["downloaded_bytes"])

                def progress(downloaded: int, _total: int | None, byte_count: int) -> None:
                    counts["downloaded_items"] = base_downloaded + max(0, downloaded)
                    counts["downloaded_bytes"] = base_bytes + max(0, byte_count)
                    update("downloading", progress=15)

                result = await self.downloader.download(
                    target_type=payload["target_type"],
                    target_name=payload["target_name"],
                    kind=kind,
                    start_date=payload["start_date"],
                    end_date=payload["end_date"],
                    output_path=path,
                    progress=progress,
                )
                downloaded_paths.append(Path(result["path"]))
                counts["downloaded_items"] = base_downloaded + int(result.get("count") or 0)
                counts["downloaded_bytes"] = base_bytes + int(result.get("bytes") or 0)

            update("importing", progress=30, eta_total=max(1, int(counts["downloaded_items"])))
            imported_file_paths: list[str] = []
            for path in downloaded_paths:
                kind = "comment" if "comment" in path.name else "post"
                result = self.archive.import_file(path, kind=kind)
                counts["imported_rows"] = int(counts["imported_rows"]) + result.indexed_count
                imported_file_paths.append(result.path)
                update("importing", progress=45, eta_total=max(1, int(counts["downloaded_items"])))

            subreddits = self.database.archive_subreddits_for_paths(imported_file_paths)
            update("metadata", progress=55, eta_total=max(1, int(counts["imported_rows"])))
            for subreddit in subreddits:
                counts["metadata_rows"] = int(counts["metadata_rows"]) + await self._enrich_subreddit_metadata(subreddit)
                update("metadata", progress=65, eta_total=max(1, int(counts["imported_rows"])))

            update("chunking", progress=72, eta_total=max(1, int(counts["metadata_rows"])))
            chunk_ids: list[int] = []
            for subreddit in subreddits:
                next_chunks = self._build_subreddit_chunks(subreddit)
                chunk_ids.extend(next_chunks)
                counts["semantic_chunks"] = int(counts["semantic_chunks"]) + len(next_chunks)
                update("chunking", progress=82, eta_total=max(1, int(counts["metadata_rows"])))

            update("embedding", progress=88, eta_total=max(1, len(chunk_ids)))
            counts["embedded_chunks"] = await self._embed_chunks(chunk_ids, counts, update)
            update("complete", status="completed", progress=100, eta_total=max(1, int(counts["embedded_chunks"])))
        except Exception as exc:  # noqa: BLE001 - job status should retain the failure
            update("error", status="failed", progress=0, log=str(exc))

    def _download_path(self, payload: dict[str, Any], kind: str) -> Path:
        slug = _safe_slug(f"{payload['target_type']}-{payload['target_name']}")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self.settings.data_dir / "reddit" / slug / timestamp
        return directory / f"{kind}s.jsonl"

    async def _enrich_subreddit_metadata(self, subreddit: str) -> int:
        items = self.database.list_reddit_items_for_subreddit(subreddit)
        by_author = _author_rollups(items)
        processed = 0
        for item in items:
            meta = dict(item.get("meta") or {})
            raw = dict(item.get("raw") or {})
            deterministic = _deterministic_metadata(item, raw, by_author.get(str(item.get("author") or ""), {}))
            meta.update(deterministic)
            try:
                title = str(item.get("title") or f"Reddit {item.get('kind') or 'item'}")
                classified = await self.lemonade.classify(
                    self.settings.classifier_model_id,
                    title,
                    str(item.get("text") or ""),
                    "reddit_archive",
                )
                meta["classifier"] = classified.model_dump()
            except Exception:
                meta.setdefault("classifier", {"status": "unavailable"})
            self.database.update_reddit_item_meta(int(item["id"]), meta)
            processed += 1
        return processed

    def _build_subreddit_chunks(self, subreddit: str) -> list[int]:
        self.database.delete_archive_semantic_for_subreddit(subreddit)
        items = self.database.list_reddit_items_for_subreddit(subreddit)
        posts_by_key, comments_by_key = _thread_indexes(items)
        chunk_ids: list[int] = []
        for item in items:
            text = _contextual_text(item, posts_by_key, comments_by_key)
            if not text.strip():
                continue
            metadata = dict(item.get("meta") or {})
            metadata.setdefault("title", item.get("title") or f"Reddit {item.get('kind') or 'item'}")
            metadata.setdefault("source_type", "reddit_archive")
            for index, chunk_text in enumerate(_split_words(text, self.settings.chunk_tokens, self.settings.overlap_tokens)):
                citation_id = f"R{item['id']}-C{index}"
                chunk_ids.append(
                    self.database.upsert_archive_semantic_chunk(
                        int(item["id"]),
                        index,
                        citation_id,
                        chunk_text,
                        metadata,
                    )
                )
        return chunk_ids

    async def _embed_chunks(self, chunk_ids: list[int], counts: dict[str, Any], update: Callable[..., None]) -> int:
        embedded = 0
        batch_size = 32
        for start in range(0, len(chunk_ids), batch_size):
            batch_ids = chunk_ids[start : start + batch_size]
            chunks = self.database.get_archive_semantic_chunks(batch_ids)
            vectors = await self.lemonade.embed(self.settings.embedding_model_id, [chunk["text"] for chunk in chunks])
            for chunk, vector in zip(chunks, vectors, strict=False):
                self.database.insert_archive_embedding(int(chunk["id"]), self.settings.embedding_model_id, vector)
                embedded += 1
            counts["embedded_chunks"] = embedded
            update("embedding", progress=90 + int(8 * (embedded / max(1, len(chunk_ids)))), eta_total=max(1, len(chunk_ids)))
        return embedded


def _selected_kinds(payload: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    if payload.get("include_posts"):
        kinds.append("post")
    if payload.get("include_comments"):
        kinds.append("comment")
    return kinds


def _clean_target_name(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^[ru]/", "", text, flags=re.IGNORECASE)
    return text.strip()


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-").lower()
    if not slug:
        raise ValueError("Target name must contain at least one safe path character")
    return slug[:120]


def _date_to_utc_seconds(value: str) -> int | None:
    text = (value or "").strip().lower()
    if not text or text == "now":
        return None
    parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _date_param(value: str) -> str | None:
    text = (value or "").strip()
    if not text or text.lower() == "now":
        return None
    datetime.strptime(text, "%Y-%m-%d")
    return text


def _extract_response_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "posts", "comments"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _eta(started: float, completed: int, total: int | None, status: str) -> tuple[int | None, str]:
    if status == "completed":
        return 0, "complete"
    if status == "failed":
        return None, "failed"
    if not total or total <= 0 or completed <= 0:
        return None, "estimating"
    elapsed = max(0.001, time.monotonic() - started)
    rate = completed / elapsed
    if rate <= 0:
        return None, "estimating"
    remaining = max(0, int((total - completed) / rate))
    if remaining == 0:
        return 0, "under 1 minute"
    minutes = max(1, round(remaining / 60))
    return remaining, f"about {minutes} min"


def _completed_for_eta(counts: dict[str, Any], stage: str) -> int:
    key = {
        "downloading": "downloaded_items",
        "importing": "imported_rows",
        "metadata": "metadata_rows",
        "chunking": "semantic_chunks",
        "embedding": "embedded_chunks",
    }.get(stage)
    return int(counts.get(key) or 0) if key else 0


def _author_rollups(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rollups: dict[str, dict[str, Any]] = {}
    for item in items:
        author = str(item.get("author") or "")
        if not author:
            continue
        rollup = rollups.setdefault(
            author,
            {"poster_archive_item_count": 0, "poster_archive_score": 0, "poster_first_seen_utc": None, "poster_last_seen_utc": None},
        )
        rollup["poster_archive_item_count"] += 1
        rollup["poster_archive_score"] += int(item.get("score") or 0)
        created = _int_or_none(item.get("created_utc"))
        if created is not None:
            first = rollup["poster_first_seen_utc"]
            last = rollup["poster_last_seen_utc"]
            rollup["poster_first_seen_utc"] = created if first is None else min(first, created)
            rollup["poster_last_seen_utc"] = created if last is None else max(last, created)
    return rollups


def _deterministic_metadata(item: dict[str, Any], raw: dict[str, Any], author_rollup: dict[str, Any]) -> dict[str, Any]:
    account_created = _first_int(raw, ("author_created_utc", "account_created_utc", "created_utc_author"))
    created = _int_or_none(item.get("created_utc"))
    account_age_days = None
    if account_created is not None and created is not None and created >= account_created:
        account_age_days = int((created - account_created) / 86400)
    return {
        "subreddit": item.get("subreddit") or "",
        "author": item.get("author") or "",
        "kind": item.get("kind") or "",
        "created_utc": created,
        "score": item.get("score"),
        "upvotes": item.get("score"),
        "permalink": item.get("permalink") or "",
        "parent_id": item.get("parent_id") or "",
        "link_id": item.get("link_id") or "",
        "poster_karma": _first_int(
            raw,
            (
                "author_total_karma",
                "total_karma",
                "author_karma",
                "link_karma",
                "comment_karma",
            ),
        ),
        "poster_account_created_utc": account_created,
        "poster_account_age_days_at_post": account_age_days,
        **author_rollup,
    }


def _first_int(raw: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _int_or_none(raw.get(key))
        if value is not None:
            return value
    return None


def _thread_indexes(items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    posts: dict[str, dict[str, Any]] = {}
    comments: dict[str, dict[str, Any]] = {}
    for item in items:
        target = posts if item.get("kind") == "post" else comments
        for key in _thread_keys(item):
            target[key] = item
    return posts, comments


def _thread_keys(item: dict[str, Any]) -> set[str]:
    keys = {str(item.get("item_key") or ""), str(item.get("reddit_id") or "")}
    reddit_id = str(item.get("reddit_id") or "")
    if reddit_id:
        prefix = "t1_" if item.get("kind") == "comment" else "t3_"
        keys.add(reddit_id if reddit_id.startswith(("t1_", "t3_")) else f"{prefix}{reddit_id}")
    return {key for key in keys if key}


def _contextual_text(
    item: dict[str, Any],
    posts_by_key: dict[str, dict[str, Any]],
    comments_by_key: dict[str, dict[str, Any]],
) -> str:
    header = (
        f"In subreddit r/{item.get('subreddit') or 'unknown'}, "
        f"u/{item.get('author') or 'unknown'} posted a {item.get('kind') or 'item'} "
        f"with score/upvotes {item.get('score') if item.get('score') is not None else 'unknown'}."
    )
    if item.get("kind") == "post":
        return "\n\n".join(part for part in [header, str(item.get("title") or ""), str(item.get("selftext") or item.get("text") or "")] if part)

    link_id = str(item.get("link_id") or "")
    parent_id = str(item.get("parent_id") or "")
    post = posts_by_key.get(link_id)
    parent = comments_by_key.get(parent_id)
    context = [header]
    if post:
        context.append(f"Thread title: {post.get('title') or '(untitled post)'}")
        if post.get("selftext"):
            context.append(f"Post body: {post.get('selftext')}")
    if parent:
        context.append(f"Parent comment from u/{parent.get('author') or 'unknown'}: {parent.get('body') or parent.get('text') or ''}")
    context.append(f"Comment text: {item.get('body') or item.get('text') or ''}")
    return "\n\n".join(part for part in context if str(part).strip())


def _split_words(text: str, chunk_tokens: int, overlap_tokens: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    step = max(1, chunk_tokens - overlap_tokens)
    chunks: list[str] = []
    for start in range(0, len(words), step):
        end = min(start + chunk_tokens, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
    return chunks
