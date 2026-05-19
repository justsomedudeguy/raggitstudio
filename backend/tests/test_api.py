import numpy as np
import asyncio
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
        return {
            "id": model_id,
            "recipe_options": {"ctx_size": 262144, "llamacpp_backend": "rocm", "llamacpp_args": "--mmproj later"},
        }

    async def probe_vision(self, model_id: str):
        return VisionProbeResult(ready=False, reason="mmproj_missing", message="provide the mmproj")

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
        yield {"choices": [{"delta": {"reasoning_content": "thinking"}}]}
        yield {"choices": [{"delta": {"content": "answer"}}]}
        yield {"type": "done"}


class SlowVisionFakeLemonade(FakeLemonade):
    async def probe_vision(self, model_id: str):
        await asyncio.sleep(0.05)
        return VisionProbeResult(ready=True, reason="ready", message="late")


def test_status_reports_model_and_vision_gate(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.get("/api/status")

    assert response.status_code == 200
    data = response.json()
    assert data["model"]["id"] == "chat-model"
    assert data["model"]["context_size"] == 262144
    assert data["vision"]["ready"] is False
    assert data["vision"]["reason"] == "mmproj_missing"


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


def test_status_times_out_slow_vision_probe(tmp_path):
    settings = _settings(tmp_path)
    settings.status_timeout_seconds = 0.001
    db = Database(settings.database_path)
    db.initialize()
    app = create_app(settings=settings, database=db, lemonade=SlowVisionFakeLemonade())
    client = TestClient(app)

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["vision"]["reason"] == "vision_probe_timeout"


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


def _app(tmp_path, lemonade=None):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize()
    return create_app(settings=settings, database=db, lemonade=lemonade or FakeLemonade())


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
