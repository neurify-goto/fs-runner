-- companies: send_queue 生成時のベースフィルタに最適化した部分インデックス
-- 条件: form_url IS NOT NULL AND prohibition_detected = false（NULLはfalse扱い）
-- 用途: scripts/functions/create_queue_for_targeting.sql の Stage1/Stage2 双方で活用
create index if not exists ix_companies_form_allowed
  on public.companies (id)
  where form_url is not null and coalesce(prohibition_detected, false) = false;

