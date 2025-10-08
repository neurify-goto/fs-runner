-- Cloud Batch metadata migration
-- Adds execution_mode column and normalizes metadata for batch executions

alter table public.job_executions
  add column if not exists execution_mode text not null default 'cloud_run';

update public.job_executions
   set execution_mode = coalesce(metadata ->> 'execution_mode', 'cloud_run')
 where execution_mode = 'cloud_run';

-- Ensure metadata contains a batch object when batch-specific fields are stored at the top level
update public.job_executions
   set metadata = jsonb_set(
     coalesce(metadata, '{}'::jsonb),
     '{batch}',
     coalesce(metadata -> 'batch', jsonb_build_object())
   )
 where execution_mode = 'batch';

