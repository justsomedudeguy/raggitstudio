from __future__ import annotations

import json
import os
import re
import shutil
import struct
import traceback
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator
from urllib.parse import urlsplit

from customchat.database import Database

try:
    import orjson
except ImportError:  # pragma: no cover - optional speedup
    orjson = None


@dataclass(slots=True)
class RedditArchiveItem:
    archive_file_id: int
    line_number: int
    item_key: str
    reddit_id: str
    kind: str
    subreddit: str
    author: str
    created_utc: int | None
    score: int | None
    title: str
    selftext: str
    body: str
    url: str
    permalink: str
    link_id: str
    parent_id: str
    text: str
    raw: dict[str, Any]
    meta: dict[str, Any]


@dataclass(slots=True)
class ArchiveImportResult:
    file_id: int
    path: str
    file_format: str
    kind: str | None
    status: str
    indexed_count: int
    failed_count: int
    log: str


@dataclass(slots=True)
class ArchiveCountResult:
    term: str
    case_sensitive: bool
    occurrences: int
    matched_items: int
    searched_items: int
    by_kind: dict[str, int]


@dataclass(slots=True)
class ArchiveHtmlExportResult:
    subreddit: str
    index_path: str
    artifact_path: str
    post_count: int


def iter_archive_rows(path: Path) -> Iterator[dict[str, Any]]:
    suffix = path.suffix.lower()
    with path.open("rb") as handle:
        if suffix in {".jsonl", ".ndjson"}:
            yield from _get_json_lines_file_json_stream(handle)
            return
        if suffix == ".zst":
            yield from _get_zst_file_json_stream(handle)
            return
        if suffix == ".zst_blocks":
            yield from _get_zst_blocks_file_json_stream(handle)
            return
        if suffix == ".json":
            yield from _get_json_file_stream(handle)
            return
    raise ValueError(f"Unsupported archive file type: {path.suffix}")


def normalize_reddit_item(
    raw: dict[str, Any],
    fallback_kind: str | None,
    archive_file_id: int,
    line_number: int,
) -> RedditArchiveItem:
    kind = _infer_kind(raw, fallback_kind)
    reddit_id = str(raw.get("id") or "").strip()
    item_key = str(raw.get("name") or _prefixed_id(kind, reddit_id) or f"archive:{archive_file_id}:{line_number}")
    title = _clean_text(raw.get("title"))
    selftext = _clean_text(raw.get("selftext"))
    body = _clean_text(raw.get("body"))
    if kind == "post":
        text = "\n\n".join(part for part in [title, selftext] if part)
    else:
        text = body
    meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    return RedditArchiveItem(
        archive_file_id=archive_file_id,
        line_number=line_number,
        item_key=item_key,
        reddit_id=reddit_id,
        kind=kind,
        subreddit=_clean_text(raw.get("subreddit")),
        author=_clean_text(raw.get("author")),
        created_utc=_int_or_none(raw.get("created_utc")),
        score=_int_or_none(raw.get("score")),
        title=title,
        selftext=selftext,
        body=body,
        url=_clean_text(raw.get("url")),
        permalink=_clean_text(raw.get("permalink") or raw.get("permalink_url")),
        link_id=_clean_text(raw.get("link_id")),
        parent_id=_clean_text(raw.get("parent_id")),
        text=text,
        raw=raw,
        meta=meta,
    )


def format_archive_context(results: list[dict[str, Any]], max_chars: int = 12000) -> str:
    parts: list[str] = []
    used = 0
    for result in results:
        citation = f"[R{result['id']}]"
        title = result.get("title") or result.get("body", "")[:80] or "Reddit item"
        line = (
            f"{citation} {result.get('kind', 'item')} r/{result.get('subreddit', 'unknown')} "
            f"u/{result.get('author', 'unknown')} score={result.get('score')}: {title}\n"
            f"{result.get('text', '')}"
        )
        if used + len(line) > max_chars:
            break
        parts.append(line)
        used += len(line)
    return "\n\n".join(parts)


class ArchiveService:
    def __init__(self, database: Database):
        self.database = database

    def import_file(
        self,
        path: Path,
        kind: str | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> ArchiveImportResult:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        file_format = _file_format(path)
        fallback_kind = _normalize_kind(kind) or _kind_from_filename(path)
        file_id = self.database.create_archive_file(str(path), file_format, fallback_kind)
        indexed = 0
        failures: list[str] = []
        pending: list[dict[str, Any]] = []

        def flush_pending() -> None:
            nonlocal pending
            if pending:
                self.database.upsert_reddit_items(pending)
                pending = []

        try:
            for line_number, raw in enumerate(iter_archive_rows(path), start=1):
                try:
                    item = normalize_reddit_item(raw, fallback_kind, file_id, line_number)
                    pending.append(asdict(item))
                    indexed += 1
                    if len(pending) >= 500:
                        flush_pending()
                        if progress:
                            progress(indexed, len(failures))
                    if indexed % 1000 == 0:
                        self.database.update_archive_file(
                            file_id,
                            status="running",
                            indexed_count=indexed,
                            failed_count=len(failures),
                            log="\n".join(failures[-20:]),
                        )
                        if progress:
                            progress(indexed, len(failures))
                except Exception as exc:  # noqa: BLE001 - keep indexing the rest of the archive
                    failures.append(f"{path}#{line_number}: {exc}")
            flush_pending()
            if progress:
                progress(indexed, len(failures))
            status = "completed" if not failures else "partial"
            log = "\n".join(failures)
            self.database.update_archive_file(file_id, status, indexed, len(failures), log, finished=True)
            return ArchiveImportResult(file_id, str(path), file_format, fallback_kind, status, indexed, len(failures), log)
        except Exception as exc:  # noqa: BLE001
            self.database.update_archive_file(file_id, "failed", indexed, len(failures) + 1, str(exc), finished=True)
            return ArchiveImportResult(file_id, str(path), file_format, fallback_kind, "failed", indexed, len(failures) + 1, str(exc))

    def list_files(self) -> list[dict[str, Any]]:
        return self.database.list_archive_files()

    def coverage(self) -> dict[str, int]:
        return self.database.archive_coverage()

    def purge(self, artifacts_dir: Path) -> dict[str, Any]:
        deleted = self.database.purge_loaded_data()
        archive_html_dir = Path(artifacts_dir) / "archive-html"
        removed_archive_html = archive_html_dir.exists()
        if removed_archive_html:
            _ensure_within(archive_html_dir, Path(artifacts_dir))
            shutil.rmtree(archive_html_dir)
        archive_html_dir.mkdir(parents=True, exist_ok=True)
        return {
            "deleted": deleted,
            "removed_archive_html": removed_archive_html,
            "coverage": self.coverage(),
        }

    def purge_subreddit(self, subreddit: str, artifacts_dir: Path, data_dir: Path) -> dict[str, Any]:
        summary = self.database.get_archive_subreddit_summary(subreddit)
        if summary is None:
            raise KeyError(f"Subreddit not found: {subreddit}")
        result = self.database.purge_subreddit_data(subreddit)
        removed_files: list[str] = []
        skipped_files: list[str] = []
        for path_value in result["source_files"]:
            path = Path(path_value)
            try:
                _ensure_within(path, Path(data_dir))
            except ValueError:
                skipped_files.append(str(path))
                continue
            if path.exists() and path.is_file():
                path.unlink()
                removed_files.append(str(path))
                _remove_empty_parents(path.parent, Path(data_dir))

        export_root = Path(artifacts_dir) / "archive-html"
        output_dir = export_root / _safe_subreddit_slug(summary["subreddit"])
        removed_archive_html = False
        if output_dir.exists():
            _ensure_within(output_dir, export_root)
            shutil.rmtree(output_dir)
            removed_archive_html = True
        return {
            "subreddit": summary["subreddit"],
            "deleted": result["deleted"],
            "removed_files": removed_files,
            "skipped_files": skipped_files,
            "removed_archive_html": removed_archive_html,
            "coverage": self.coverage(),
        }

    def subreddit_summaries(self) -> list[dict[str, Any]]:
        return self.database.list_archive_subreddit_summaries()

    def export_subreddit_html(self, subreddit: str, artifacts_dir: Path) -> ArchiveHtmlExportResult:
        summary = self.database.get_archive_subreddit_summary(subreddit)
        if summary is None:
            raise KeyError(f"Subreddit not found: {subreddit}")
        export_root = Path(artifacts_dir) / "archive-html"
        export_root.mkdir(parents=True, exist_ok=True)
        output_dir = export_root / _safe_subreddit_slug(summary["subreddit"])
        _ensure_within(output_dir, export_root)
        posts_dir = output_dir / "posts"
        posts_dir.mkdir(parents=True, exist_ok=True)

        posts = self.database.list_reddit_posts_for_subreddit(summary["subreddit"])
        comments = self.database.list_reddit_comments_for_subreddit(summary["subreddit"])
        comments_by_link_id: dict[str, list[dict[str, Any]]] = {}
        for comment in comments:
            link_id = str(comment.get("link_id") or "")
            comments_by_link_id.setdefault(link_id, []).append(comment)

        post_links: list[tuple[dict[str, Any], str, list[dict[str, Any]]]] = []
        for post in posts:
            post_comments = _comments_for_post(post, comments_by_link_id)
            filename = _post_filename(post)
            _write_post_page(posts_dir / filename, summary, post, post_comments)
            post_links.append((post, f"posts/{filename}", post_comments))

        index_path = output_dir / "index.html"
        _write_index_page(index_path, summary, post_links)
        artifact_path = index_path.relative_to(Path(artifacts_dir)).as_posix()
        return ArchiveHtmlExportResult(summary["subreddit"], str(index_path), artifact_path, len(posts))

    def count_occurrences(
        self,
        term: str,
        case_sensitive: bool = False,
        kind: str | None = None,
        subreddit: str | None = None,
    ) -> ArchiveCountResult:
        if not term:
            raise ValueError("Count term is required")
        occurrence_count = 0
        matched_items = 0
        searched_items = 0
        by_kind: dict[str, int] = {}
        needle = term if case_sensitive else term.casefold()
        for row in self.database.iter_reddit_item_texts(kind=_normalize_kind(kind), subreddit=subreddit):
            searched_items += 1
            haystack = row["text"] or ""
            count = haystack.count(needle) if case_sensitive else haystack.casefold().count(needle)
            if count:
                matched_items += 1
                occurrence_count += count
                row_kind = str(row["kind"])
                by_kind[row_kind] = by_kind.get(row_kind, 0) + count
        return ArchiveCountResult(term, case_sensitive, occurrence_count, matched_items, searched_items, dict(sorted(by_kind.items())))

    def search(
        self,
        query: str,
        limit: int = 20,
        kind: str | None = None,
        subreddit: str | None = None,
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.database.search_reddit_items(
            query=query,
            limit=limit,
            kind=_normalize_kind(kind),
            subreddit=subreddit,
            after=after,
            before=before,
        )


# The streaming helpers below are adapted from Arthur Heitmann's Arctic Shift
# helper scripts and zst_blocks_format project, with local error handling added.
def _get_zst_file_json_stream(handle: BinaryIO, chunk_size: int = 1024 * 1024 * 10) -> Iterator[dict[str, Any]]:
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError("Install zstandard to import .zst Arctic Shift archives") from exc
    decompressor = zstandard.ZstdDecompressor(max_window_size=2**31)
    current = ""

    def yield_lines_json() -> Iterator[dict[str, Any]]:
        nonlocal current
        lines = current.split("\n")
        current = lines[-1]
        for line in lines[:-1]:
            if not line:
                continue
            try:
                yield _json_loads(line)
            except json.JSONDecodeError:
                print("Error parsing line: " + line)
                traceback.print_exc()

    reader = decompressor.stream_reader(handle)
    while True:
        try:
            chunk = reader.read(chunk_size)
        except zstandard.ZstdError:
            print("Error reading zst chunk")
            traceback.print_exc()
            break
        if not chunk:
            break
        current += chunk.decode("utf-8", "replace")
        yield from yield_lines_json()
    yield from yield_lines_json()
    if current:
        yield _json_loads(current)


def _get_json_lines_file_json_stream(handle: BinaryIO) -> Iterator[dict[str, Any]]:
    for line in handle:
        line_text = line.decode("utf-8", errors="replace").strip()
        if not line_text:
            continue
        try:
            yield _json_loads(line_text)
        except json.JSONDecodeError:
            print("Error parsing line: " + line_text)
            traceback.print_exc()


def _get_json_file_stream(handle: BinaryIO) -> Iterator[dict[str, Any]]:
    data = _json_loads(handle.read())
    if isinstance(data, list):
        yield from data
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        yield from data["data"]
    elif isinstance(data, dict):
        yield data
    else:
        raise ValueError("JSON archive must contain an object, array, or data array")


def _get_zst_blocks_file_json_stream(handle: BinaryIO) -> Iterator[dict[str, Any]]:
    for row in _stream_zst_block_rows(handle):
        line = row.decode("utf-8", errors="replace")
        try:
            yield _json_loads(line)
        except json.JSONDecodeError:
            print("Error parsing line: " + line)
            traceback.print_exc()


def _stream_zst_block_rows(handle: BinaryIO) -> Iterable[bytes]:
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError("Install zstandard to import .zst_blocks Arctic Shift archives") from exc
    uint32 = struct.Struct("<I")
    uint32_pair = struct.Struct("<II")
    file_size = os.path.getsize(handle.name)
    while handle.tell() < file_size:
        compressed_size_bytes = handle.read(4)
        if len(compressed_size_bytes) < 4:
            break
        compressed_size = uint32.unpack(compressed_size_bytes)[0]
        compressed_data = handle.read(compressed_size)
        decompressed = zstandard.ZstdDecompressor().decompress(compressed_data)
        view = memoryview(decompressed)
        row_count = uint32.unpack(view[0:4])[0]
        data_start = 4 + row_count * 8
        for index in range(row_count):
            offset, size = uint32_pair.unpack(view[4 + index * 8 : 12 + index * 8])
            yield decompressed[data_start + offset : data_start + offset + size]


def _json_loads(data: str | bytes) -> Any:
    if orjson is not None:
        return orjson.loads(data)
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return json.loads(data)


def _file_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"jsonl", "ndjson", "zst", "zst_blocks", "json"}:
        return suffix
    raise ValueError(f"Unsupported archive file type: {path.suffix}")


def _kind_from_filename(path: Path) -> str | None:
    name = path.name.lower()
    if "comment" in name or name.startswith("rc_"):
        return "comment"
    if "post" in name or "submission" in name or name.startswith("rs_"):
        return "post"
    return None


def _infer_kind(raw: dict[str, Any], fallback_kind: str | None) -> str:
    name = str(raw.get("name") or "")
    if name.startswith("t1_") or "body" in raw:
        return "comment"
    if name.startswith("t3_") or "selftext" in raw or "title" in raw:
        return "post"
    return _normalize_kind(fallback_kind) or "post"


def _normalize_kind(kind: str | None) -> str | None:
    if not kind:
        return None
    value = kind.lower().strip()
    if value in {"comment", "comments", "rc"}:
        return "comment"
    if value in {"post", "posts", "submission", "submissions", "rs"}:
        return "post"
    return value


def _prefixed_id(kind: str, reddit_id: str) -> str:
    if not reddit_id:
        return ""
    if reddit_id.startswith(("t1_", "t3_")):
        return reddit_id
    return f"{'t1' if kind == 'comment' else 't3'}_{reddit_id}"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _write_index_page(path: Path, summary: dict[str, Any], post_links: list[tuple[dict[str, Any], str, list[dict[str, Any]]]]) -> None:
    post_rows = "\n".join(
        "<li>"
        f"<a href=\"{_attr(href)}\">{_html(post.get('title') or '(untitled post)')}</a> "
        f"<span>{_html(_item_meta(post))}</span> "
        f"<span>{len(comments)} comment{'s' if len(comments) != 1 else ''}</span>"
        "</li>"
        for post, href, comments in post_links
    )
    if not post_rows:
        post_rows = "<li>No posts found for this subreddit.</li>"
    source_files = "".join(f"<li>{_html(path_value)}</li>" for path_value in summary["source_files"]) or "<li>None</li>"
    metadata_fields = ", ".join(summary["metadata_fields"]) or "none"
    embedding_models = ", ".join(summary["embedding_model_ids"]) or "none"
    embedding_dimensions = ", ".join(str(value) for value in summary["embedding_dimensions"]) or "none"
    body = f"""
<header>
  <h1>r/{_html(summary['subreddit'])}</h1>
  <p>{summary['items']} indexed items, {summary['posts']} posts, {summary['comments']} comments</p>
</header>
<section>
  <h2>Database metadata</h2>
  <dl>
    <dt>Created UTC range</dt><dd>{_html(_created_range(summary))}</dd>
    <dt>Latest import</dt><dd>{_html(summary.get('latest_import_status') or 'unknown')} at {_html(summary.get('latest_import_at') or 'unknown')}</dd>
    <dt>Source files</dt><dd><ul>{source_files}</ul></dd>
  </dl>
</section>
<section>
  <h2>RAG metadata</h2>
  <dl>
    <dt>Semantic chunks</dt><dd>{summary['semantic_chunks']}</dd>
    <dt>Embedded items</dt><dd>{summary['embedded_items']}</dd>
    <dt>Embedding models</dt><dd>{_html(embedding_models)}</dd>
    <dt>Embedding dimensions</dt><dd>{_html(embedding_dimensions)}</dd>
    <dt>Metadata fields</dt><dd>{_html(metadata_fields)}</dd>
  </dl>
</section>
<section>
  <h2>Posts</h2>
  <ol>{post_rows}</ol>
</section>
"""
    path.write_text(_page(f"r/{summary['subreddit']} archive", body), encoding="utf-8")


def _write_post_page(path: Path, summary: dict[str, Any], post: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    reddit_link = _reddit_permalink(post.get("permalink"))
    live_link = f"<a class=\"permalink\" href=\"{_attr(reddit_link)}\">permalink</a>" if reddit_link else ""
    comment_items = _comments_html(post, comments)
    body = f"""
<nav><a href="../index.html">Back to index</a></nav>
<article>
  <h1>{_html(post.get('title') or '(untitled post)')}</h1>
  <p>{_html(_item_meta(post))} {live_link}</p>
  <div class="text">{_block_text(post.get('selftext') or post.get('text') or '')}</div>
</article>
<section>
  <h2>Comments</h2>
  <ol class="comments">{comment_items}</ol>
</section>
"""
    path.write_text(_page(f"{post.get('title') or 'Post'} - r/{summary['subreddit']}", body), encoding="utf-8")


def _comments_html(post: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    if not comments:
        return "<li>No comments indexed for this post.</li>"
    comments_by_key = _comments_by_thread_key(comments)
    children_by_parent: dict[int, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    post_keys = _post_thread_keys(post)
    for comment in comments:
        parent_id = str(comment.get("parent_id") or "")
        parent = comments_by_key.get(parent_id)
        if parent:
            children_by_parent.setdefault(int(parent["id"]), []).append(comment)
        elif not parent_id or parent_id in post_keys:
            roots.append(comment)
        else:
            roots.append(comment)
    return "\n".join(_comment_html(comment, children_by_parent, post_keys, comments_by_key) for comment in roots)


def _comments_by_thread_key(comments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for comment in comments:
        for key in _comment_thread_keys(comment):
            by_key[key] = comment
    return by_key


def _comment_thread_keys(comment: dict[str, Any]) -> set[str]:
    keys = {str(comment.get("item_key") or ""), str(comment.get("reddit_id") or "")}
    reddit_id = str(comment.get("reddit_id") or "")
    if reddit_id:
        keys.add(reddit_id if reddit_id.startswith("t1_") else f"t1_{reddit_id}")
    return {key for key in keys if key}


def _comment_html(
    comment: dict[str, Any],
    children_by_parent: dict[int, list[dict[str, Any]]],
    post_keys: set[str],
    comments_by_key: dict[str, dict[str, Any]],
) -> str:
    reddit_link = _reddit_permalink(comment.get("permalink"))
    live_link = f" <a class=\"permalink\" href=\"{_attr(reddit_link)}\">permalink</a>" if reddit_link else ""
    anchor = _comment_anchor(comment)
    parent_link = _parent_comment_link(comment, post_keys, comments_by_key)
    children = children_by_parent.get(int(comment["id"]), [])
    child_html = ""
    if children:
        child_items = "\n".join(_comment_html(child, children_by_parent, post_keys, comments_by_key) for child in children)
        child_html = f"<ol class=\"comment-children\">{child_items}</ol>"
    return (
        f"<li id=\"{_attr(anchor)}\">"
        f"<p>{_html(_item_meta(comment))}{parent_link}{live_link}</p>"
        f"<div class=\"text\">{_block_text(comment.get('body') or comment.get('text') or '')}</div>"
        f"{child_html}"
        "</li>"
    )


def _parent_comment_link(comment: dict[str, Any], post_keys: set[str], comments_by_key: dict[str, dict[str, Any]]) -> str:
    parent_id = str(comment.get("parent_id") or "")
    if not parent_id or parent_id in post_keys:
        return ""
    parent = comments_by_key.get(parent_id)
    if not parent:
        return f" <span class=\"parent-ref\">in reply to {_html(parent_id)}</span>"
    return f" <span class=\"parent-ref\">in reply to <a href=\"#{_attr(_comment_anchor(parent))}\">{_html(parent_id)}</a></span>"


def _comment_anchor(comment: dict[str, Any]) -> str:
    key = str(comment.get("item_key") or comment.get("reddit_id") or comment.get("id") or "comment")
    return f"comment-{_safe_file_component(key)}"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(title)}</title>
  <style>
    body {{ color: #111; background: #fff; font: 14px/1.45 Arial, sans-serif; margin: 0 auto; max-width: 980px; padding: 24px; }}
    a {{ color: #0645ad; }}
    header, section, article, nav {{ border-bottom: 1px solid #ddd; padding: 12px 0; }}
    h1, h2 {{ margin: 0 0 8px; }}
    dl {{ display: grid; grid-template-columns: 180px 1fr; gap: 6px 12px; }}
    dt {{ font-weight: bold; }}
    dd {{ margin: 0; }}
    li {{ margin: 0 0 12px; }}
    .comments, .comment-children {{ padding-left: 22px; }}
    .comment-children {{ border-left: 2px solid #ddd; margin-top: 10px; }}
    .parent-ref, .permalink {{ font-size: 12px; }}
    .text {{ white-space: normal; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _comments_for_post(post: dict[str, Any], comments_by_link_id: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for key in _post_thread_keys(post):
        for comment in comments_by_link_id.get(key, []):
            comment_id = int(comment["id"])
            if comment_id not in seen_ids:
                comments.append(comment)
                seen_ids.add(comment_id)
    return comments


def _post_thread_keys(post: dict[str, Any]) -> set[str]:
    keys = {str(post.get("item_key") or ""), str(post.get("reddit_id") or "")}
    reddit_id = str(post.get("reddit_id") or "")
    if reddit_id:
        keys.add(reddit_id if reddit_id.startswith("t3_") else f"t3_{reddit_id}")
    return {key for key in keys if key}


def _post_filename(post: dict[str, Any]) -> str:
    name = str(post.get("reddit_id") or post.get("item_key") or post.get("id") or "post")
    return f"{int(post['id'])}-{_safe_file_component(name)}.html"


def _safe_subreddit_slug(subreddit: str) -> str:
    return _safe_file_component(subreddit.lower().removeprefix("r/"))


def _safe_file_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-").lower()
    if not safe:
        raise ValueError("Subreddit must contain at least one safe path character")
    return safe[:100]


def _ensure_within(path: Path, root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Export path escaped the artifacts directory") from exc


def _remove_empty_parents(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    current = path.resolve()
    while current != resolved_root:
        try:
            current.relative_to(resolved_root)
        except ValueError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _reddit_permalink(value: Any) -> str:
    permalink = _clean_text(value).strip()
    if not permalink:
        return ""
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink}"
    parsed = urlsplit(permalink)
    if parsed.scheme in {"http", "https"} and parsed.netloc.endswith("reddit.com"):
        return permalink
    return ""


def _item_meta(item: dict[str, Any]) -> str:
    parts = [
        f"u/{item.get('author') or 'unknown'}",
        f"score={item.get('score') if item.get('score') is not None else 'unknown'}",
    ]
    if item.get("created_utc") is not None:
        parts.append(f"created_utc={item['created_utc']}")
    return " ".join(parts)


def _created_range(summary: dict[str, Any]) -> str:
    start = summary.get("min_created_utc")
    end = summary.get("max_created_utc")
    return f"{start if start is not None else 'unknown'} to {end if end is not None else 'unknown'}"


def _block_text(value: Any) -> str:
    escaped = _html(_clean_text(value))
    return "<br>".join(escaped.split("\n")) if escaped else ""


def _html(value: Any) -> str:
    return escape(str(value), quote=False)


def _attr(value: Any) -> str:
    return escape(str(value), quote=True)


def extract_count_term(prompt: str) -> str | None:
    lowered = prompt.lower()
    if not any(marker in lowered for marker in ("how many", "count", "occurrence", "occurrences", "mentioned", "mentions")):
        return None
    quoted = re.search(r"[\"']([^\"']{1,120})[\"']", prompt)
    if quoted:
        return quoted.group(1).strip()
    term_match = re.search(r"\bterm\s+([A-Za-z0-9_.:/#-]{1,80})", prompt, flags=re.IGNORECASE)
    if term_match:
        return term_match.group(1).strip()
    return None
