-- platform_memory DB init. Runs once when the data volume is first created.
-- Idempotent: re-running on a fresh volume is safe.
--
-- This database is DEDICATED to platform_memory and is intentionally kept separate
-- from the platform's main Postgres (which is an external managed instance with no
-- AGE/pgvector guarantees). See docs/nexus-migration-plan §6.

CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
-- Trigram search for exact-identifier lexical retrieval (ADR-016). The service
-- also attempts CREATE EXTENSION at ensure_schema time and degrades gracefully
-- without it; installing here keeps managed deployments deterministic.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- AGE requires loading the library and ag_catalog on the search_path.
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Create the knowledge graph if it does not exist yet.
-- Graph name comes from CB_GRAPH_NAME (default: company_brain).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'company_brain') THEN
        PERFORM ag_catalog.create_graph('company_brain');
    END IF;
END
$$;
