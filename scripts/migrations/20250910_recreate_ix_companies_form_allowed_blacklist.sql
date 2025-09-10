-- Migration: Recreate partial index for send_queue candidate filter
-- Purpose: include `black IS NULL` in the predicate used during queue generation
-- Context: replaces existing `ix_companies_form_allowed` that did not filter by blacklist

-- NOTE:
-- - If you require minimal locking, run CREATE INDEX CONCURRENTLY on a temp name,
--   then DROP INDEX CONCURRENTLY and RENAME. For simplicity and clarity, this
--   migration uses a straightforward DROP → CREATE.
-- - Execute in a maintenance window if your traffic is sensitive to DDL locks.

-- 1) Drop the old partial index if it exists
drop index if exists public.ix_companies_form_allowed;

-- 2) Create the updated partial index (black IS NULL added)
create index if not exists ix_companies_form_allowed
  on public.companies (id)
  where (
    form_url is not null
    and black is null
    and coalesce(prohibition_detected, false) = false
  );

-- 3) Optional: encourage planner refresh
-- analyze public.companies;

