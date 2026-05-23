import numpy as np
import asyncio
import json
from fastapi.testclient import TestClient

from customchat.api import create_app
from customchat.config import Settings
from customchat.database import Database
from customchat.lemonade_client import VisionProbeResult
from customchat.metadata import SourceMetadata


class FakeLemonade:
    def __init__(self):
        self.embed_model_ids = []
        self.rerank_model_ids = []
        self.get_model_ids = []
        self.probe_model_ids = []
        self.loaded_model_ids = []
        self.chat_payloads = []

    async def list_models(self):
        return {
            "data": [
                {
                    "id": "chat-model",
                    "labels": ["custom"],
                    "max_context_window": 262144,
                    "recipe": "llamacpp",
                    "recipe_options": {"llamacpp_backend": "rocm"},
                },
                {
                    "id": "vision-chat-model",
                    "labels": ["vision", "custom"],
                    "max_context_window": 131072,
                    "recipe": "llamacpp",
                },
                {"id": "embedder", "labels": ["embeddings"], "recipe": "llamacpp"},
                {"id": "reranker", "labels": ["reranking"], "recipe": "llamacpp"},
                {"id": "zerank-2-GGUF-Q8_0", "labels": ["custom"], "recipe": "llamacpp"},
                {"id": "qmd-query-expansion-1.7B-gguf-Q4_K_M", "labels": ["custom"], "recipe": "llamacpp"},
                {"id": "ms-marco-MiniLM-L6-v2-F16-GGUF-F16", "labels": ["custom"], "recipe": "llamacpp"},
                {"id": "speaker", "labels": ["tts"], "recipe": "kokoro"},
            ]
        }

    async def get_model(self, model_id: str):
        self.get_model_ids.append(model_id)
        return {
            "id": model_id,
            "recipe_options": {"ctx_size": 262144, "llamacpp_backend": "rocm", "llamacpp_args": "--mmproj later"},
        }

    async def probe_vision(self, model_id: str):
        self.probe_model_ids.append(model_id)
        return VisionProbeResult(ready=False, reason="mmproj_missing", message="provide the mmproj")

    async def load_chat_model(self, model_id: str):
        self.loaded_model_ids.append(model_id)
        return {"model_id": model_id}

    async def classify(self, model_id: str, title: str, text: str, source_type: str):
        return SourceMetadata(
            document_type="note",
            source_type=source_type,
            title=title,
            summary=text[:80],
            topics=["alpha"],
            tags=["test"],
            confidence=0.9,
        )

    async def embed(self, model_id: str, texts: list[str]):
        self.embed_model_ids.append(model_id)
        vectors = []
        for text in texts:
            vectors.append([1.0, 0.0] if "alpha" in text.lower() else [0.0, 1.0])
        return vectors

    async def rerank(self, model_id: str, query: str, documents: list[str]):
        self.rerank_model_ids.append(model_id)
        return [{"index": index, "relevance_score": float(len(documents) - index)} for index, _ in enumerate(documents)]

    async def chat_stream(self, payload):
        self.chat_payloads.append(payload)
        yield {"choices": [{"delta": {"reasoning_content": "thinking"}}]}
        yield {"choices": [{"delta": {"content": "answer"}}]}
        yield {"type": "done"}


class FakeLemonadeFactory:
    def __init__(self):
        self.base_urls = []
        self.clients = []

    def __call__(self, base_url: str):
        self.base_urls.append(base_url)
        client = FakeLemonade()
        client.base_url = base_url.rstrip("/")
        self.clients.append(client)
        return client


class FakeWebSearch:
    def __init__(self):
        self.queries = []

    async def search(self, query: str, max_results: int = 5):
        self.queries.append(query)
        return [{"title": "web result", "url": "https://example.test", "snippet": "web"}]


class FakeRedditDownloader:
    def __init__(self):
        self.calls = []

    async def download(self, **kwargs):
        self.calls.append(kwargs)
        output_path = kwargs["output_path"]
        kind = kwargs["kind"]
        rows = {
            "post": [
                {
                    "id": "p1",
                    "name": "t3_p1",
                    "subreddit": "theehive",
                    "author": "poster",
                    "created_utc": 1710000000,
                    "author_created_utc": 1700000000,
                    "author_total_karma": 1234,
                    "score": 12,
                    "title": "Alpha synthesis discussion",
                    "selftext": "alpha post evidence",
                    "permalink": "/r/theehive/comments/p1/a/",
                }
            ],
            "comment": [
                {
                    "id": "c1",
                    "name": "t1_c1",
                    "subreddit": "theehive",
                    "author": "commenter",
                    "created_utc": 1710000010,
                    "score": 5,
                    "body": "alpha comment evidence",
                    "link_id": "t3_p1",
                    "parent_id": "t3_p1",
                    "permalink": "/r/theehive/comments/p1/_/c1/",
                }
            ],
        }[kind]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return {"path": str(output_path), "count": len(rows), "bytes": output_path.stat().st_size}


class SlowVisionFakeLemonade(FakeLemonade):
    async def probe_vision(self, model_id: str):
        await asyncio.sleep(0.05)
        return VisionProbeResult(ready=True, reason="ready", message="late")


def test_status_lists_models_without_loading_or_probing_default_model(tmp_path):
    fake = FakeLemonade()
    app = _app(tmp_path, lemonade=fake)
    client = TestClient(app)

    response = client.get("/api/status")

    assert response.status_code == 200
    data = response.json()
    assert data["model"] is None
    assert data["vision"]["ready"] is False
    assert data["vision"]["reason"] == "not_loaded"
    assert fake.get_model_ids == []
    assert fake.probe_model_ids == []


def test_status_lists_selectable_main_llms_only(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.get("/api/status")

    assert response.status_code == 200
    data = response.json()
    assert [model["id"] for model in data["main_models"]] == ["chat-model", "vision-chat-model"]
    assert data["main_models"][0]["context_size"] == 262144
    assert data["main_models"][0]["backend"] == "rocm"
    assert data["embedding"]["id"] == "embedder"
    assert data["reranker"]["id"] == "reranker"


def test_load_model_records_selected_model_for_status(tmp_path):
    fake = FakeLemonade()
    app = _app(tmp_path, lemonade=fake)
    client = TestClient(app)

    load_response = client.post("/api/models/load", json={"model_id": "vision-chat-model"})
    response = client.get("/api/status")

    assert load_response.status_code == 200
    assert load_response.json()["model_id"] == "vision-chat-model"
    assert fake.loaded_model_ids == ["vision-chat-model"]
    assert response.status_code == 200
    assert response.json()["model"]["id"] == "vision-chat-model"


def test_load_model_rejects_missing_model_id(tmp_path):
    fake = FakeLemonade()
    app = _app(tmp_path, lemonade=fake)
    client = TestClient(app)

    response = client.post("/api/models/load", json={"model_id": "   "})

    assert response.status_code == 400
    assert response.json()["detail"] == "Choose a chat model to load."
    assert fake.loaded_model_ids == []


def test_provider_settings_update_rebuilds_active_model_client(tmp_path):
    factory = FakeLemonadeFactory()
    app = _app(tmp_path, lemonade=factory("http://old.example/v1"))
    app.state.lemonade_factory = factory
    client = TestClient(app)

    response = client.put("/api/provider-settings", json={"lemonade_base_url": " http://new.example/v1/ "})
    status = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["lemonade"]["base_url"] == "http://new.example/v1"
    assert factory.base_urls == ["http://old.example/v1", "http://new.example/v1"]
    assert status.status_code == 200
    assert status.json()["lemonade"]["base_url"] == "http://new.example/v1"
    assert app.state.lemonade.base_url == "http://new.example/v1"
    assert app.state.loaded_chat_model_id is None


def test_provider_settings_update_rejects_blank_base_url(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.put("/api/provider-settings", json={"lemonade_base_url": "   "})

    assert response.status_code == 400
    assert response.json()["detail"] == "Enter an OpenAI-compatible base URL."


def test_ingest_text_creates_retrievable_cited_chunk(tmp_path):
    fake = FakeLemonade()
    app = _app(tmp_path, lemonade=fake)
    client = TestClient(app)

    ingest = client.post(
        "/api/ingest",
        json={"kind": "text", "title": "Alpha Note", "text": "alpha evidence " * 30},
    )
    assert ingest.status_code == 200
    assert ingest.json()["processed_count"] == 1

    search = client.post("/api/rag/search", json={"query": "alpha", "top_k": 5})
    assert search.status_code == 200
    data = search.json()
    assert data["results"][0]["citation_id"].startswith("D")
    assert "alpha evidence" in data["packed_context"]
    assert fake.embed_model_ids == ["embedder", "embedder"]
    assert fake.rerank_model_ids == ["reranker"]


def test_archive_import_count_and_status_coverage(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    archive = tmp_path / "r_theehive_posts.jsonl"
    archive.write_text(
        '{"id":"p1","subreddit":"theehive","author":"a","created_utc":1710000000,'
        '"score":1,"title":"P2P update","selftext":"P2P p2p",'
        '"permalink":"/r/theehive/comments/p1/a/"}\n',
        encoding="utf-8",
    )

    imported = client.post("/api/archives/import", json={"path": str(archive), "kind": "post"})
    counted = client.post("/api/archives/count", json={"term": "P2P", "case_sensitive": True})
    status = client.get("/api/status")

    assert imported.status_code == 200
    assert imported.json()["indexed_count"] == 1
    assert counted.status_code == 200
    assert counted.json()["occurrences"] == 2
    assert status.json()["archive"]["items"] == 1
    assert status.json()["archive"]["posts"] == 1
    assert status.json()["archive"]["embedded_items"] == 0


def test_archive_subreddit_summaries_include_metadata_and_rag_counts(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    archive = tmp_path / "r_theehive_mixed.jsonl"
    archive.write_text(
        "\n".join(
            [
                '{"id":"p1","name":"t3_p1","subreddit":"TheEHive","author":"a","created_utc":1710000000,'
                '"score":1,"title":"P2P update","selftext":"P2P p2p",'
                '"permalink":"/r/theehive/comments/p1/a/","_meta":{"topic":"payments","source":"fixture"}}',
                '{"id":"c1","name":"t1_c1","subreddit":"theehive","author":"c","created_utc":1710000002,'
                '"score":3,"body":"comment P2P","link_id":"t3_p1","parent_id":"t3_p1",'
                '"permalink":"/r/theehive/comments/p1/_/c1/","_meta":{"source":"fixture"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    imported = client.post("/api/archives/import", json={"path": str(archive)})
    assert imported.status_code == 200

    database = app.state.database
    post = database.search_reddit_items("", kind="post", subreddit="theehive", limit=1)[0]
    comment = database.search_reddit_items("", kind="comment", subreddit="theehive", limit=1)[0]
    with database.connect() as db:
        db.execute(
            """
            INSERT INTO archive_semantic_chunks(
                item_id, chunk_index, citation_id, text, embedding_model_id, embedding_dimensions
            )
            VALUES (?, 0, 'A1', 'post semantic chunk', 'embedder', 2)
            """,
            (post["id"],),
        )
        db.execute(
            """
            INSERT INTO archive_semantic_chunks(item_id, chunk_index, citation_id, text)
            VALUES (?, 0, 'A2', 'comment semantic chunk')
            """,
            (comment["id"],),
        )

    response = client.get("/api/archives/subreddits")

    assert response.status_code == 200
    data = response.json()
    assert data["coverage"]["items"] == 2
    [summary] = data["subreddits"]
    assert summary["subreddit"] == "theehive"
    assert summary["items"] == 2
    assert summary["posts"] == 1
    assert summary["comments"] == 1
    assert summary["min_created_utc"] == 1710000000
    assert summary["max_created_utc"] == 1710000002
    assert summary["source_files"] == [str(archive)]
    assert summary["latest_import_status"] == "completed"
    assert summary["semantic_chunks"] == 2
    assert summary["embedded_items"] == 1
    assert summary["embedding_model_ids"] == ["embedder"]
    assert summary["embedding_dimensions"] == [2]
    assert summary["metadata_fields"] == ["source", "topic"]


def test_archive_subreddit_html_export_writes_served_escaped_pages(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    archive = tmp_path / "r_theehive_mixed.jsonl"
    archive.write_text(
        "\n".join(
            [
                '{"id":"p1","name":"t3_p1","subreddit":"theehive","author":"a","created_utc":1710000000,'
                '"score":1,"title":"<script>alert(1)</script>","selftext":"post body",'
                '"permalink":"/r/theehive/comments/p1/a/"}',
                '{"id":"c1","name":"t1_c1","subreddit":"theehive","author":"c","created_utc":1710000002,'
                '"score":3,"body":"<b>comment</b>","link_id":"t3_p1","parent_id":"t3_p1",'
                '"permalink":"/r/theehive/comments/p1/_/c1/"}',
                '{"id":"c2","name":"t1_c2","subreddit":"theehive","author":"d","created_utc":1710000003,'
                '"score":4,"body":"nested reply","link_id":"t3_p1","parent_id":"t1_c1",'
                '"permalink":"/r/theehive/comments/p1/_/c2/"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert client.post("/api/archives/import", json={"path": str(archive)}).status_code == 200

    response = client.post("/api/archives/subreddits/THEEHIVE/html-export")

    assert response.status_code == 200
    data = response.json()
    assert data["subreddit"] == "theehive"
    assert data["post_count"] == 1
    assert data["open_url"].endswith("/artifacts/archive-html/theehive/index.html")
    index_path = tmp_path / "data" / "artifacts" / "archive-html" / "theehive" / "index.html"
    assert data["index_path"] == str(index_path)
    assert index_path.exists()
    assert (index_path.parent / "posts").is_dir()

    served = client.get(data["open_url"])
    assert served.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in served.text
    assert "<script>alert(1)</script>" not in served.text
    post_page = next((index_path.parent / "posts").glob("*.html"))
    post_html = post_page.read_text(encoding="utf-8")
    assert "&lt;b&gt;comment&lt;/b&gt;" in post_html
    assert 'id="comment-t1_c1"' in post_html
    assert 'href="#comment-t1_c1"' in post_html
    assert '<ol class="comment-children">' in post_html
    assert "https://www.reddit.com/r/theehive/comments/p1/a/" in post_html
    assert ">permalink</a>" in post_html
    assert str(index_path).startswith(str(tmp_path / "data" / "artifacts"))


def test_archive_purge_clears_reddit_archive_and_generated_html(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    archive = tmp_path / "r_theehive_mixed.jsonl"
    archive.write_text(
        "\n".join(
            [
                '{"id":"p1","name":"t3_p1","subreddit":"theehive","author":"a","created_utc":1710000000,'
                '"score":1,"title":"archive post","selftext":"post body",'
                '"permalink":"/r/theehive/comments/p1/a/"}',
                '{"id":"c1","name":"t1_c1","subreddit":"theehive","author":"c","created_utc":1710000002,'
                '"score":3,"body":"archive comment","link_id":"t3_p1","parent_id":"t3_p1",'
                '"permalink":"/r/theehive/comments/p1/_/c1/"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert client.post("/api/archives/import", json={"path": str(archive)}).status_code == 200
    database = app.state.database
    post = database.search_reddit_items("", kind="post", subreddit="theehive", limit=1)[0]
    source_id = database.upsert_source("scraper", f"{archive}#1", "Loaded row", "loaded-row-hash", "ready")
    other_source_id = database.upsert_source("text", "manual://note", "Manual note", "manual-note-hash", "ready")
    database.insert_source_artifact(source_id, "jsonl-row", str(archive), raw_json={"line": 1})
    document_id = database.insert_document(
        source_id,
        "Loaded row",
        "generic corpus row from r/theehive",
        SourceMetadata(title="Loaded row", source_type="scraper", document_type="reddit"),
    )
    chunk_id = database.insert_chunk(
        document_id,
        source_id,
        0,
        "generic embedded corpus chunk",
        "D1-C0",
        SourceMetadata(title="Loaded row", source_type="scraper", document_type="reddit"),
    )
    database.insert_embedding(chunk_id, "embedder", [1.0, 0.0])
    other_document_id = database.insert_document(
        other_source_id,
        "Manual note",
        "unrelated loaded corpus content",
        SourceMetadata(title="Manual note", source_type="text", document_type="note"),
    )
    other_chunk_id = database.insert_chunk(
        other_document_id,
        other_source_id,
        0,
        "unrelated embedded corpus chunk",
        "D2-C0",
        SourceMetadata(title="Manual note", source_type="text", document_type="note"),
    )
    database.insert_embedding(other_chunk_id, "embedder", [0.0, 1.0])
    with database.connect() as db:
        db.execute(
            """
            INSERT INTO archive_semantic_chunks(
                item_id, chunk_index, citation_id, text, embedding_model_id, embedding_dimensions
            )
            VALUES (?, 0, 'A1', 'post semantic chunk', 'embedder', 2)
            """,
            (post["id"],),
        )
    export = client.post("/api/archives/subreddits/theehive/html-export").json()
    assert (tmp_path / "data" / "artifacts" / "archive-html" / "theehive" / "index.html").exists()

    response = client.post("/api/archives/purge")

    assert response.status_code == 200
    data = response.json()
    assert data["deleted"]["archive_files"] == 1
    assert data["deleted"]["reddit_items"] == 2
    assert data["deleted"]["archive_semantic_chunks"] == 1
    assert data["deleted"]["corpus_sources"] == 2
    assert data["deleted"]["corpus_documents"] == 2
    assert data["deleted"]["corpus_chunks"] == 2
    assert data["deleted"]["corpus_embeddings"] == 2
    assert data["removed_archive_html"] is True
    assert data["coverage"]["items"] == 0
    assert client.get("/api/archives/subreddits").json()["subreddits"] == []
    assert not (tmp_path / "data" / "artifacts" / "archive-html" / "theehive").exists()
    assert client.get(export["open_url"]).status_code == 404
    assert database.count_rows("sources") == 0
    assert database.count_rows("documents") == 0
    assert database.count_rows("chunks") == 0
    assert database.count_rows("embeddings") == 0


def test_reddit_import_job_downloads_imports_metadata_chunks_embeddings_and_eta(tmp_path):
    fake_downloader = FakeRedditDownloader()
    fake_lemonade = FakeLemonade()
    app = _app(
        tmp_path,
        lemonade=fake_lemonade,
        reddit_downloader=fake_downloader,
        run_reddit_imports_inline=True,
    )
    client = TestClient(app)

    started = client.post(
        "/api/reddit-imports",
        json={
            "target_type": "subreddit",
            "target_name": "TheeHive",
            "start_date": "2024-01-01",
            "end_date": "now",
            "include_posts": True,
            "include_comments": True,
        },
    )

    assert started.status_code == 200
    job = client.get(f"/api/reddit-imports/{started.json()['id']}").json()
    assert job["status"] == "completed"
    assert job["current_stage"] == "complete"
    assert job["progress_percent"] == 100
    assert job["eta_label"] == "complete"
    assert job["stage_counts"]["downloaded_items"] == 2
    assert job["stage_counts"]["imported_rows"] == 2
    assert job["stage_counts"]["metadata_rows"] == 2
    assert job["stage_counts"]["semantic_chunks"] >= 2
    assert job["stage_counts"]["embedded_chunks"] == job["stage_counts"]["semantic_chunks"]
    assert {call["kind"] for call in fake_downloader.calls} == {"post", "comment"}

    [summary] = client.get("/api/archives/subreddits").json()["subreddits"]
    assert summary["subreddit"] == "theehive"
    assert summary["semantic_chunks"] >= 2
    assert summary["embedded_items"] == 2
    assert "upvotes" in summary["metadata_fields"]
    assert "poster_karma" in summary["metadata_fields"]
    assert "poster_account_age_days_at_post" in summary["metadata_fields"]
    assert "classifier" in summary["metadata_fields"]

    search = client.post("/api/rag/search", json={"query": "alpha", "top_k": 5})
    assert search.status_code == 200
    data = search.json()
    assert data["results"][0]["citation_id"].startswith("R")
    assert "alpha" in data["packed_context"]
    assert fake_lemonade.embed_model_ids == ["embedder", "embedder"]


def test_clear_subreddit_removes_only_that_loaded_data_and_keeps_chat_history(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    theehive = tmp_path / "data" / "reddit" / "theehive" / "manual" / "posts.jsonl"
    python = tmp_path / "data" / "reddit" / "python" / "manual" / "posts.jsonl"
    theehive.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    theehive.write_text(
        '{"id":"p1","name":"t3_p1","subreddit":"theehive","author":"a","created_utc":1710000000,'
        '"score":1,"title":"remove me","selftext":"theehive content"}\n',
        encoding="utf-8",
    )
    python.write_text(
        '{"id":"p2","name":"t3_p2","subreddit":"python","author":"b","created_utc":1710000001,'
        '"score":2,"title":"keep me","selftext":"python content"}\n',
        encoding="utf-8",
    )
    assert client.post("/api/archives/import", json={"path": str(theehive), "kind": "post"}).status_code == 200
    assert client.post("/api/archives/import", json={"path": str(python), "kind": "post"}).status_code == 200
    conversation = client.post("/api/conversations", json={"title": "Keep chat"}).json()

    response = client.delete("/api/archives/subreddits/theehive")

    assert response.status_code == 200
    data = response.json()
    assert data["subreddit"] == "theehive"
    assert data["deleted"]["reddit_items"] == 1
    assert not theehive.exists()
    assert python.exists()
    summaries = client.get("/api/archives/subreddits").json()["subreddits"]
    assert [summary["subreddit"] for summary in summaries] == ["python"]
    assert client.get(f"/api/conversations/{conversation['id']}").status_code == 200


def test_clear_subreddit_background_job_reports_progress_and_result(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    archive = tmp_path / "data" / "reddit" / "theehive" / "manual" / "posts.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        '{"id":"p1","name":"t3_p1","subreddit":"theehive","author":"a","created_utc":1710000000,'
        '"score":1,"title":"remove me","selftext":"theehive content"}\n',
        encoding="utf-8",
    )
    assert client.post("/api/archives/import", json={"path": str(archive), "kind": "post"}).status_code == 200

    started = client.delete("/api/archives/subreddits/theehive?background=true")

    assert started.status_code == 202
    started_data = started.json()
    assert started_data["subreddit"] == "theehive"
    assert started_data["status"] in {"queued", "running", "completed"}
    assert started_data["message"].startswith("Clearing r/theehive")

    job = client.get(f"/api/archive-clear-jobs/{started_data['id']}")

    assert job.status_code == 200
    job_data = job.json()
    assert job_data["id"] == started_data["id"]
    assert job_data["status"] == "completed"
    assert job_data["result"]["coverage"]["items"] == 0
    assert client.get("/api/archives/subreddits").json()["subreddits"] == []


def test_chat_count_prompt_uses_archive_count_tool(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    archive = tmp_path / "r_theehive_comments.jsonl"
    archive.write_text(
        '{"id":"c1","subreddit":"theehive","author":"c","created_utc":1710000000,'
        '"score":1,"body":"P2P p2p P2P","link_id":"t3_p1","parent_id":"t3_p1",'
        '"permalink":"/r/theehive/comments/p1/_/c1/"}\n',
        encoding="utf-8",
    )
    client.post("/api/archives/import", json={"path": str(archive), "kind": "comment"})

    response = client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": 'How many times is "P2P" mentioned?'}],
            "mode": "thinking",
            "model_id": "chat-model",
            "tools_enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert "archive.count" in body
    assert "3 occurrence" in body
    assert "answer" not in body


def test_chat_rag_query_streams_archive_context(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    archive = tmp_path / "r_theehive_comments.jsonl"
    archive.write_text(
        '{"id":"c1","subreddit":"theehive","author":"c","created_utc":1710000000,'
        '"score":1,"body":"needle evidence from the raw archive","link_id":"t3_p1","parent_id":"t3_p1",'
        '"permalink":"/r/theehive/comments/p1/_/c1/"}\n',
        encoding="utf-8",
    )
    client.post("/api/archives/import", json={"path": str(archive), "kind": "comment"})

    response = client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "Summarize the relevant archive evidence."}],
            "mode": "deep-rag",
            "model_id": "chat-model",
            "query": "needle",
            "tools_enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert "event: archive_context" in body
    assert "needle evidence from the raw archive" in body


def test_chat_uses_loaded_subreddit_archive_instead_of_web_search(tmp_path):
    fake = FakeLemonade()
    fake_web = FakeWebSearch()
    app = _app(tmp_path, lemonade=fake, web_search=fake_web)
    client = TestClient(app)
    archive = tmp_path / "r_theehive_comments.jsonl"
    archive.write_text(
        '{"id":"c1","subreddit":"theehive","author":"c","created_utc":1710000000,'
        '"score":1,"body":"MDMA synthesis datastore evidence","link_id":"t3_p1","parent_id":"t3_p1",'
        '"permalink":"/r/theehive/comments/p1/_/c1/"}\n',
        encoding="utf-8",
    )
    client.post("/api/archives/import", json={"path": str(archive), "kind": "comment"})

    response = client.post(
        "/api/chat/stream",
        json={
            "messages": [
                {"role": "user", "content": "what are the 10 most often discussed drugs to synthesize on theehive?"},
                {"role": "assistant", "content": "generic historical answer"},
                {
                    "role": "user",
                    "content": 'I was referring to the subreddit "r/theehive" currently loaded as a datastore',
                },
            ],
            "mode": "thinking",
            "model_id": "chat-model",
            "tools_enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert "event: archive_context" in body
    assert "MDMA synthesis datastore evidence" in body
    assert fake_web.queries == []
    assert "Use the following local Reddit archive datastore context" in fake.chat_payloads[0]["messages"][0]["content"]


def _app(tmp_path, lemonade=None, web_search=None, reddit_downloader=None, run_reddit_imports_inline=False):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize()
    return create_app(
        settings=settings,
        database=db,
        lemonade=lemonade or FakeLemonade(),
        web_search=web_search,
        reddit_downloader=reddit_downloader,
        run_reddit_imports_inline=run_reddit_imports_inline,
    )


def _settings(tmp_path):
    settings = Settings(
        app_root=tmp_path,
        lemonade_base_url="http://example.test/v1",
        chat_model_id="chat-model",
        embedding_model_id="embedder",
        reranker_model_id="reranker",
        classifier_model_id="chat-model",
        chunk_tokens=20,
        overlap_tokens=5,
    )
    return settings
