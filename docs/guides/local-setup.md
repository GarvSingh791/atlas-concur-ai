# Local setup

Run Atlas on your own machine, run every test tier, and check that it works. For engineers and
reviewers who want more than the [live demo](https://atlas-concur-ai.vercel.app).

**In one screen:** no API keys are needed; every provider defaults to a deterministic `local`
implementation. Path A runs everything in Docker and is the quickest way to try the app
(about 3–4 minutes for the first build). Path B runs only Postgres and MinIO in Docker and the
app on your machine, which is the setup for development and for the end-to-end tests. All
commands run from the **repository root** unless a step says `cd frontend`: `alembic.ini` lives
at the root and every import path is root-relative.

---

## Contents

- [Prerequisites](#prerequisites)
- [Path A: full Docker stack](#path-a-full-docker-stack)
- [Path B: hybrid dev setup](#path-b-hybrid-dev-setup)
- [Running the tests](#running-the-tests)
- [Troubleshooting](#troubleshooting)
- [How to verify in five minutes](#how-to-verify-in-five-minutes)

Environment variables are documented in the [configuration reference](configuration.md).

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Git | any recent | |
| Docker + Docker Compose | Compose **v2** (`docker compose`) | Needed for both paths |
| Python | **3.12** | Same version as CI and the backend image |
| Node.js + npm | **20** | Same version as CI and the frontend image |
| GNU Make | any | Needed for `make eval-db` and the keyed / calibration targets |

On Ubuntu, `python3.12 -m venv` needs `sudo apt install python3.12 python3.12-venv` (Ubuntu
22.04 needs the deadsnakes PPA). Without sudo, use `uv venv --python 3.12 .venv` or pyenv.

```bash
git clone https://github.com/GarvSingh791/atlas-concur-ai.git atlas
cd atlas
cp .env.example .env
```

---

## Path A: full Docker stack

Runs Postgres (pgvector), MinIO, the backend and the frontend in containers
(`docker/docker-compose.yml`).

```bash
# 1. Turn on demo seeding (.env.example ships with SEED_ON_STARTUP=false; without it there are no users).
sed -i 's/^SEED_ON_STARTUP=false/SEED_ON_STARTUP=true/' .env      # macOS: sed -i '' '...' .env

# 2. Replace the placeholder JWT secret with a random one.
sed -i "s/^JWT_SECRET=.*/JWT_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')/" .env

# Optional: store receipts in the bundled MinIO instead of the container's /tmp.
# sed -i 's/^STORAGE_PROVIDER=.*/STORAGE_PROVIDER=minio/' .env

# 3. Build and start everything, then follow the backend while it migrates and seeds.
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose --env-file .env -f docker/docker-compose.yml logs -f backend
```

The backend container's entrypoint runs `alembic upgrade head`, then the demo seed when
`SEED_ON_STARTUP=true`. It is ready once the log shows `Atlas seed complete.` and then
`Database connection verified`.

| Service | URL |
|---|---|
| App | http://localhost:3000 (log in with a [demo account](../../README.md#demo-accounts)) |
| Backend health | http://localhost:8000/health (at the root, not under `/api/v1`) |
| API docs | http://localhost:8000/docs and http://localhost:8000/redoc (only when `APP_DEBUG=true`, which `.env.example` sets) |
| MinIO console | http://localhost:9001 (log in with `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY`) |

With the shipped `.env` (`STORAGE_PROVIDER=local`), receipts are written inside the backend
container and lost when it is recreated. With `STORAGE_PROVIDER=minio`, compose sets
`STORAGE_PUBLIC_ENDPOINT_URL=http://localhost:9000` so receipt previews load in the browser.

```bash
docker compose --env-file .env -f docker/docker-compose.yml down        # stop
docker compose --env-file .env -f docker/docker-compose.yml down -v     # stop and wipe data volumes
docker compose --env-file .env -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d backend   # hot reload
```

The `docker-compose.dev.yml` override mounts `backend/`, `config/`, `ai_agents/`, `database/` and
`services/` into the container and runs uvicorn with `--reload`.

**Optional Redis.** The rate limiter keeps its counters in memory by default. To try the shared
store, start the `redis` profile and point the backend at it:

```bash
RATE_LIMIT_STORE=redis REDIS_URL=redis://redis:6379/0 \
  docker compose --env-file .env -f docker/docker-compose.yml --profile redis up -d
```

Background and verification: [rate-limit Redis runbook](../runbooks/rate-limit-redis.md).

---

## Path B: hybrid dev setup

The datastores run in Docker and the app runs on your machine, the same way CI's e2e job runs.

> [!NOTE]
> If you started Path A, free ports :8000 and :3000 first:
> `docker compose --env-file .env -f docker/docker-compose.yml stop backend frontend`

```bash
# 1. Start Postgres (pgvector) and MinIO only
docker compose --env-file .env -f docker/docker-compose.yml up -d postgres minio

# 2. Python environment
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 3. Migrate and seed (repo root; uses DATABASE_URL from .env)
alembic upgrade head                 # 25 revisions, 0001 → 0025_grade_entitlement_rules
python -m database.seeds.seed_all    # idempotent; prints "Atlas seed complete."

# 4. Run the API on :8000
uvicorn backend.app.main_entry.app:app --reload --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
cd frontend
cp .env.local.example .env.local     # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
npm ci
npm run dev                          # http://localhost:3000 → /login
```

Open **http://localhost:3000**. Use `localhost`, not `127.0.0.1`, or CORS blocks the requests.

**What the seed creates** (safe to run again):

- The organisation "Atlas Demo Corp" (login code `atlas-demo-corp`) and the role and permission
  catalogue.
- Five users at `atlas-demo.internal`: `employee@` (grade IC), `manager@` (SVP, so AIR-04 is
  demonstrable), `finance@` (Director), `manager2@` (VP) and `employee2@` (Senior IC). All use
  the password in `SEED_DEFAULT_PASSWORD` (`change-me-seed-password` by default).
- The deterministic-rule policy with its 24 active rules plus 1 draft rule.
- The Helios RAG corpus (219 embedded units).
- Sample trips, card transactions, calendar events, pending receipts, and expense reports in
  mixed states with approvals.

**Try it with the local providers:**

- **Planner:** set the chips to From `SFO`, To `JFK`, Depart a future date, Travelers `1`.
- **Local OCR** matches on the uploaded file's *contents*. The file must still be a real PNG,
  JPEG or WebP to pass the upload checks; with local OCR an image whose bytes do not contain
  `airfare`, `hotel` or `meal` returns a generic placeholder you can correct by hand.
- **Policy assistant:** with `LLM_PROVIDER=local` it abstains on policy questions, because no
  model is attached. The live demo answers them with Gemini and clause citations.

<details>
<summary><b>Pointing at a managed Postgres instead (for example a throwaway Neon branch)</b></summary>

Exported environment variables override `.env`. Migration `0001` creates the `pgcrypto` and
`vector` extensions itself. Tested on Neon PG18.

```bash
export DATABASE_URL='postgresql://<user>:<password>@<branch-host>/<db>?sslmode=require'
alembic upgrade head          # prefer Neon's direct (non "-pooler") host for DDL
python -m database.seeds.seed_all
uvicorn backend.app.main_entry.app:app --host 0.0.0.0 --port 8000
unset DATABASE_URL            # ALWAYS unset before running pytest (see the warning below)
```
</details>

---

## Running the tests

What each tier proves and the latest results are in [TESTING.md](../quality/TESTING.md).

> [!WARNING]
> **The DB-backed pytest fixture runs `DROP SCHEMA public CASCADE` on whatever `DATABASE_URL`
> points at.** An exported variable always wins over the `conftest.py` default. **Always set
> `DATABASE_URL` inline to a throwaway database whose name ends in `_test`**, and run one pytest
> suite at a time against it.

The commands assume the compose defaults `POSTGRES_USER=atlas_user` / `POSTGRES_PASSWORD=change-me`
and the Path B virtualenv.

**1. Hermetic backend suite (CI `backend` job).** No database; DB tests skip themselves.

```bash
ruff check . && ruff format --check . && mypy backend config evals
alembic heads                           # exactly one: 0025_grade_entitlement_rules (head)
DATABASE_URL=postgresql+psycopg2://atlas:atlas@localhost:1/none pytest -m 'not db' --ignore=evals/manual
```

**2. Full DB-backed suite on an isolated test database.**

```bash
unset DATABASE_URL
docker exec atlas-postgres createdb -U atlas_user atlas_test
DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test \
  PYTHONHASHSEED=0 pytest -m db
git checkout -- ai_agents/rag/abstention_threshold.json ai_agents/rag/rerank_calibration.json   # the fit tests rewrite their local sections
```

**3. AI-eval DB tier (CI `eval-db` job).** The `make` target pins its own `atlas_test_aieval` URL.

```bash
docker exec atlas-postgres createdb -U atlas_user atlas_test_aieval
make eval-db                            # writes evals/reports/metrics.json (not committed)
```

**4. Frontend checks (CI `frontend` job).**

```bash
cd frontend && npm ci
npm run lint && npm run format:check && npm run type-check && npm run test && npm run build
```

**5. Playwright end-to-end tests (CI `e2e` job).** Against a dedicated database with every
provider `local` and rate limiting off. Free ports :8000 and :3000 first.

```bash
# Terminal 1: seeded backend on a dedicated e2e database
docker exec atlas-postgres createdb -U atlas_user atlas_test_e2e
export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_e2e
export LLM_PROVIDER=local OCR_PROVIDER=local EMBEDDINGS_PROVIDER=local STORAGE_PROVIDER=local PYTHONHASHSEED=0
export RATE_LIMIT_ENABLED=false CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8000
alembic upgrade head && python -m database.seeds.seed_all
uvicorn backend.app.main_entry.app:app --host 0.0.0.0 --port 8000
# afterwards: unset DATABASE_URL LLM_PROVIDER OCR_PROVIDER EMBEDDINGS_PROVIDER STORAGE_PROVIDER RATE_LIMIT_ENABLED
```

```bash
# Terminal 2: Playwright starts (or reuses) the Next.js dev server on :3000
cd frontend
npx playwright install --with-deps chromium
npm run e2e:p7t4                        # setup + employee + manager + finance projects
```

**Shortcuts.** `make lint` (backend and frontend lint, format check, types), `make test`
(`pytest` + Vitest), `make ci-local` (lint, test, frontend build), `make eval` (hermetic and DB
eval tiers).

<details>
<summary><b>Keyed / live evaluations</b> (not part of CI; need API keys)</summary>

These skip themselves without keys. All except `make eval-live` pin the `atlas_test_aieval`
database; run `unset DATABASE_URL` before `make eval-live`. Keys stay in your shell environment;
the `Makefile` recipes never embed them.

- `make eval-live` · `make eval-live-r5` (RAG faithfulness with Gemini and the Novita reranker) ·
  `make eval-rerank-live` · `make eval-rerank-live-novita` · `make eval-live-matrix` ·
  `make eval-profile-live`

The calibration targets (`calibrate-*`, `threshold-*`, `recalibrate`, `recalibrate-live`) re-fit
τ\*. See [`evals/README.md`](../../evals/README.md) and the `Makefile`.
</details>

---

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Login fails on a fresh Docker stack | `SEED_ON_STARTUP` was still `false`. Set it to `true` and restart the backend, or run `docker compose --env-file .env -f docker/docker-compose.yml exec backend python -m database.seeds.seed_all` |
| Login answers 429 | Rate limit or failed-login backoff. Wait for `Retry-After`; for local test loops set `RATE_LIMIT_ENABLED=false` |
| `docker compose up` fails with "required variable … is missing" | `JWT_SECRET`, `POSTGRES_PASSWORD` or `STORAGE_SECRET_ACCESS_KEY` is empty in `.env`. Always pass `--env-file .env` from the repo root |
| Port 5432 / 8000 / 3000 already in use | Another setup path or a host Postgres is running. Stop it, or set `POSTGRES_PORT` and update `DATABASE_URL` |
| Docker frontend ignores a changed API URL | The compose build arg is still named `NEXT_PUBLIC_API_URL`; the app reads `NEXT_PUBLIC_API_BASE_URL` and falls back to `http://localhost:8000/api/v1` |
| Receipt upload answers 415 | The file's content does not match its claimed type or extension, or it is both an image and a PDF. Upload a plain PNG, JPEG, WebP or PDF |
| Password reset or verification email never arrives | `EMAIL_PROVIDER` is `console` (the default), which delivers nothing. Set `EMAIL_PROVIDER=smtp` with `SMTP_HOST` and `EMAIL_FROM`. Invitations still work: the finance admin sees the link once when inviting |
| PDF receipt upload answers 422 | The PDF is password-protected, damaged, has no pages, has a page larger than 20 × 20 inches, or could not be rendered within the time / memory caps. Remove the password or export it again |
| Uploaded receipt shows placeholder fields locally | Expected with `OCR_PROVIDER=local`: it matches the file's contents |
| Backend exits with `[Atlas] STARTUP ERROR` | `JWT_SECRET` or `DATABASE_URL` is empty, or (production) the startup guard refused an unsafe value; the message names the variable |
| Signed in, but the session never loads / CORS errors | `CORS_ALLOWED_ORIGINS` is missing the origin, or you opened `127.0.0.1` instead of `localhost` |
| `alembic: Can't locate revision` / `No config file 'alembic.ini'` | Run `alembic` from the repo root |
| Startup fails with `STORAGE_PROVIDER=s3` or `OCR_PROVIDER=anthropic` | Not supported. Use `minio` for any S3-compatible store; OCR is `local` or `openrouter` |
| A DB test run fails with `relation "organizations" does not exist` | Two pytest processes shared one test database and one dropped the schema mid-run. Run one suite at a time |

---

## How to verify in five minutes

1. `cp .env.example .env`, enable seeding and set a random `JWT_SECRET`
   ([Path A](#path-a-full-docker-stack)), then
   `docker compose --env-file .env -f docker/docker-compose.yml up -d --build`.
2. Wait for `Atlas seed complete.` in the backend log; check `curl -s http://localhost:8000/health`.
3. Log in at http://localhost:3000 as each demo user (password `change-me-seed-password`) and
   follow the "Good things to try" column of the [demo accounts table](../../README.md#demo-accounts).
   For example: employee submits a report → manager returns it → employee withdraws it →
   finance reimburses an approved report and sees it in the audit trail.
4. Smoke the API:
   ```bash
   curl -s -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"employee@atlas-demo.internal","password":"change-me-seed-password"}'
   ```
5. Run the suites in [Running the tests](#running-the-tests) and compare with
   [TESTING.md](../quality/TESTING.md).
