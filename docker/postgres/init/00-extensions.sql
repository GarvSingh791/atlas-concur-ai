-- Atlas — Postgres bootstrap.
--
-- Runs ONCE, automatically, on first initialisation of an empty data volume
-- (Postgres executes every *.sql in /docker-entrypoint-initdb.d at init time).
-- Provisions the extensions the schema depends on but the Alembic migrations do
-- NOT create themselves (the test conftest creates these same two):
--
--   * vector   — pgvector type backing policy_embeddings (migration 0004).
--   * pgcrypto — gen_random_uuid() defaults used across the schema.
--
-- IF NOT EXISTS keeps this idempotent and harmless on a pre-provisioned DB. The
-- pgvector/pgvector:pg16 image ships both extensions; this only enables them.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
