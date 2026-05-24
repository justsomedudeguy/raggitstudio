# Improvement TODO

## Priority: High

- [x] **Item 15**: Add root-level `package.json` with `npm run dev` to start both frontend and backend together (use `concurrently`).
- [x] **Reddit import decoupling**: Split classifier from metadata stage to speed up import pipeline.
- [x] **Import resume**: Import resumes from last completed stage on restart.
- [x] **Duplicate import protection**: Same target returns existing active job.
- [x] **Stale job reconciliation**: Unfinished jobs marked interrupted at startup.
- [x] **Manual interrupt**: `PATCH /api/reddit-imports/{job_id}` endpoint.
- [ ] **Item 1**: Move `@vitejs/plugin-react`, `vite`, and `typescript` to `devDependencies` in `frontend/package.json`.
- [ ] **Item 11**: Add a React Error Boundary in `main.tsx` to prevent full app crashes.
- [ ] **Item 12**: Make port numbers configurable via environment variables in README.
- [ ] **Item 14**: Add tests for `App.tsx` components and `streamChat` SSE logic.
- [ ] **Item 17**: Add structured logging with file rotation to the backend.

## Priority: Medium

- [x] **Item 3**: Split `App.tsx` into per-view files with resume button logic and active import status display.
- [x] **Archive coverage**: Separate `stale_running_jobs` and `resumable_import_jobs` counts in coverage API.
- [x] **Classifier model migration**: Support new model ID format in classifier checks.
- [ ] **Item 2**: Add a backend lockfile (`requirements-lock.txt` via pip-tools or uv) for reproducible builds.
- [ ] **Item 4**: Extract inline helper components (`MessageBubble`, `PanelTitle`, `ViewHeader`, `EmptyState`, `InlineError`, `StatusPill`) from `App.tsx`.
- [ ] **Item 8**: Add `.github/workflows/ci.yml` for automated testing on push/PR.
- [ ] **Item 13**: Verify `tsconfig.app.json` has `strict: true`, `noUnusedLocals`, and `noUnusedParameters`.
- [ ] **Item 16**: Add TTL or periodic cleanup for `app.state.archive_clear_jobs` to prevent memory leaks.
- [ ] **Item 19**: Verify `frontend/index.html` exists and is tracked in git.
- [ ] **Item 20**: Move Pydantic request models to a `schemas.py` module with `__all__`.

## Priority: Low

- [ ] **Item 5**: Populate `docs/` directory or remove it.
- [x] **Item 6**: Add comments to `.env.example` explaining model ID configurability.
- [ ] **Item 7**: Consider CSS modules or Tailwind for `App.css` (1240 lines) if it grows further.
- [ ] **Item 9**: Add a proper `__main__.py` entry point for the backend.
- [ ] **Item 10**: Add `server.proxy` for `/api` forwarding in `vite.config.ts` for dev convenience.
- [ ] **Item 18**: Document that CORS is permissive and not production-hardened in README.
