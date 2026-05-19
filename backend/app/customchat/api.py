from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from customchat.archive import ArchiveService, extract_count_term, format_archive_context
from customchat.config import Settings, settings as default_settings
from customchat.database import Database
from customchat.lemonade_client import LemonadeClient
from customchat.screenshots import capture_monitor
from customchat.services import IngestionService, RagService
from customchat.web_search import WebSearchService, format_web_context, should_use_web_search


class IngestRequest(BaseModel):
    kind: str
    title: str | None = None
    text: str | None = None
    path: str | None = None
    url: str | None = None
    max_pages: int | None = None
    uri: str | None = None


class RagSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=40)
    filters: dict[str, Any] = Field(default_factory=dict)


class ArchiveImportRequest(BaseModel):
    path: str
    kind: str | None = None


class ArchiveCountRequest(BaseModel):
    term: str
    case_sensitive: bool = False
    kind: str | None = None
    subreddit: str | None = None


class ArchiveSearchRequest(BaseModel):
    query: str = ""
    limit: int = Field(default=20, ge=1, le=100)
    kind: str | None = None
    subreddit: str | None = None
    after: int | None = None
    before: int | None = None


class ChatStreamRequest(BaseModel):
    messages: list[dict[str, Any]]
    mode: str = "thinking"
    model_id: str | None = None
    collection_ids: list[int] = Field(default_factory=list)
    query: str | None = None
    tools_enabled: bool = True
    conversation_id: int | None = None
    system_prompt: str = ""


class ScreenshotRequest(BaseModel):
    monitor_index: int = 1


class ConversationRequest(BaseModel):
    title: str = "New chat"
    system_prompt: str = ""


class ConversationUpdateRequest(BaseModel):
    title: str | None = None
    system_prompt: str | None = None


class MessageCreateRequest(BaseModel):
    role: str
    content: str
    reasoning: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def create_app(
    settings: Settings = default_settings,
    database: Database | None = None,
    lemonade: LemonadeClient | None = None,
    web_search: WebSearchService | None = None,
) -> FastAPI:
    database = database or Database(settings.database_path)
    database.initialize()
    lemonade = lemonade or LemonadeClient(settings.lemonade_base_url)
    web_search = web_search or WebSearchService()
    ingestion = IngestionService(settings, database, lemonade)
    rag = RagService(settings, database, lemonade)
    archive = ArchiveService(database)

    app = FastAPI(title="CustomChat Local Qwen RAG Workbench")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.database = database
    app.state.lemonade = lemonade

    @app.get("/api/status")
    async def status():
        try:
            models = await lemonade.list_models()
            main_models = _main_llm_options(models.get("data", []))
            available_ids = [item.get("id") for item in models.get("data", [])]
            effective_model_id = (
                settings.chat_model_id
                if settings.chat_model_id in available_ids
                else main_models[0]["id"]
                if main_models
                else settings.chat_model_id
            )
            model = await lemonade.get_model(effective_model_id)
            try:
                vision = await asyncio.wait_for(
                    lemonade.probe_vision(effective_model_id),
                    timeout=settings.status_timeout_seconds,
                )
                vision_payload = {"ready": vision.ready, "reason": vision.reason, "message": vision.message}
            except TimeoutError:
                vision_payload = {
                    "ready": False,
                    "reason": "vision_probe_timeout",
                    "message": "Vision probe exceeded the status timeout.",
                }
            recipe_options = model.get("recipe_options", {})
            return {
                "lemonade": {"base_url": settings.lemonade_base_url, "reachable": True},
                "model": {
                    "id": settings.chat_model_id,
                    "effective_id": effective_model_id,
                    "available": settings.chat_model_id in available_ids or model.get("id") == settings.chat_model_id,
                    "context_size": recipe_options.get("ctx_size"),
                    "backend": recipe_options.get("llamacpp_backend"),
                    "args": recipe_options.get("llamacpp_args"),
                },
                "main_models": main_models,
                "embedding": {"id": settings.embedding_model_id},
                "reranker": {"id": settings.reranker_model_id},
                "classifier": {"id": settings.classifier_model_id},
                "vision": vision_payload,
                "database": {
                    "sources": database.count_rows("sources"),
                    "documents": database.count_rows("documents"),
                    "chunks": database.count_rows("chunks"),
                    "embeddings": database.count_rows("embeddings"),
                },
                "archive": archive.coverage(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "lemonade": {"base_url": settings.lemonade_base_url, "reachable": False, "error": str(exc)},
                "vision": {"ready": False, "reason": "status_error", "message": str(exc)},
            }

    @app.get("/api/conversations")
    async def conversations():
        return {"conversations": database.list_conversations()}

    @app.post("/api/conversations")
    async def create_conversation(request: ConversationRequest):
        conversation_id = database.create_conversation(request.title, request.system_prompt)
        return database.get_conversation(conversation_id)

    @app.get("/api/conversations/{conversation_id}")
    async def conversation_detail(conversation_id: int):
        conversation = database.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"conversation": conversation, "messages": database.list_messages(conversation_id)}

    @app.patch("/api/conversations/{conversation_id}")
    async def update_conversation(conversation_id: int, request: ConversationUpdateRequest):
        if database.get_conversation(conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        database.update_conversation(conversation_id, request.title, request.system_prompt)
        return database.get_conversation(conversation_id)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def create_message(conversation_id: int, request: MessageCreateRequest):
        if database.get_conversation(conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        message_id = database.insert_message(
            conversation_id=conversation_id,
            role=request.role,
            content=request.content,
            reasoning=request.reasoning,
            metadata=request.metadata,
        )
        return {"id": message_id}

    @app.post("/api/ingest")
    async def ingest(request: IngestRequest):
        result = await ingestion.ingest(request.model_dump(exclude_none=True))
        return asdict(result)

    @app.get("/api/ingest/{job_id}")
    async def ingestion_job(job_id: int):
        job = database.get_ingestion_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Ingestion job not found")
        return job

    @app.post("/api/rag/search")
    async def rag_search(request: RagSearchRequest):
        return await rag.search(request.query, top_k=request.top_k, filters=request.filters)

    @app.get("/api/archives")
    async def archives():
        return {"files": archive.list_files(), "coverage": archive.coverage()}

    @app.post("/api/archives/import")
    async def archive_import(request: ArchiveImportRequest):
        return asdict(archive.import_file(Path(request.path), kind=request.kind))

    @app.post("/api/archives/count")
    async def archive_count(request: ArchiveCountRequest):
        return asdict(
            archive.count_occurrences(
                request.term,
                case_sensitive=request.case_sensitive,
                kind=request.kind,
                subreddit=request.subreddit,
            )
        )

    @app.post("/api/archives/search")
    async def archive_search(request: ArchiveSearchRequest):
        return {
            "results": archive.search(
                request.query,
                limit=request.limit,
                kind=request.kind,
                subreddit=request.subreddit,
                after=request.after,
                before=request.before,
            ),
            "coverage": archive.coverage(),
        }

    @app.get("/api/sources")
    async def sources():
        return {"sources": database.list_sources()}

    @app.get("/api/documents")
    async def documents():
        return {"documents": database.list_documents()}

    @app.get("/api/chunks/{chunk_id}")
    async def chunk(chunk_id: int):
        try:
            return database.get_chunk(chunk_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Chunk not found") from None

    @app.post("/api/screenshots/capture")
    async def screenshot(request: ScreenshotRequest):
        captured = capture_monitor(settings.attachments_dir, request.monitor_index)
        attachment_id = database.insert_attachment("screenshot", captured["path"], captured)
        return {"attachment_id": attachment_id, **captured}

    @app.post("/api/chat/stream")
    async def chat_stream(request: ChatStreamRequest):
        async def events():
            selected_model_id = request.model_id.strip() if request.model_id and request.model_id.strip() else settings.chat_model_id
            conversation_id = request.conversation_id
            if conversation_id is not None and database.get_conversation(conversation_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if conversation_id is None:
                title = _conversation_title(request.messages)
                conversation_id = database.create_conversation(title=title, system_prompt=request.system_prompt)
            else:
                database.update_conversation(conversation_id, system_prompt=request.system_prompt)
            yield _sse("conversation", {"id": conversation_id})

            last_user = _last_user_message(request.messages)
            if last_user:
                database.insert_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=last_user,
                    metadata={"mode": request.mode, "rag_query": request.query or "", "model_id": selected_model_id},
                )

            if request.tools_enabled and last_user:
                count_term = extract_count_term(last_user)
                if count_term:
                    exact_count = archive.count_occurrences(count_term, case_sensitive=True)
                    folded_count = archive.count_occurrences(count_term, case_sensitive=False)
                    payload = {
                        "tool": "archive.count",
                        "arguments": {"term": count_term, "case_sensitive": True},
                        "result": asdict(exact_count),
                        "case_insensitive_result": asdict(folded_count),
                    }
                    yield _sse("tool_call", payload)
                    answer = _archive_count_answer(count_term, exact_count, folded_count)
                    database.insert_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=answer,
                        metadata={"mode": request.mode, "model_id": selected_model_id, "tool": "archive.count"},
                    )
                    yield _sse("content", {"text": answer})
                    yield _sse("done", {})
                    return

            rag_context = ""
            if request.query:
                retrieval = await rag.search(request.query, top_k=8)
                rag_context = retrieval["packed_context"]
                yield _sse("rag_context", retrieval)
                archive_results = archive.search(request.query, limit=12)
                if archive_results:
                    archive_context = format_archive_context(archive_results)
                    yield _sse(
                        "archive_context",
                        {"results": archive_results, "packed_context": archive_context, "coverage": archive.coverage()},
                    )
                    rag_context = "\n\n".join(part for part in [rag_context, archive_context] if part)
            messages = list(request.messages)
            system_context_parts: list[str] = []
            if request.system_prompt.strip():
                system_context_parts.append(request.system_prompt.strip())
            if rag_context:
                system_context_parts.append(
                    "Use the following retrieved context. Cite chunks with their bracketed IDs.\n\n"
                    f"{rag_context}"
                )
            if request.tools_enabled and last_user and should_use_web_search(last_user):
                try:
                    web_results = await web_search.search(last_user, max_results=5)
                    yield _sse(
                        "tool_call",
                        {"tool": "web.search", "arguments": {"query": last_user}, "results": web_results},
                    )
                    system_context_parts.append(
                        "Use these web search results when they are relevant. Cite them as [WEB1], [WEB2], etc.\n\n"
                        f"{format_web_context(web_results)}"
                    )
                except Exception as exc:  # noqa: BLE001
                    yield _sse(
                        "tool_call",
                        {"tool": "web.search", "arguments": {"query": last_user}, "error": str(exc)},
                    )
            if system_context_parts:
                messages.insert(0, {"role": "system", "content": "\n\n".join(system_context_parts)})
            payload = {
                "model": selected_model_id,
                "messages": messages,
                "temperature": 0.6,
                "top_p": 0.95,
                "max_tokens": 4096,
            }
            if request.mode == "direct":
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            assistant_content: list[str] = []
            assistant_reasoning: list[str] = []
            async for raw in lemonade.chat_stream(payload):
                if raw.get("type") == "done":
                    database.insert_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content="".join(assistant_content),
                        reasoning="".join(assistant_reasoning),
                        metadata={"mode": request.mode, "model_id": selected_model_id},
                    )
                    yield _sse("done", {})
                    continue
                choice = (raw.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if "reasoning_content" in delta:
                    assistant_reasoning.append(delta["reasoning_content"])
                    yield _sse("reasoning", {"text": delta["reasoning_content"]})
                if "content" in delta and delta["content"]:
                    assistant_content.append(delta["content"])
                    yield _sse("content", {"text": delta["content"]})
                if "tool_calls" in delta:
                    yield _sse("tool_call", {"tool_calls": delta["tool_calls"]})
                if choice.get("finish_reason"):
                    yield _sse("timing", {"finish_reason": choice["finish_reason"], "timings": raw.get("timings")})

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _conversation_title(messages: list[dict[str, Any]]) -> str:
    text = _last_user_message(messages).strip()
    if not text:
        return "New chat"
    return text[:60]


def _archive_count_answer(term: str, exact_count: Any, folded_count: Any) -> str:
    parts = [
        f'Case-sensitive "{term}" appears {exact_count.occurrences} occurrence'
        f'{"s" if exact_count.occurrences != 1 else ""} across {exact_count.matched_items} Reddit item'
        f'{"s" if exact_count.matched_items != 1 else ""}.'
    ]
    if folded_count.occurrences != exact_count.occurrences:
        parts.append(
            f'Case-insensitive "{term}" appears {folded_count.occurrences} occurrence'
            f'{"s" if folded_count.occurrences != 1 else ""} across {folded_count.matched_items} Reddit item'
            f'{"s" if folded_count.matched_items != 1 else ""}.'
        )
    parts.append(f"Searched {exact_count.searched_items} indexed Reddit archive item{'s' if exact_count.searched_items != 1 else ''}.")
    if exact_count.by_kind:
        by_kind = ", ".join(f"{kind}: {count}" for kind, count in exact_count.by_kind.items())
        parts.append(f"Case-sensitive occurrences by kind: {by_kind}.")
    return " ".join(parts)


def _main_llm_options(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    auxiliary_labels = {"embeddings", "reranking", "transcription", "tts"}
    auxiliary_recipes = {"kokoro", "whispercpp"}
    options = []
    for model in models:
        model_id = model.get("id")
        if not model_id:
            continue
        labels = [str(label) for label in model.get("labels", [])]
        normalized_labels = {label.lower() for label in labels}
        recipe = str(model.get("recipe") or "").lower()
        if normalized_labels & auxiliary_labels or recipe in auxiliary_recipes or _looks_like_auxiliary_model(model):
            continue
        recipe_options = model.get("recipe_options") or {}
        options.append(
            {
                "id": model_id,
                "labels": labels,
                "context_size": model.get("max_context_window") or recipe_options.get("ctx_size"),
                "backend": recipe_options.get("llamacpp_backend"),
                "args": recipe_options.get("llamacpp_args"),
            }
        )
    return options


def _looks_like_auxiliary_model(model: dict[str, Any]) -> bool:
    checkpoints = model.get("checkpoints") if isinstance(model.get("checkpoints"), dict) else {}
    searchable = " ".join(
        str(value)
        for value in [
            model.get("id"),
            model.get("checkpoint"),
            *(checkpoints or {}).values(),
        ]
        if value
    ).lower()
    auxiliary_terms = (
        "embed",
        "rerank",
        "ranker",
        "zerank",
        "ms-marco",
        "query-expansion",
        "qmd-query",
    )
    return any(term in searchable for term in auxiliary_terms)


app = create_app()
