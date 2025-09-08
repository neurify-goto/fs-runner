-- companies.prefecture の絞り込み最適化用インデックス
-- 用途: create_queue_for_targeting 内での targetting_sql 条件（例: prefecture = '東京都'）
-- 既存のベース部分インデックス(ix_companies_form_allowed)を補完
create index if not exists idx_companies_prefecture
  on public.companies (prefecture)
  where form_url is not null and coalesce(prohibition_detected, false) = false;

