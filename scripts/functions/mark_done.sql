-- 送信結果を記録し、キューを更新（原子的）
create or replace function public.mark_done(
  p_target_date date,
  p_targeting_id bigint,
  p_company_id bigint,
  p_success boolean,
  p_error_type text,
  p_classify_detail jsonb,
  p_bot_protection boolean,
  p_submitted_at timestamptz
)
returns void
language plpgsql
as $$
begin
  -- submissions へ記録（JST時刻は呼び出し側から受け取る）
  insert into public.submissions(
    targeting_id, company_id, success, error_type, classify_detail, submitted_at
  ) values (
    p_targeting_id, p_company_id, p_success, p_error_type, p_classify_detail, p_submitted_at
  );

  -- companies の bot_protection を反映（true のみ）
  if p_bot_protection is true then
    update public.companies set bot_protection_detected = true where id = p_company_id;
  end if;

  -- send_queue を更新
  update public.send_queue
    set status = case when p_success then 'done' else 'failed' end,
        attempts = attempts + 1
  where target_date_jst = p_target_date and targeting_id = p_targeting_id and company_id = p_company_id;
end;
$$;

