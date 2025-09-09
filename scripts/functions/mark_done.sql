-- 送信結果を記録し、キューを更新（原子的）
-- 旧シグネチャ（p_field_mappingなし）と従来シグネチャをDROPし、上書き定義に統一
drop function if exists public.mark_done(date,bigint,bigint,boolean,text,jsonb,boolean,timestamp with time zone);
drop function if exists public.mark_done(date,bigint,bigint,boolean,text,jsonb,jsonb,boolean,timestamp with time zone);

create or replace function public.mark_done(
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
  -- 1) 対象キュー行の存在と帰属を確認（あれば FOR UPDATE でロック）
  select sq.assigned_by into v_assigned_by
    from public.send_queue sq
   where sq.target_date_jst = p_target_date
     and sq.targeting_id    = p_targeting_id
     and sq.company_id      = p_company_id
   for update;
  v_queue_found := found;

  if v_queue_found then
    -- run_id 帰属チェック
    if v_assigned_by is null or v_assigned_by = p_run_id then
      update public.send_queue
         set status = case when p_success then 'done' else 'failed' end,
             attempts = attempts + 1
       where target_date_jst = p_target_date
         and targeting_id    = p_targeting_id
         and company_id      = p_company_id;
      get diagnostics v_updated = row_count;
    else
      -- 帰属不一致: submissions は記録せず、NOTICE のみ返す（整合性維持）
      raise notice 'mark_done: run_id mismatch (queue exists). date=%, targeting_id=%, company_id=%, run_id=%, assigned_by=%',
        p_target_date, p_targeting_id, p_company_id, p_run_id, v_assigned_by;
      return 0;
    end if;
  else
    -- キュー非経由（直接実行）: v_updated は 0 のまま、submissions は記録する
    v_updated := 0;
  end if;

  -- 2) submissions へ記録（JST時刻は呼び出し側で計算済）
  insert into public.submissions(
    targeting_id, company_id, success, error_type, classify_detail, field_mapping, submitted_at
  ) values (
    p_targeting_id, p_company_id, p_success, p_error_type, p_classify_detail, p_field_mapping, p_submitted_at
  );

  -- 3) companies の bot_protection を反映（true のみ）
  if p_bot_protection is true then
    update public.companies set bot_protection_detected = true where id = p_company_id;
  end if;

  -- 4) 結果
  if not v_queue_found then
    raise notice 'mark_done: queue row not found (direct execution allowed). date=%, targeting_id=%, company_id=%',
      p_target_date, p_targeting_id, p_company_id;
  end if;
  return v_updated;
end;
$$;
