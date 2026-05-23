from fastapi.testclient import TestClient

from customchat.api import create_app
from customchat.config import Settings
from customchat.database import Database
from customchat.lemonade_client import VisionProbeResult
from customchat.metadata import SourceMetadata


class MemoryFakeLemonade:
    def __init__(self):
        self.chat_payloads = []

    async def list_models(self):
        return {"data": [{"id": "chat-model"}]}

    async def get_model(self, model_id: str):
        return {"id": model_id, "recipe_options": {"ctx_size": 262144}}

    async def probe_vision(self, model_id: str):
        return VisionProbeResult(ready=False, reason="mmproj_missing", message="provide mmproj")

    async def classify(self, model_id: str, title: str, text: str, source_type: str):
        return SourceMetadata(title=title, source_type=source_type)

    async def embed(self, model_id: str, texts: list[str]):
        return [[1.0, 0.0] for _ in texts]

    async def rerank(self, model_id: str, query: str, documents: list[str]):
        return [{"index": index, "relevance_score": 1.0 / (index + 1)} for index, _ in enumerate(documents)]

    async def chat_stream(self, payload):
        self.chat_payloads.append(payload)
        yield {"choices": [{"delta": {"reasoning_content": "reasoned "}}]}
        yield {"choices": [{"delta": {"content": "remembered answer"}}]}
        yield {"choices": [{"finish_reason": "stop"}]}
        yield {"type": "done"}


class FakeWebSearch:
    def __init__(self):
        self.queries = []

    async def search(self, query: str, max_results: int = 5):
        self.queries.append(query)
        return [
            {
                "title": "Current Alpha",
                "url": "https://example.com/current-alpha",
                "snippet": "Current alpha result.",
            }
        ]


def test_conversation_system_prompt_and_messages_are_persisted(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    created = client.post(
        "/api/conversations",
        json={"title": "Research chat", "system_prompt": "Answer like a careful analyst."},
    )
    assert created.status_code == 200
    conversation_id = created.json()["id"]

    client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"role": "user", "content": "Remember this source.", "reasoning": ""},
    )
    detail = client.get(f"/api/conversations/{conversation_id}")

    assert detail.status_code == 200
    data = detail.json()
    assert data["conversation"]["system_prompt"] == "Answer like a careful analyst."
    assert data["messages"][0]["content"] == "Remember this source."
    assert client.get("/api/conversations").json()["conversations"][0]["id"] == conversation_id


def test_chat_stream_persists_user_assistant_and_system_prompt(tmp_path):
    fake = MemoryFakeLemonade()
    app = _app(tmp_path, lemonade=fake)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "model_id": "chat-model",
            "system_prompt": "Use concise citations.",
            "messages": [{"role": "user", "content": "What did we save?"}],
            "mode": "direct",
        },
    ) as response:
        body = response.read().decode("utf-8")

    assert "event: conversation" in body
    conversations = client.get("/api/conversations").json()["conversations"]
    detail = client.get(f"/api/conversations/{conversations[0]['id']}").json()
    assert detail["conversation"]["system_prompt"] == "Use concise citations."
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "remembered answer"
    assert detail["messages"][1]["reasoning"] == "reasoned "
    assert fake.chat_payloads[0]["messages"][0] == {"role": "system", "content": "Use concise citations."}


def test_chat_stream_requires_explicit_chat_model(tmp_path):
    fake = MemoryFakeLemonade()
    app = _app(tmp_path, lemonade=fake)
    client = TestClient(app)

    response = client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "Do not use a default model."}],
            "mode": "direct",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Choose a chat model before sending."
    assert fake.chat_payloads == []


def test_chat_stream_uses_requested_chat_model(tmp_path):
    fake = MemoryFakeLemonade()
    app = _app(tmp_path, lemonade=fake)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "model_id": "alternate-chat-model",
            "messages": [{"role": "user", "content": "Use the selected model."}],
            "mode": "direct",
        },
    ) as response:
        response.read()

    assert fake.chat_payloads[0]["model"] == "alternate-chat-model"


def test_contextual_web_search_runs_when_query_asks_for_current_info(tmp_path):
    fake_web = FakeWebSearch()
    app = _app(tmp_path, web_search=fake_web)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "model_id": "chat-model",
            "messages": [{"role": "user", "content": "What is the latest news about alpha?"}],
            "mode": "direct",
            "tools_enabled": True,
        },
    ) as response:
        body = response.read().decode("utf-8")

    assert fake_web.queries == ["What is the latest news about alpha?"]
    assert "event: tool_call" in body
    assert "web.search" in body


def _app(tmp_path, lemonade=None, web_search=None):
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
    db = Database(settings.database_path)
    db.initialize()
    return create_app(settings=settings, database=db, lemonade=lemonade or MemoryFakeLemonade(), web_search=web_search)
