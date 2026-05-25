# RaggitStudio

RaggitStudio is a local-first hybrid RAG workbench for a local or API OAI compatible endpoint.
It combines deterministic Reddit archive access with semantic retrieval so you can verify
that the app can reach the whole raw corpus before relying on model-generated answers.

The current app is built for a single-user desktop workflow. Source artifacts, SQLite
state, embeddings, screenshots, chat traces, archive indexes, and retrieved Reddit data
stay local and are intentionally ignored by git.

## What It Does

- Chats with selectable main LLMs exposed by the configured OAI-compatible endpoint.
- Saves chat history, assistant reasoning text, and per-conversation system prompts in
  SQLite.
- Runs contextual web-search tooling when enabled and the latest prompt asks for current
  or online information.
- Ingests normal local documents, URLs, crawls, and scraper JSONL into the document RAG
  path with chunking, embeddings, reranking, and chunk citations.
- Imports Arctic Shift style Reddit archives through a separate archive path that streams
  `.jsonl`, `.ndjson`, `.json`, `.zst`, and `.zst_blocks` files.
- Stores Reddit posts and comments in normalized SQLite tables with FTS, raw JSON,
  metadata, exact count/search endpoints, and archive coverage metrics.
- Routes exact count prompts, such as `How many times is "XXX" mentioned?`, to the raw
  archive index instead of asking the LLM to guess.
- Adds archive-backed context to semantic chat queries so answers can cite raw Reddit rows.
- Reddit import pipeline: checking embedding model -> downloading -> importing ->
  metadata -> semantic chunking -> embedding -> completed. Classifier enrichment is
  skipped for now and rows are marked for future enrichment.
- Single-shot import behavior: start Reddit imports only when the machine can stay
  available until completion. Half-imported data should be cleared before retrying.
- Duplicate import protection: starting an import for the same target while one is
  active returns a conflict instead of creating a duplicate or promising resume.
- Stale job reconciliation: unfinished queued/running jobs from crashes are marked
  interrupted and finished at app startup.
- Manual interrupt endpoint: `PATCH /api/reddit-imports/{job_id}` marks a running job
  interrupted, but the current active path does not resume it.

The Reddit archive importer is based on Arthur Heitmann's Arctic Shift tooling:
https://github.com/ArthurHeitmann/arctic_shift.

## Runtime Pieces

- Backend: FastAPI, SQLite, NumPy retrieval, and an OpenAI-compatible API client.
- Frontend: React, TypeScript, Vite, and Lucide icons.
- Archive engine: streaming readers and SQLite FTS for Reddit archive files.
- Default local model IDs are configured in code and may be overridden with environment
  variables.
- Vision submission is gated by a runtime probe. Configure a compatible vision model and
  projection files at the endpoint before using screenshot/image submission.

## Current Development State

As of May 25, 2026, the active Reddit import strategy has been changed back to
a single-shot RAG indexing flow. The previous resume-aware/classifier-heavy
implementation was copied to `backend/app/customchat/reddit_import_resumable.py`
for later revival, but the app does not call it now.

The local `r/osint` archive import has completed through embeddings:

- 18,165 Reddit rows imported.
- 18,239 semantic chunks generated.
- 18,239 archive embeddings written with `zembed-1-Q4_K_M-GGUF-Q4_K_M`.
- Embedding vectors are 2,560 dimensions.
- Archive coverage reports `semantic_index_state: ready`.

Known remaining issue: there is still a chat UI bug to investigate. Stop here
expecting the Reddit archive index to be ready, but the chat surface still needs
a focused debugging pass before treating the app as polished.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend
npm install
```

Create `.env` at the repository root and point it at your endpoint:

```powershell
@'
CUSTOMCHAT_LEMONADE_BASE_URL=http://your-openai-compatible-host/v1
CUSTOMCHAT_CHAT_MODEL_ID=your-chat-model
CUSTOMCHAT_EMBEDDING_MODEL_ID=your-embedding-model
CUSTOMCHAT_RERANKER_MODEL_ID=your-reranker-model
CUSTOMCHAT_CLASSIFIER_MODEL_ID=your-classifier-model
'@ | Set-Content .env
```

The settings keep the historical `CUSTOMCHAT_LEMONADE_*` names for compatibility with
the current code, but the base URL may be any OpenAI-compatible `/v1` endpoint.

## Run

Start both the backend and frontend from the project root:

```powershell
npm install
npm run dev
```

This launches the FastAPI backend on port 8000 and the Vite dev server on port 5173 in parallel.

Open `http://127.0.0.1:5173`.

### Individual services

Run only the backend:

```powershell
npm run dev:backend
```

Run only the frontend:

```powershell
npm run dev:frontend
```

## Archive Workflow

1. Open the Archives view.
2. Import local Arctic Shift Reddit archives, such as paired posts/comments JSONL files.
3. Confirm coverage counts for files, posts, comments, and total indexed rows.
4. Use exact count/search to verify raw corpus access.
5. Use chat or Retrieval Lab for semantic queries over the indexed corpus.

Imported archives and generated SQLite stores are local runtime data. They should not be
committed.

## Reddit Import Pipeline

The import runs in stages:

1. **Checking embedding model** - verifies that the configured embedding model is
   available before writing import data.
2. **Downloading** - fetches posts/comments from Arctic Shift API.
3. **Importing** - streams downloaded archive rows into SQLite.
4. **Metadata** - adds deterministic fields (upvotes, poster counts, text stats) and
   writes classifier metadata as `{"status": "skipped"}`.
5. **Chunking** - builds semantic chunks from item text.
6. **Embedding** - generates embeddings for all chunks.

The active importer is a single-shot flow. It reports confirmed backend counters for
downloaded rows, imported rows, metadata rows, semantic chunks, and embedded chunks.
Duplicate imports for the same target return a conflict while an import is active.
The older resume-aware/classifier-aware implementation is preserved in
`backend/app/customchat/reddit_import_resumable.py` but is not called by the app.

If an import is stopped halfway, clear that archive data before retrying. The
clear path removes subreddit rows, archive semantic chunks, embeddings,
generated HTML, and downloaded source archive files under `data/`.

## Test

```powershell
$env:PYTHONPATH='backend/app'
.\.venv\Scripts\python.exe -m pytest backend/tests -q
cd frontend
npm test
npm run build
```

## Repository Hygiene

The checked-in project should include source, tests, scripts, docs, and lockfiles. The
`.gitignore` excludes dependencies, virtual environments, local environment files,
retrieved datasets, SQLite stores, archive indexes, artifacts, embeddings, screenshots,
logs, caches, and build output.
