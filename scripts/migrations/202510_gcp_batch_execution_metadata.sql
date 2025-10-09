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

-- Migrate legacy batch_* fields into metadata.batch
update public.job_executions
   set metadata = (
     (
       coalesce(metadata, '{}'::jsonb)
         - 'batch_job_name'
         - 'batch_task_group'
         - 'batch_array_size'
         - 'batch_parallelism'
         - 'batch_machine_type'
         - 'batch_cpu_milli'
         - 'batch_memory_mb'
         - 'batch_prefer_spot'
         - 'batch_allow_on_demand'
     )
     || jsonb_build_object(
          'batch',
          jsonb_strip_nulls(
            coalesce(metadata -> 'batch', '{}'::jsonb)
            || jsonb_build_object(
                 'job_name', metadata ->> 'batch_job_name',
                 'task_group', metadata ->> 'batch_task_group',
                 'task_count', CASE WHEN metadata ? 'batch_array_size' THEN to_jsonb((metadata ->> 'batch_array_size')::int) ELSE NULL::jsonb END,
                 'parallelism', CASE WHEN metadata ? 'batch_parallelism' THEN to_jsonb((metadata ->> 'batch_parallelism')::int) ELSE NULL::jsonb END,
                 'machine_type', metadata ->> 'batch_machine_type',
                 'cpu_milli', CASE WHEN metadata ? 'batch_cpu_milli' THEN to_jsonb((metadata ->> 'batch_cpu_milli')::int) ELSE NULL::jsonb END,
                 'memory_mb', CASE WHEN metadata ? 'batch_memory_mb' THEN to_jsonb((metadata ->> 'batch_memory_mb')::int) ELSE NULL::jsonb END,
                 'prefer_spot', CASE WHEN metadata ? 'batch_prefer_spot' THEN to_jsonb((metadata ->> 'batch_prefer_spot')::boolean) ELSE NULL::jsonb END,
                 'allow_on_demand', CASE WHEN metadata ? 'batch_allow_on_demand' THEN to_jsonb((metadata ->> 'batch_allow_on_demand')::boolean) ELSE NULL::jsonb END
               )
          )
        )
   )
 where metadata ? 'batch_job_name'
    or metadata ? 'batch_task_group'
    or metadata ? 'batch_array_size'
    or metadata ? 'batch_parallelism'
    or metadata ? 'batch_machine_type'
    or metadata ? 'batch_cpu_milli'
    or metadata ? 'batch_memory_mb'
    or metadata ? 'batch_prefer_spot'
    or metadata ? 'batch_allow_on_demand';

-- Migrate legacy cloud_run_* fields into metadata.cloud_run
update public.job_executions
   set metadata = (
     (
       coalesce(metadata, '{}'::jsonb)
         - 'cloud_run_operation'
         - 'cloud_run_execution'
     )
     || jsonb_build_object(
          'cloud_run',
          jsonb_strip_nulls(
            coalesce(metadata -> 'cloud_run', '{}'::jsonb)
            || jsonb_build_object(
                 'operation', metadata ->> 'cloud_run_operation',
                 'execution', metadata ->> 'cloud_run_execution'
               )
          )
        )
   )
 where metadata ? 'cloud_run_operation'
    or metadata ? 'cloud_run_execution';
