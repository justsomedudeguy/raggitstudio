from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from customchat.archive import ArchiveService, extract_count_term, format_archive_context
from customchat.config import Settings, settings as default_settings
from customchat.database import Database
from customchat.lemonade_client import LemonadeClient
from customchat.reddit_import import RedditImportPayload, RedditImportService
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


class ModelLoadRequest(BaseModel):
    model_id: str


class ProviderSettingsRequest(BaseModel):
    lemonade_base_url: str


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


class RedditImportRequest(BaseModel):
    target_type: str
    target_name: str
    start_date: str = "2005-01-01"
    end_date: str = "now"
    include_posts: bool = True
    include_comments: bool = True


def create_app(
    settings: Settings = default_settings,
    database: Database | None = None,
    lemonade: LemonadeClient | None = None,
    web_search: WebSearchService | None = None,
    reddit_downloader: Any | None = None,
    run_reddit_imports_inline: bool = False,
) -> FastAPI:
    database = database or Database(settings.database_path)
    database.initialize()
    lemonade = lemonade or LemonadeClient(settings.lemonade_base_url)
    lemonade_factory = LemonadeClient
    web_search = web_search or WebSearchService()
    ingestion = IngestionService(settings, database, lemonade)
    rag = RagService(settings, database, lemonade)
    archive = ArchiveService(database)
    reddit_imports = RedditImportService(settings, database, archive, lemonade, downloader=reddit_downloader)

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
    app.state.lemonade_factory = lemonade_factory
    app.state.loaded_chat_model_id = None
    app.state.archive_clear_jobs = {}
    archive_html_dir = settings.artifacts_dir / "archive-html"
    archive_html_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/artifacts/archive-html", StaticFiles(directory=archive_html_dir), name="archive-html")

    @app.get("/api/status")
    async def status():
        active_lemonade = app.state.lemonade
        try:
            models = await active_lemonade.list_models()
            main_models = _main_llm_options(models.get("data", []))
            available_ids = [item.get("id") for item in models.get("data", [])]
            loaded_model_id = getattr(app.state, "loaded_chat_model_id", None)
            model_payload = None
            vision_payload = {
                "ready": False,
                "reason": "not_loaded",
                "message": "Load a chat model to check runtime model details.",
            }
            if loaded_model_id and loaded_model_id in available_ids:
                model = await active_lemonade.get_model(loaded_model_id)
                recipe_options = model.get("recipe_options", {})
                model_payload = {
                    "id": loaded_model_id,
                    "effective_id": loaded_model_id,
                    "available": True,
                    "context_size": model.get("max_context_window") or recipe_options.get("ctx_size"),
                    "backend": recipe_options.get("llamacpp_backend"),
                    "args": recipe_options.get("llamacpp_args"),
                }
                vision_payload = {
                    "ready": False,
                    "reason": "not_probed",
                    "message": "Vision probing is not run automatically from status.",
                }
            available_ids_set = set(available_ids)
            return {
                "lemonade": {"base_url": settings.lemonade_base_url, "reachable": True},
                "model": model_payload,
                "main_models": main_models,
                "embedding": {
                    "id": settings.embedding_model_id,
                    "available": settings.embedding_model_id in available_ids_set,
                },
                "reranker": {
                    "id": settings.reranker_model_id,
                    "available": settings.reranker_model_id in available_ids_set,
                },
                "classifier": {
                    "id": settings.classifier_model_id,
                    "available": settings.classifier_model_id in available_ids_set,
                },
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

    @app.put("/api/provider-settings")
    async def update_provider_settings(request: ProviderSettingsRequest):
        base_url = _normalize_base_url(request.lemonade_base_url)
        if not base_url:
            raise HTTPException(status_code=400, detail="Enter an OpenAI-compatible base URL.")
        settings.lemonade_base_url = base_url
        active_lemonade = app.state.lemonade_factory(base_url)
        app.state.lemonade = active_lemonade
        app.state.loaded_chat_model_id = None
        ingestion.lemonade = active_lemonade
        rag.lemonade = active_lemonade
        reddit_imports.lemonade = active_lemonade
        return {"lemonade": {"base_url": base_url, "reachable": True}}

    @app.post("/api/models/load")
    async def load_model(request: ModelLoadRequest):
        active_lemonade = app.state.lemonade
        model_id = request.model_id.strip()
        if not model_id:
            raise HTTPException(status_code=400, detail="Choose a chat model to load.")
        models = await active_lemonade.list_models()
        selectable_ids = {model["id"] for model in _main_llm_options(models.get("data", []))}
        if model_id not in selectable_ids:
            raise HTTPException(status_code=404, detail="Chat model not found in fetched model options.")
        try:
            result = await active_lemonade.load_chat_model(model_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        app.state.loaded_chat_model_id = model_id
        return {"model_id": model_id, "loaded": True, "result": result}

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

    @app.get("/api/archives/subreddits")
    async def archive_subreddits():
        return {"subreddits": archive.subreddit_summaries(), "coverage": archive.coverage()}

    @app.post("/api/archives/subreddits/{subreddit}/html-export")
    async def archive_subreddit_html_export(subreddit: str, request: Request):
        try:
            result = archive.export_subreddit_html(subreddit, settings.artifacts_dir)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Subreddit not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        html_path = Path(result.artifact_path).relative_to("archive-html").as_posix()
        return {
            "subreddit": result.subreddit,
            "open_url": str(request.url_for("archive-html", path=html_path)),
            "index_path": result.index_path,
            "post_count": result.post_count,
        }

    @app.delete("/api/archives/subreddits/{subreddit}")
    async def archive_subreddit_delete(subreddit: str, background_tasks: BackgroundTasks, response: Response, background: bool = False):
        try:
            if not background:
                return archive.purge_subreddit(subreddit, settings.artifacts_dir, settings.data_dir)
            summary = archive.database.get_archive_subreddit_summary(subreddit)
            if summary is None:
                raise KeyError(f"Subreddit not found: {subreddit}")
            job = _create_archive_clear_job(app, summary["subreddit"], int(summary.get("items") or 0))
            response.status_code = 202
            background_tasks.add_task(_run_archive_clear_job, app, job["id"], archive, settings.artifacts_dir, settings.data_dir)
            return job
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Subreddit not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/archive-clear-jobs/{job_id}")
    async def archive_clear_job(job_id: str):
        job = app.state.archive_clear_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Clear job not found")
        return job

    @app.post("/api/archives/import")
    async def archive_import(request: ArchiveImportRequest):
        return asdict(archive.import_file(Path(request.path), kind=request.kind))

    @app.post("/api/archives/purge")
    async def archive_purge():
        return archive.purge(settings.artifacts_dir)

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

    @app.post("/api/reddit-imports")
    async def reddit_import_start(request: RedditImportRequest, background_tasks: BackgroundTasks):
        try:
            job = reddit_imports.create_job(RedditImportPayload(**request.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job_id = int(job["id"])
        if run_reddit_imports_inline:
            await reddit_imports.run_job(job_id)
        else:
            background_tasks.add_task(reddit_imports.run_job, job_id)
        return database.get_reddit_import_job(job_id)

    @app.get("/api/reddit-imports/{job_id}")
    async def reddit_import_status(job_id: int):
        job = database.get_reddit_import_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Reddit import job not found")
        return job

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
        active_lemonade = app.state.lemonade
        selected_model_id = request.model_id.strip() if request.model_id and request.model_id.strip() else ""
        if not selected_model_id:
            raise HTTPException(status_code=400, detail="Choose a chat model before sending.")
        app.state.loaded_chat_model_id = selected_model_id

        async def events():
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
            archive_context_used = False
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
                    archive_context_used = True
            elif request.tools_enabled and last_user:
                archive_query = _loaded_archive_query(last_user, database)
                if archive_query:
                    archive_results = archive.search(archive_query["query"], limit=12, subreddit=archive_query["subreddit"])
                    if not archive_results:
                        archive_results = archive.search("", limit=12, subreddit=archive_query["subreddit"])
                    if archive_results:
                        archive_context = format_archive_context(archive_results)
                        yield _sse(
                            "archive_context",
                            {"results": archive_results, "packed_context": archive_context, "coverage": archive.coverage()},
                        )
                        rag_context = archive_context
                        archive_context_used = True
            messages = list(request.messages)
            system_context_parts: list[str] = []
            if request.system_prompt.strip():
                system_context_parts.append(request.system_prompt.strip())
            if rag_context:
                intro = (
                    "Use the following local Reddit archive datastore context. Cite chunks with their bracketed IDs."
                    if archive_context_used
                    else "Use the following retrieved context. Cite chunks with their bracketed IDs."
                )
                system_context_parts.append(f"{intro}\n\n{rag_context}")
            if request.tools_enabled and last_user and not archive_context_used and should_use_web_search(last_user):
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
            async for raw in active_lemonade.chat_stream(payload):
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


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        return ""
    if not normalized.startswith(("http://", "https://")):
        return ""
    return normalized


def _create_archive_clear_job(app: FastAPI, subreddit: str, item_count: int) -> dict[str, Any]:
    job = {
        "id": str(uuid.uuid4()),
        "subreddit": subreddit,
        "item_count": item_count,
        "status": "queued",
        "message": f"Clearing r/{subreddit} {item_count:,} indexed items.",
        "created_at": time.time(),
        "updated_at": time.time(),
        "result": None,
        "error": "",
    }
    app.state.archive_clear_jobs[job["id"]] = job
    return job


def _run_archive_clear_job(app: FastAPI, job_id: str, archive: ArchiveService, artifacts_dir: Path, data_dir: Path) -> None:
    job = app.state.archive_clear_jobs[job_id]
    job["status"] = "running"
    job["message"] = f"Clearing r/{job['subreddit']} {job['item_count']:,} indexed items."
    job["updated_at"] = time.time()
    try:
        result = archive.purge_subreddit(job["subreddit"], artifacts_dir, data_dir)
        job["status"] = "completed"
        job["result"] = result
        job["message"] = f"Cleared r/{result['subreddit']}."
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(exc)
        job["message"] = f"Failed to clear r/{job['subreddit']}."
    finally:
        job["updated_at"] = time.time()


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


def _loaded_archive_query(prompt: str, database: Database) -> dict[str, str] | None:
    lowered = prompt.lower()
    if not any(marker in lowered for marker in ("subreddit", "datastore", "loaded", "archive", "r/")):
        return None
    for summary in database.list_archive_subreddit_summaries():
        subreddit = str(summary.get("subreddit") or "")
        if not subreddit:
            continue
        if f"r/{subreddit.lower()}" in lowered or subreddit.lower() in lowered:
            terms = [
                term
                for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", prompt)
                if term.lower() not in {"subreddit", "currently", "loaded", "datastore", subreddit.lower()}
            ]
            return {"subreddit": subreddit, "query": " ".join(terms[:12]) or subreddit}
    return None


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
