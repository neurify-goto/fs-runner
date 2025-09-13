-- 原子的専有で次のバッチを取得（send_queue_extra 版）
drop function if exists public.claim_next_batch_extra(date,bigint,text,integer,integer);
drop function if exists public.claim_next_batch_extra(date,bigint,text,integer,integer,integer);
drop function if exists public.claim_next_batch_extra(date,bigint,text,integer,integer,integer,integer);

create or replace function public.claim_next_batch_extra(
  p_target_date date,
  p_targeting_id bigint,
  p_run_id text,
  p_limit integer,
  p_shard_id integer default null,
  p_max_daily integer default null,
  p_assigned_grace_minutes integer default 2
)
returns table(company_id bigint, queue_id bigint, assigned_at timestamp with time zone)
language plpgsql
as $$
declare
  v_start_utc timestamp with time zone;
  v_end_utc   timestamp with time zone;
  v_today_success integer := 0;
  v_today_assigned integer := 0;
  v_effective_limit integer := 0;
  v_date_key integer := 0;
  v_lock_key bigint := 0;
begin
  if coalesce(p_max_daily, 0) > 10000 then
    raise exception 'p_max_daily exceeds maximum allowed value (10000)';
  end if;
  v_start_utc := (p_target_date::timestamp AT TIME ZONE 'Asia/Tokyo');
  v_end_utc   := ((p_target_date::timestamp + interval '1 day') AT TIME ZONE 'Asia/Tokyo');
  v_date_key  := (to_char(p_target_date, 'YYYYMMDD'))::integer;
  v_lock_key := (('x' || substr(md5(p_targeting_id::text || ':' || v_date_key::text), 1, 16))::bit(64))::bigint;
  perform pg_advisory_xact_lock(v_lock_key);

  if coalesce(p_max_daily, 0) > 0 then
    select count(*) into v_today_success
      from public.submissions s
     where s.targeting_id = p_targeting_id
       and s.success = true
       and s.submitted_at >= v_start_utc
       and s.submitted_at <  v_end_utc;

    select count(*) into v_today_assigned
      from public.send_queue_extra sq
     where sq.target_date_jst = p_target_date
       and sq.targeting_id    = p_targeting_id
       and sq.status          = 'assigned'
       and (sq.assigned_at is null or sq.assigned_at < now() - (coalesce(p_assigned_grace_minutes, 2) || ' minutes')::interval);

    v_effective_limit := greatest(0, least(p_limit, p_max_daily - v_today_success - v_today_assigned));
    if v_effective_limit <= 0 then
      raise notice 'claim_next_batch_extra: capacity exhausted (targeting_id=%, success=% assigned=% / cap=%).', p_targeting_id, v_today_success, v_today_assigned, p_max_daily;
      return;
    end if;
  else
    v_effective_limit := p_limit;
  end if;

  return query
  with to_claim as (
    select sq.id
      from public.send_queue_extra sq
     where sq.target_date_jst = p_target_date
       and sq.targeting_id    = p_targeting_id
       and sq.status          = 'pending'
       and (p_shard_id is null or sq.shard_id = p_shard_id)
     order by sq.priority, sq.id
     limit v_effective_limit
     for update of sq skip locked
  ), upd as (
    update public.send_queue_extra sq
       set status = 'assigned', assigned_by = p_run_id, assigned_at = now()
      from to_claim tc
     where sq.id = tc.id
     returning sq.company_id as company_id, sq.id as queue_id, sq.assigned_at as assigned_at
  )
  select upd.company_id, upd.queue_id, upd.assigned_at from upd;
end;
$$;

grant execute on function public.claim_next_batch_extra(date,bigint,text,integer,integer,integer,integer)
  to authenticated, service_role;

