-- 送信結果を記録し、キューを更新（send_queue_extra / companies_extra 版）
drop function if exists public.mark_done_extra(date,bigint,bigint,boolean,text,jsonb,boolean,timestamp with time zone);
drop function if exists public.mark_done_extra(date,bigint,bigint,boolean,text,jsonb,jsonb,boolean,timestamp with time zone);

create or replace function public.mark_done_extra(
  p_target_date date,
  p_targeting_id bigint,
  p_company_id bigint,
  p_success boolean,
  p_error_type text,
  p_classify_detail jsonb,
  p_field_mapping jsonb,
  p_bot_protection boolean,
  p_submitted_at timestamptz,
  p_run_id text
)
returns integer
language plpgsql
as $$
declare
  v_updated integer := 0;
  v_assigned_by text;
  v_queue_found boolean := false;
begin
  -- キュー行の存在と帰属を確認
  select sq.assigned_by into v_assigned_by
    from public.send_queue_extra sq
   where sq.target_date_jst = p_target_date
     and sq.targeting_id    = p_targeting_id
     and sq.company_id      = p_company_id
   for update;
  v_queue_found := found;

  if v_queue_found then
    if v_assigned_by is null or v_assigned_by = p_run_id then
      update public.send_queue_extra
         set status = case when p_success then 'done' else 'failed' end,
             attempts = attempts + 1
       where target_date_jst = p_target_date
         and targeting_id    = p_targeting_id
         and company_id      = p_company_id;
      get diagnostics v_updated = row_count;
    else
      raise notice 'mark_done_extra: run_id mismatch (queue exists). date=%, targeting_id=%, company_id=%, run_id=%, assigned_by=%',
        p_target_date, p_targeting_id, p_company_id, p_run_id, v_assigned_by;
      return 0;
    end if;
  else
    v_updated := 0;
  end if;

  -- submissions へ記録
  insert into public.submissions(
    targeting_id, company_id, success, error_type, classify_detail, field_mapping, submitted_at
  ) values (
    p_targeting_id, p_company_id, p_success, p_error_type, p_classify_detail, p_field_mapping, p_submitted_at
  );

  -- companies_extra の bot_protection を反映（true のみ）
  if p_bot_protection is true then
    update public.companies_extra set bot_protection_detected = true where id = p_company_id;
  end if;

  if not v_queue_found then
    raise notice 'mark_done_extra: queue row not found (direct execution allowed). date=%, targeting_id=%, company_id=%',
      p_target_date, p_targeting_id, p_company_id;
  end if;
  return v_updated;
end;
$$;

grant execute on function public.mark_done_extra(date,bigint,bigint,boolean,text,jsonb,jsonb,boolean,timestamp with time zone,text)
  to authenticated, service_role;

