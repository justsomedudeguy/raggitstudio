from pathlib import Path

from customchat.ingestion import (
    SourcePayload,
    canonicalize_url,
    content_hash,
    normalize_html,
    parse_scraper_jsonl,
)


def test_canonicalize_url_removes_fragments_and_sorts_query_params():
    result = canonicalize_url("HTTPS://Example.com:443/path/?b=2&a=1#section")

    assert result == "https://example.com/path/?a=1&b=2"


def test_normalize_html_extracts_title_and_main_text():
    html = """
    <html>
      <head><title>Example Page</title><script>ignored()</script></head>
      <body><nav>Menu</nav><main><h1>Heading</h1><p>Useful text.</p></main></body>
    </html>
    """

    title, text = normalize_html(html)

    assert title == "Example Page"
    assert "Heading" in text
    assert "Useful text." in text
    assert "ignored" not in text


def test_parse_scraper_jsonl_preserves_url_and_raw_payload(tmp_path: Path):
    source = tmp_path / "pages.jsonl"
    source.write_text(
        '{"url":"https://example.com/a","title":"A","text":"First page"}\n'
        '{"url":"https://example.com/b","html":"<title>B</title><p>Second page</p>"}\n',
        encoding="utf-8",
    )

    payloads = parse_scraper_jsonl(source)

    assert [p.url for p in payloads] == ["https://example.com/a", "https://example.com/b"]
    assert [p.title for p in payloads] == ["A", "B"]
    assert payloads[0].text == "First page"
    assert "Second page" in payloads[1].text
    assert isinstance(payloads[0], SourcePayload)


def test_content_hash_is_stable_for_same_content():
    assert content_hash("same text") == content_hash("same text")
    assert content_hash("same text") != content_hash("different text")

