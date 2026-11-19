# Phase 8 / Completion C5–C6 — README and reproducibility drill
**Date:** 2026-11-08
**Author:** Garv Singh Saini
**Status:** COMPLETE — README rewritten (C5); fresh-clone and empty-Neon-branch
  rebuilds both pass following only the README (C6)

## C5 — README

`README.md` was rewritten as the single source for understanding, running and
deploying Atlas.

**Contents.** The README covers:
- the pitch, stack badges, live links and demo accounts;
- the architecture (four planes, deterministic core + routed agents, guardrails,
  multi-document RAG), with the key decisions as blockquotes;
- a role-by-feature matrix;
- the Docker and hybrid setup paths, with environment variables in collapsible
  tables;
- the five test tiers and troubleshooting;
- deployment and CI/CD, sourced quality numbers, repository structure, and status.

**Review.** Four reviews before C6: recruiter, engineering manager,
reproducibility, and accuracy/confidentiality. The private design documents are
paraphrased, never linked or quoted.

**QA findings recorded in the README rather than changed in code:**
- `.env.example` turns seeding off by default.
- `LLM_MODEL` in `.env.example` doesn't match OpenRouter's model naming.
- CORS fails when the app is opened on `127.0.0.1`.
- `OCR_CONFIDENCE_THRESHOLD` is declared but unused; the receipt pipeline routes at
  0.70.
- The Docker frontend passes `NEXT_PUBLIC_API_URL`, while the code reads
  `NEXT_PUBLIC_API_BASE_URL`.

## C6 — Reproducibility drill

### Local fresh clone (README only)

A fresh clone was run in a separate Compose project with new volumes; the
developer stack was stopped, its volumes untouched.

| Step | Result |
|---|---|
| Path A (Docker): clone → running app, three role logins | about 4 min (205 s of it the first image build) |
| Path B (hybrid): venv, migrate, seed, uvicorn, Next.js dev | pass |
| Tier 1 hermetic pytest + ruff + mypy + single Alembic head | 913 passed, 0 failed |
| Tier 2 full DB suite on an isolated DB | 1657 passed, 13 skipped; the same 27 known legacy failures as the baseline |
| Tier 3 eval-db | 48 passed |
| Tier 4 frontend lint / format / type-check / vitest / build | clean; 236 tests |
| Tier 5 Playwright (3 roles) | 19 / 19 |
| Clone → all tiers green | about 15.5 min |

**Gaps found.** No README command was wrong. The gaps were:
- two stale test counts;
- no route for machines without sudo or Python 3.12;
- no API smoke-test step;
- first-build timing.

All are fixed in the README, and a second fresh run passed every step as written.
The no-sudo container route was exercised; the uv/pyenv route was not, because
neither tool is installed on the drill machine.

### Cloud: empty Neon branch

Steps:
1. Created a Neon branch `c6-repro-drill` from `production` (auto-delete after
   1 day).
2. Confirmed its compute endpoint was the branch's own, not production's.
3. Emptied its schema (`DROP SCHEMA public CASCADE`; 23 tables → 0) through the
   branch's own connection.
4. Ran the current backend image against it with `SEED_ON_STARTUP=true`.

Results:
- **Migrations:** `alembic upgrade head` ran 0001 → `0012_hnsw_ef_construction_200`
  on the empty database, re-creating the `pgcrypto` and `vector` extensions.
- **Seed:** completed, with 208 policy embeddings.
- **Boot:** `/health` 200 after 84 s, including the migrations and the seed.
- **Logins:** all three roles log in and see the expected data (4 / 4 / 6 reports).

The branch was then deleted. The project is back to its single `production`
branch, and the local copy of the connection string was shredded.

## Local environment repairs made along the way

- **Backend image.** The local backend image predated migration 0012, so the dev
  backend was stuck in a restart loop before C6. It was rebuilt from current code.
- **MinIO volume.** The Compose file now uses the Chainguard MinIO image, which runs
  as uid 65532. The existing `atlas_atlas-minio-data` volume had been created by the
  old root-run image, so its owner was changed to match. The data is unchanged.
- **Test databases.** DB-backed test runs use throwaway `atlas_test*` databases; the
  backend test fixture drops the schema of whatever database it points at.

## Follow-ups

- **Neon credentials.** The `c6-repro-drill` connection string was pasted into a
  chat session. Neon branches inherit their parent's role passwords, so
  `neondb_owner`'s password should be reset and Render's `DATABASE_URL` updated
  (see `docs/staging_env_map.md`, rotation table).
- **Python version comment.** `pyproject.toml` still says the dev runtime is 3.10;
  the project targets 3.12.
