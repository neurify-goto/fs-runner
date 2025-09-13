-- 取り残しの再配布（send_queue_extra 版）
create or replace function public.requeue_stale_assigned_extra(
  p_target_date date,
  p_targeting_id bigint,
  p_stale_minutes integer
)
returns integer
language plpgsql
as $$
declare
  v_cnt integer := 0;
begin
  update public.send_queue_extra
    set status = 'pending', assigned_by = null, assigned_at = null
  where target_date_jst = p_target_date
    and targeting_id = p_targeting_id
    and status = 'assigned'
    and assigned_at is not null
    and assigned_at < now() - (p_stale_minutes || ' minutes')::interval;
  get diagnostics v_cnt = row_count;
  return v_cnt;
end;
$$;

grant execute on function public.requeue_stale_assigned_extra(date,bigint,integer)
  to authenticated, service_role;

