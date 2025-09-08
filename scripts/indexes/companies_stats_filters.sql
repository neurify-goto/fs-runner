-- companies: statsダッシュボード（4項目）の集計高速化向け部分インデックス
-- 用途: scripts/companies_stats_rpc_functions.sql の get_companies_stats_all()
-- 方針: duplication IS NULL を含むフィルタの高速化。

-- 1) フォーム未探索: company_url IS NOT NULL AND form_found IS NULL AND duplication IS NULL
create index if not exists ix_companies_stats_form_not_explored
  on public.companies (id)
  where company_url is not null and form_found is null and duplication is null;

-- 2) フォームURLあり: form_url IS NOT NULL AND duplication IS NULL
create index if not exists ix_companies_stats_with_form_url
  on public.companies (id)
  where form_url is not null and duplication is null;

