# Phase 11: Public-demo security, shared demo accounts (track `ps/demo`)

Base: `749469f` (branch `product-gaps`, alembic head `0023_entry_release_lineage`).
Branch: `ps/demo`. No migration was needed. Deployed staging is unchanged and still
runs `2fbe114`. `DEMO_MODE` is **not** turned on anywhere; the owner decides that.

## Why

The public demo shares well-known accounts, and the README publishes their password:
`employee@`, `manager@`, `finance@`, `manager2@` and `employee2@atlas-demo.internal`.
`finance@` is a full administrator. One visitor could ruin the demo for everyone, for
example by deactivating `manager@`, demoting or re-grading accounts, or archiving the
policy rules (archiving is terminal). There was also no way to clean up the data
visitors leave behind.

## What was built

| Area | Files |
| --- | --- |
| Settings `DEMO_MODE` (default false) and `DEMO_RESTRICT_DESTRUCTIVE` (unset = follows DEMO_MODE) | `config/settings/base.py` |
| Demo protections (identification, guards, audited 403s) | `backend/app/core/demo.py` (new) |
| Users admin hooks: edit and deactivate refused for protected accounts | `backend/app/modules/identity/admin.py` (two calls) |
| `demo_protected` flag on every user row | `backend/app/modules/identity/schemas.py`, `service.py` (`with_manager_names`) |
| Seeded policy rule archive refused | `backend/app/modules/policy/service.py` (`update_rule_status`, one call) |
| `GET /api/v1/public/config` (public, `{"demo_mode": bool}`) | `backend/app/api/public_config.py` (new), `backend/app/api/v1.py` |
| Demo reset operator tool | `database/seeds/reset_demo_data.py` (new) |
| Runbook | `docs/runbooks/demo-reset.md` (new) |
| render.yaml: documents the settings as comments (not set) | `render.yaml` |
| Users page: lock badge with tooltip, edit and deactivate disabled with a reason | `frontend/components/users/users-screen.tsx`, `lib/types/identity.ts` |
| "Demo environment" banner (dismissible per session) | `frontend/components/atlas/demo-banner.tsx` (new), `lib/query/public-config-hooks.ts` (new), `components/atlas/app-shell.tsx` (one line) |

### Protections (only when `DEMO_MODE=true`, all server-side)

- **Protected demo accounts.** An account is protected when its *stored* email is one
  of the seed's `DEMO_GRADES` emails **and** its *stored* organisation is named
  exactly `Atlas Demo Corp` (`seed_demo_grades.DEMO_ORG_NAME`). Nothing the client
  sends is trusted. For these accounts, any finance admin's `PATCH /users/{id}` with
  any field (name, role, manager, department, grade) and `POST /users/{id}/deactivate`
  answer **403 `DEMO_ACCOUNT_PROTECTED`** with a friendly message. The refusal is
  audited as `user.demo_protected_refused` and committed before the error, because
  the request transaction is otherwise discarded. Email, password, status and
  organisation have no write path at all: the PATCH body forbids extra fields (422),
  and no password or organisation write endpoint exists. Tests pin that.
  Reactivation stays allowed, because it can only restore a demo account.
- **Visitor-created users** (any other email) stay fully manageable. The same seeded
  email in **another** organisation is not protected.
- **Last-finance-admin and self-change guards** are unchanged and still apply.
- **Organisation login code**: `GET /organization` is read-only, and PATCH and PUT
  return 405. Tests pin that.
- **Seeded policy rules** (with `DEMO_RESTRICT_DESTRUCTIVE`, which follows DEMO_MODE
  unless set) cannot be archived: **403 `DEMO_RULE_PROTECTED`**, audited as
  `policy_rule.demo_protected_refused`, so the refusal shows in the rule's history. A
  seeded rule is a rule of the demo org's stub policy (`POLICY_NAME` and
  `POLICY_VERSION`) whose (clause, type, target) is one the seed creates. Activating
  the seeded draft, authoring new rules, and archiving visitor-authored rules all
  still work. There is no rule-definition edit endpoint, and the activate and archive
  paths are already audited.

## Key decisions

- **403, not 409.** The refusal is a permission policy, not a state conflict. The
  code `DEMO_ACCOUNT_PROTECTED` lets the UI and tests tell it apart from RBAC 403s.
- **A protected account refuses every edit**, including name and department, which
  the brief did not list. Changing a shared account's display name is a defacement
  vector. Visitors still try every user-admin feature on users they create.
- **`DEMO_RESTRICT_DESTRUCTIVE`**: an audit of every mutating route (`grep
  @router.(post|patch|put|delete)`) found **no bulk destructive finance action**.
  Deletes are owner-only, there are no policy-document write endpoints, and exports
  are read-only. The only irreversible finance action left is archiving a seeded
  rule, so the flag governs exactly that. The owner can lift it while keeping
  account protection.
- **The reset wipes and re-seeds instead of diffing.** It deletes **all** demo-org
  transactional rows and re-runs the idempotent seed steps in the same transaction.
  This is the only way to also undo visitors' changes to *seeded* records (a withdrawn
  seeded report, a matched card swipe, a deleted seeded receipt). The cost is that a
  dry run counts the seed's own rows as deletions. The output says so.
- **Audit rows are kept.** `audit_log` and `ai_decision_logs` are append-only, so the
  reset never deletes them. Rows whose actor was a deleted visitor keep their content,
  and `actor_id` becomes NULL through the FK's own `ON DELETE SET NULL`. The reset
  writes one `demo.reset` row (counts plus restored account ids) and one
  `policy_rule.demo_reset` row per restored rule.
- **Two tenant-isolation proofs inside the apply transaction.** (1) An FK walk over
  `pg_constraint` refuses the run if any other organisation's row references a row
  the reset would delete. (2) An in-database md5 of every other organisation's rows
  in every table is compared before and after, and any difference rolls back.
  Tables with no organisation scope are hashed whole.
- **Safety flags.** Dry run is the default and runs in `SET TRANSACTION READ ONLY`.
  An apply needs `DEMO_RESET_ENABLED=true` from the process environment (never
  `.env`), `--org "Atlas Demo Corp"` exactly, and exactly one organisation with that
  name. The demo org row is locked, so resets cannot overlap.
- **Storage after the commit, best-effort.** Visitor receipt objects are deleted only
  after their rows are gone, so no row can point at a missing object. A failure is
  counted and reported. The seed's `receipts/seed/*` images are never deleted.
- **Seed account table.** `reset_demo_data.SEED_ACCOUNTS` mirrors `identity_seed`
  and `_seed_org_chart_extras` instead of editing those hot seed files. An
  import-time assert ties it to `DEMO_GRADES`, and the reset test compares a reset
  org with a **fresh `seed_all`**, so any drift fails the build.
- **No automatic reset.** The runbook gives the Render Shell, one-off job, operator
  machine and (later) cron commands.

## Known limits

- The guards identify the demo org by name. Organisations cannot be created or
  renamed through the API, so the name is server-owned. A second org with the same
  name would also be protected, and the reset refuses to run while two exist.
- A visitor-authored rule that reuses a seeded (clause, type, target) is also treated
  as seeded and cannot be archived. It is harmless, and the reset removes it.
- The reset does not run the boot repairs (draft pre-submit flags, elapsed-trip
  sweep). They run on the next boot, as they do after a fresh seed.
- After a reset, JWTs of deleted visitor accounts stop working (user not found).
  Seeded accounts' existing tokens stay valid until they expire.
- The `<button>` inside `<button>` nesting warning from `AtlasButton`'s
  disabled-with-reason tooltip already existed (the users page's own-row button has
  it too). It was not changed here.

## How to verify

```bash
R=<local-runner-dir>/ps/run_demo.sh
$R -q backend/tests/test_ps_demo_protection_db.py   # 61 tests: every op x every account
$R -q backend/tests/test_ps_demo_reset_db.py        # 16 tests: own throwaway DB
cd frontend && npx vitest run __tests__/demo         # 8 tests: badge, disabled controls, banner
```

Mutation proofs (each guard reverted: red; restored: green). Account update guard
36 failed. Deactivate guard 7. Rule-archive guard 2. Org check 1. DEMO_MODE switch 2.
Rule org check 1. Restrict flag 1. `demo_protected` flag 1. Refusal-audit commit 1.
Public config 1. Reset: env flag 3, org name 4, account restore 3, rule restore 6,
visitor-user delete 6, FK proof (apply) 1, FK proof (query) 1, isolation hash 1,
storage best-effort 1, READ ONLY 1, seed-key filter 2, re-seed 7, wipe scope (all
error), duplicate org 1, password restore 3. UI: edit lock 2, deactivate lock 1,
reactivate 1, badge 1, banner gate 2, session dismissal 1.

Manual staging check, once the owner sets `DEMO_MODE=true`: sign in as `finance@`.
The Users page shows "Demo" lock badges on the five accounts, and their edit and
deactivate buttons are disabled with a tooltip. `curl -X POST .../users/<manager
id>/deactivate` with the finance token returns 403 `DEMO_ACCOUNT_PROTECTED`. The
banner appears and stays dismissed until the browser session ends. For the reset,
follow `docs/runbooks/demo-reset.md`, dry run first.

## Gates (this branch)

- Hermetic: 3531 passed, 4 skipped. Full DB: 1823 passed, 8 skipped. eval-db: 48 passed.
- ruff check, ruff format --check and mypy (backend config evals) are clean.
  `database/seeds/reset_demo_data.py` was also mypy-checked on its own.
- Frontend: vitest 114 files / 829 tests, type-check, lint and format:check are clean.

## QA findings

- **Order-dependent test on the base.** `test_kl_grade_db.py::TestDemoGradeBootStep::
  test_sets_only_unset_demo_grades_and_never_overwrites` failed in a full DB run on
  `749469f` too (reproduced on a clean export of the base). `test_c7_22` commits a
  full seed first, and `seed_demo_grades` then resolves that earlier "Atlas Demo Corp".
  Fix: the test renames earlier demo orgs inside its rolled-back session. The
  assertions are unchanged.
- **Intended test updates.** The contract matrix registers `GET /public/config` as
  public, so the count goes from 66 to 67, and a body-less public GET must answer 200.
  `__tests__/c7/route-gate.test.tsx` stubs `DemoBanner` the same way it stubs the
  other query-backed shell children.
- A full DB run rewrites `ai_agents/rag/abstention_threshold.json` and
  `rerank_calibration.json`. This already happened before this branch. Those changes
  were reverted, not committed.
