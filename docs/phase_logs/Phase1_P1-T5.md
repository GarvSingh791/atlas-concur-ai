# Phase Log

**Phase:**       Phase 1 — Project Bootstrap
**Sub-Phase:**   P1-T5 — Tooling & CI Baseline
**Task ID:**     P1-T5
**Date:**        2026-06-07
**Author:**      Garv Singh Saini
**Status:**      ✔ Complete, Verified, Security Reviewed
**Phase 1:**     ✔ COMPLETE (this was the final task)

---

## Objective
Add linting, formatting, type-checking, and a test runner for both the Python backend
and the TypeScript frontend, with one trivial smoke test per layer, plus a GitHub Actions
CI workflow that installs, lints, type-checks, tests, and builds the Docker images.
Goal: a green, hermetic CI pipeline on the empty skeleton — closing Phase 1.

## Work Completed
- Added Ruff (lint + format + import-sort) and mypy for the five Python packages,
  configured in a repo-root pyproject.toml
- Added pytest with hermetic configuration (testpaths, repo-root on pythonpath, dummy
  env via root conftest.py)
- Added one backend smoke test (app instance + /health → 200 without a live database)
  and a collectable ai_agents/tests placeholder
- Added ESLint (extending eslint-config-next) + Prettier, and Vitest (jsdom + React +
  @-alias) for the frontend, with one component smoke test
- Added a 3-job GitHub Actions workflow: backend, frontend, docker-build
- Added a Makefile mirroring CI (lint/format/test/build/ci-local)
- Applied a one-time, behavior-preserving Ruff + Prettier normalization so the existing
  skeleton passes clean

## Files Created
  pyproject.toml
  requirements-dev.txt
  conftest.py
  backend/tests/test_smoke.py
  ai_agents/tests/test_placeholder.py
  frontend/.prettierrc.json
  frontend/.prettierignore
  frontend/vitest.config.ts
  frontend/__tests__/smoke.test.tsx
  .github/workflows/ci.yml
  Makefile
  docs/phase_logs/Phase1_P1-T5.md

## Files Modified
Intentional config changes:
  frontend/package.json — added scripts (format, format:check, test, test:watch) and
    devDeps (prettier, eslint-config-prettier, vitest, @vitejs/plugin-react,
    @testing-library/react, @testing-library/dom, jsdom)
  frontend/.eslintrc.json — appended "prettier" to extends
  frontend/package-lock.json — regenerated; npm ci confirmed in-sync

Formatting-only changes (sanctioned; no logic touched):
  backend/app/core/errors.py — wrapped a 102-char function signature
  backend/app/core/logging.py — removed unused traceback import, de-quoted annotation
  backend/app/main_entry/app.py — import sort, AsyncIterator moved to collections.abc,
    B904 fix (except ... as err / raise ... from err — behavior-preserving, makes the
    implicit exception chain explicit)
  frontend — 16 files normalized to consistent semicolons (Prettier):
    app/(app)/layout.tsx, components.json, components/ui/avatar.tsx,
    components/ui/badge.tsx, components/ui/button.tsx, components/ui/card.tsx,
    components/ui/dialog.tsx, components/ui/dropdown-menu.tsx, components/ui/input.tsx,
    components/ui/label.tsx, components/ui/separator.tsx, components/ui/skeleton.tsx,
    components/ui/toast.tsx, components/ui/toaster.tsx, components/ui/tooltip.tsx,
    lib/hooks/use-toast.ts, lib/utils.ts, next.config.mjs, styles/globals.css

## Integration Points
- backend/tests/ and ai_agents/tests/ runners and the Vitest setup are EXTENDED (not
  replaced) in Phase 7 with real unit, API, AI-validation, and E2E suites;
  ai_agents/tests/ is the labeled AI-eval harness home (Development Rule 11)
- .github/workflows/ci.yml is EXTENDED in Phase 8 with a deploy job and real GitHub
  Actions secrets (none referenced today)
- The docker-build CI job reuses the exact build contexts from P1-T4 docker-compose.yml
  (backend = repo root for the sibling config import; frontend = frontend/)
- The Makefile gives a local command surface identical to CI so "what CI runs" and
  "what I run locally" stay in sync through all future phases

## Technical Decisions
- Ruff over flake8/black/isort — one tool for lint + format + import-sort; fewer deps,
  faster, single config block; kept the full rule set (E,F,I,UP,B)
- Vitest over Jest — native ESM/TS/Vite, no Babel/ts-jest transform layer; pinned
  vitest@^2.1.9 (Vite 5) to avoid the vitest-4/Vite-6 peer break against React 18
- Hermetic /health test — the DB connect lives only in the FastAPI lifespan; the smoke
  test uses TestClient(app) WITHOUT the with-context manager so lifespan never runs;
  proven with an unroutable DATABASE_URL and no Postgres (3 tests in 0.20s, no timeout)
- Import resolution — config.settings instantiates at import time and sys.exit(1)s
  without JWT_SECRET/DATABASE_URL, so the root conftest.py sets dummy values before
  collection and pytest pythonpath=["."] puts the repo root on sys.path; CI mirrors this
  with job-level env stubs
- mypy — added the official pydantic.mypy plugin to fix Settings() env-loading false
  positives (not config-loosening); scoped a per-module arg-type override only for the
  Starlette add_exception_handler stub limitation (runtime-correct), the narrowest
  config-only fix
- docker-build only builds (not runs) — image validity is the Phase 1 gate; running the
  stack is Phase 6/7; gated on needs: [backend, frontend]

## Testing Performed
Verified by LOCAL SIMULATION of every CI command (not pushed to GitHub). All green:

  Backend: ruff check . ✓ · ruff format --check . ✓ (19 files) ·
    mypy backend config ✓ (17 files) · pytest ✓ (3 passed)
  Frontend: npm ci ✓ · lint ✓ · format:check ✓ · type-check ✓ · test ✓ · build ✓
  Docker: atlas-backend:ci ✓ and atlas-frontend:ci ✓ (both built clean)
  Workflow YAML parsed with PyYAML; triggers/jobs/steps confirmed

Adversarial QA:
  - Injected unused import + type error (Python) → Ruff F401 + mypy caught; removed
    and reconfirmed clean
  - Injected jsx-no-undef + type error (TSX) → ESLint + tsc caught; removed and
    reconfirmed clean
  - Hermeticity proven with an unroutable DB (no Postgres running): 3 tests in 0.20s
  - Secret scan: only the labelled dummy CI secret and a localhost test DSN — no real
    secrets

## Risks Identified
- CI verified by local simulation only — the first real GitHub Actions run happens on
  first push; fresh runners can occasionally surface environment/cache differences
- System Node is v12 locally; local `make ci-local` only fully matches CI when run
  under the nvm Node 20 shell (CI pins Node 20 to match Dockerfile.frontend)

## Resolution Actions
- Existing skeleton not lint/format-clean → one-time behavior-preserving Ruff (--fix +
  format) and Prettier (--write) normalization; no logic changed
- mypy false positives → official pydantic.mypy plugin + narrowly scoped per-module
  override; no source loosening
- A QA scratch file created at the wrong path (repo-root components/) was caught
  immediately, removed, and recreated under frontend/components/ — no residue
- First-push CI risk: documented for awareness; will watch the first GitHub Actions
  run and fix any runner-specific issues if they arise

---

## Phase 1 Checkpoint — COMPLETE
Phase 1 Definition of Done is fully met:
  ✔ All services boot via docker compose (P1-T4, verified previously)
  ✔ GET /health returns 200
  ✔ CI pipeline green on the empty skeleton (every step simulated locally;
    both Docker images build)
Phase 1 is COMPLETE. Phase 2 may begin.

## Next Sub-Phase Preview
P2-T1 — DB Session & Base Conventions (start of Phase 2 — Core Backend)
Objective: Implement the database session/engine wiring and the base model
conventions from DATABASE_SCHEMA.md (UUID PKs, timestamps, organization_id
tenant seam, created_by/updated_by, soft-delete, version), verified by
creating and reading a trivial model against the running database in a test.

---
