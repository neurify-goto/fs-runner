-- Add field_mapping column to submissions table if missing (idempotent)
alter table if exists public.submissions
  add column if not exists field_mapping jsonb null;
