-- scripts/indexes/companies_host_normalized.sql
-- Purpose:
--   company_url から抽出した正規化ホストに対する関数インデックス。
--   add_extra_companies 系スクリプトの NOT EXISTS 判定を高速化。

-- Rationale:
-- - Matches the exact normalization used in NOT EXISTS filter
-- - Covers protocol-relative URLs ('//example.com') and RFC3986-compliant schemes
-- - Maintenance: keep this expression in sync with add_extra_companies*.sql

create index IF not exists idx_companies_host_normalized on public.companies using btree (
  LOWER(
    split_part(
      regexp_replace(
        regexp_replace(TRIM(company_url), '^([A-Za-z][A-Za-z0-9+.-]*://|//)', ''),
        '[/?#].*$', ''
      ),
      ':', 1
    )
  )
) TABLESPACE pg_default
where
  (company_url is not null and company_url <> ''::text);

-- End of script.
