# Improvement TODO

## Priority: High

- [x] **Item 15**: Add root-level `package.json` with `npm run dev` to start both frontend and backend together (use `concurrently`).
- [x] **Single-shot Reddit RAG import**: Active path runs model check, download, import, deterministic metadata, semantic chunking, and embedding in one pass.
- [x] **Classifier deferral**: Active imports skip `lemonade.classify` and mark classifier metadata as skipped for future enrichment.
- [x] **Import progress visibility**: Persist and display confirmed backend counters for download, import, metadata, chunks, embeddings, and stale updates.
- [x] **Duplicate import protection**: Same target with an active import returns a conflict instead of launching another worker.
- [x] **Stale job reconciliation**: Unfinished jobs are marked interrupted and finished at startup.
- [x] **Archive resumable importer preserved**: Keep the previous resume-aware/classifier-aware implementation in `backend/app/customchat/reddit_import_resumable.py` for future revival.
- [x] **Live OSINT embeddings completed**: Local `r/osint` archive now has 18,239 / 18,239 semantic chunk embeddings and reports semantic index ready.
- [x] **Local handoff/env files ignored**: Ignore `.env.example`, `env.example`, and `AGENT_HANDOFF.md`.
- [ ] **Chat UI bug**: Investigate the remaining chat UI bug before treating the app as polished.
- [ ] **Resumable import revival**: Reconnect the archived resume-aware/classifier-aware implementation after the single-shot RAG path is stable.
- [ ] **Classifier enrichment revival**: Add a separate post-RAG enrichment path that targets rows with `classifier.status = "skipped"`.
- [ ] **Item 1**: Move `@vitejs/plugin-react`, `vite`, and `typescript` to `devDependencies` in `frontend/package.json`.
- [ ] **Item 11**: Add a React Error Boundary in `main.tsx` to prevent full app crashes.
- [ ] **Item 12**: Make port numbers configurable via environment variables in README.
- [ ] **Item 14**: Add tests for `App.tsx` components and `streamChat` SSE logic.
- [ ] **Item 17**: Add structured logging with file rotation to the backend.

## Priority: Medium

- [x] **Item 3**: Split `App.tsx` into per-view files with active import status display.
- [x] **Archive coverage**: Track active/interrupted import state and classifier skipped counts in coverage APIs.
- [x] **Classifier model migration**: Support new model ID format in classifier checks.
- [x] **Frontend import progress helper**: Add a confirmed-counter progress helper with stage rows, ETA, last-update age, and stale-job warnings.
- [x] **Resume UI hidden**: Remove resume actions and copy from the active archive UI while single-shot imports are the supported path.
- [ ] **Item 2**: Add a backend lockfile (`requirements-lock.txt` via pip-tools or uv) for reproducible builds.
- [ ] **Item 4**: Extract inline helper components (`MessageBubble`, `PanelTitle`, `ViewHeader`, `EmptyState`, `InlineError`, `StatusPill`) from `App.tsx`.
- [ ] **Item 8**: Add `.github/workflows/ci.yml` for automated testing on push/PR.
- [ ] **Item 13**: Verify `tsconfig.app.json` has `strict: true`, `noUnusedLocals`, and `noUnusedParameters`.
- [ ] **Item 16**: Add TTL or periodic cleanup for `app.state.archive_clear_jobs` to prevent memory leaks.
- [ ] **Item 19**: Verify `frontend/index.html` exists and is tracked in git.
- [ ] **Item 20**: Move Pydantic request models to a `schemas.py` module with `__all__`.

## Priority: Low

- [ ] **Item 5**: Populate `docs/` directory or remove it.
- [x] **Item 6**: Document model ID configurability; local env templates are ignored.
- [ ] **Item 7**: Consider CSS modules or Tailwind for `App.css` (1240 lines) if it grows further.
- [ ] **Item 9**: Add a proper `__main__.py` entry point for the backend.
- [ ] **Item 10**: Add `server.proxy` for `/api` forwarding in `vite.config.ts` for dev convenience.
- [ ] **Item 18**: Document that CORS is permissive and not production-hardened in README.
