-- 用途: scripts/companies_stats_rpc_functions.sql の get_companies_stats_all()
-- 方針: duplication IS NULL を含むフィルタの高速化。

-- 1) フォーム未探索: company_url IS NOT NULL AND form_found IS NULL AND duplication IS NULL
create index if not exists ix_companies_stats_form_not_explored
  on public.companies (id)
  where company_url is not null and form_found is null and duplication is null;

-- 2) フォームURLあり: form_found = true
--   RPC(get_companies_stats_all) の with_form_url_count が form_found=true を参照するため、
--   インデックスも predicate を form_found=true に合わせる。
drop index if exists public.ix_companies_stats_with_form_url;
create index if not exists ix_companies_stats_with_form_found
  on public.companies (id)
  where form_found is true;

-- 3) 有効フォーム: form_found = true AND prohibition_detected IS NULL AND duplication IS NULL AND black IS NULL
create index if not exists ix_companies_stats_valid_form
  on public.companies (id)
  where form_found is true and prohibition_detected is null and duplication is null and black is null;
