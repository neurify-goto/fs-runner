-- 原子的専有で次のバッチを取得
-- shard_id を指定すると該当シャードのみから取得
create or replace function public.claim_next_batch(
  p_target_date date,
  p_targeting_id bigint,
  p_run_id text,
  p_limit integer,
  p_shard_id integer default null,
  p_max_daily integer default null
)
returns table(company_id bigint)
language plpgsql
as $$
declare
  v_start_utc timestamp with time zone;
  v_end_utc   timestamp with time zone;
  v_today_success integer := 0;
begin
  -- JST境界のUTC時刻
  v_start_utc := (p_target_date::timestamp AT TIME ZONE 'Asia/Tokyo');
  v_end_utc   := ((p_target_date::timestamp + interval '1 day') AT TIME ZONE 'Asia/Tokyo');

  -- 当日成功数を集計し、上限に達していれば0件返却
  if coalesce(p_max_daily, 0) > 0 then
    select count(*) into v_today_success
      from public.submissions s
     where s.targeting_id = p_targeting_id
       and s.success = true
       and s.submitted_at >= v_start_utc
       and s.submitted_at <  v_end_utc;
    if v_today_success >= p_max_daily then
      return;
    end if;
  end if;

  return query
  with to_claim as (
    select sq.id
    from public.send_queue sq
    left join public.submissions s
      on s.targeting_id = p_targeting_id
     and s.company_id   = sq.company_id
     and s.submitted_at >= v_start_utc
     and s.submitted_at <  v_end_utc
    where sq.target_date_jst = p_target_date
      and sq.targeting_id    = p_targeting_id
      and sq.status          = 'pending'
      and (p_shard_id is null or sq.shard_id = p_shard_id)
      and s.id is null
    order by sq.priority, sq.id
    limit p_limit
    for update of sq skip locked
  ), upd as (
    update public.send_queue sq
       set status = 'assigned', assigned_by = p_run_id, assigned_at = now()
      from to_claim tc
     where sq.id = tc.id
     returning sq.company_id as company_id
  )
  select upd.company_id from upd;
end;
$$;
