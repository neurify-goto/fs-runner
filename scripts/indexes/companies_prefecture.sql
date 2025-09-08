-- 命名規則統一のため ix_ プレフィックスを使用
drop index if exists idx_companies_prefecture;
create index if not exists ix_companies_prefecture
  on public.companies (prefecture)
  where form_url is not null and coalesce(prohibition_detected, false) = false;
