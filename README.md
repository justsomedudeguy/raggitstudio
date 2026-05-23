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

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend
npm install
```

Copy `.env.example` to `.env` and point it at your endpoint:

```powershell
Copy-Item .env.example .env
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
