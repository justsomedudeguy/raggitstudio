from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup


@dataclass(slots=True)
class SourcePayload:
    source_type: str
    uri: str
    text: str
    title: str = "Untitled"
    url: str | None = None
    path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def content_hash(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    for tag in soup(["nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else "Untitled"
    node = soup.find("main") or soup.body or soup
    text = node.get_text(" ", strip=True)
    return title or "Untitled", " ".join(text.split())


def parse_scraper_jsonl(path: Path) -> list[SourcePayload]:
    payloads: list[SourcePayload] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            payloads.append(_payload_from_mapping(raw, fallback_uri=f"{path}#{line_number}"))
    return payloads


def parse_json_file(path: Path) -> list[SourcePayload]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [_payload_from_mapping(item, fallback_uri=f"{path}#{index}") for index, item in enumerate(raw)]
    if isinstance(raw, dict):
        return [_payload_from_mapping(raw, fallback_uri=str(path))]
    return [SourcePayload(source_type="json", uri=str(path), path=str(path), text=str(raw), raw={"value": raw})]


def extract_text_from_file(path: Path) -> list[SourcePayload]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        return [SourcePayload(source_type="file", uri=str(path), path=str(path), title=path.name, text=text)]
    if suffix in {".html", ".htm"}:
        html = path.read_text(encoding="utf-8", errors="replace")
        title, text = normalize_html(html)
        return [SourcePayload(source_type="file", uri=str(path), path=str(path), title=title, text=text, raw={"html": html})]
    if suffix == ".jsonl":
        return parse_scraper_jsonl(path)
    if suffix == ".json":
        return parse_json_file(path)
    if suffix == ".csv":
        return [_payload_from_csv(path)]
    if suffix == ".pdf":
        return [_payload_from_pdf(path)]
    if suffix == ".docx":
        return [_payload_from_docx(path)]
    text = path.read_text(encoding="utf-8", errors="replace")
    return [SourcePayload(source_type="file", uri=str(path), path=str(path), title=path.name, text=text)]


def _payload_from_mapping(raw: dict[str, Any], fallback_uri: str) -> SourcePayload:
    url = raw.get("url") or raw.get("canonical_url")
    uri = canonicalize_url(url) if url else fallback_uri
    html = raw.get("html") or raw.get("content_html")
    title = raw.get("title") or "Untitled"
    text = raw.get("text") or raw.get("markdown") or raw.get("content") or raw.get("body")
    if not text and html:
        title_from_html, text = normalize_html(str(html))
        if title == "Untitled":
            title = title_from_html
    text = str(text or "")
    return SourcePayload(
        source_type="web" if url else "scraper",
        uri=uri,
        url=uri if url else None,
        title=str(title),
        text=text,
        raw=raw,
    )


def _payload_from_csv(path: Path) -> SourcePayload:
    rows: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(" | ".join(f"{key}: {value}" for key, value in row.items()))
    return SourcePayload(source_type="file", uri=str(path), path=str(path), title=path.name, text="\n".join(rows))


def _payload_from_pdf(path: Path) -> SourcePayload:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return SourcePayload(source_type="file", uri=str(path), path=str(path), title=path.name, text="\n\n".join(pages))


def _payload_from_docx(path: Path) -> SourcePayload:
    from docx import Document

    document = Document(str(path))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())
    return SourcePayload(source_type="file", uri=str(path), path=str(path), title=path.name, text=text)

