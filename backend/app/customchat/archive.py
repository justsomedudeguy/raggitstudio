from __future__ import annotations

import json
import os
import re
import struct
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator

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

    def import_file(self, path: Path, kind: str | None = None) -> ArchiveImportResult:
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
                    if indexed % 1000 == 0:
                        self.database.update_archive_file(
                            file_id,
                            status="running",
                            indexed_count=indexed,
                            failed_count=len(failures),
                            log="\n".join(failures[-20:]),
                        )
                except Exception as exc:  # noqa: BLE001 - keep indexing the rest of the archive
                    failures.append(f"{path}#{line_number}: {exc}")
            flush_pending()
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
