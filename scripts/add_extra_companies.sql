-- scripts/add_extra_companies.sql
-- Purpose:
--   Insert records from public.extra_companies into public.companies,
--   avoiding duplicates by matching the hostname (domain incl. subdomain)
--   of company_url. Columns not present in extra_companies are set to NULL.
--   The primary key id is assigned sequentially as (max(id) + row_number).
--
-- Notes:
--   - Hostname comparison distinguishes subdomains (e.g., www.example.com != example.com).
--   - Hostnames are compared in lowercase and after stripping scheme/port.
--   - Rows with NULL/empty company_url in extra_companies are treated as having
--     unknown host and are inserted (no duplicate check possible by host).

WITH base AS (
  SELECT COALESCE(MAX(id), 0) AS max_id
  FROM public.companies
),
src AS (
  SELECT
    ec.client,
    ec.company_name,
    ec.postal_code,
    ec.location,
    ec.prefecture,
    ec.tel,
    ec.company_url,
    NULLIF(TRIM(ec.company_url), '') AS company_url_norm,
    -- Host normalization:
    -- 1) Strip scheme (RFC3986: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )) + '://' OR protocol-relative '//'
    -- 2) Cut off path/query/fragment
    -- 3) Drop port by splitting at ':'
    -- 4) Lowercase
    CASE
      WHEN NULLIF(TRIM(ec.company_url), '') IS NULL THEN NULL
      ELSE NULLIF(
        LOWER(
          split_part(
            regexp_replace(
              regexp_replace(TRIM(ec.company_url), '^([A-Za-z][A-Za-z0-9+.-]*://|//)', ''),
              '[/?#].*$', ''
            ),
            ':', 1
          )
        ),
        ''
      )
    END AS host
  FROM public.extra_companies ec
),
dedup_src AS (
  -- extra_companies 内で同一ホストが複数ある場合は 1 件に絞る
  -- host が NULL の行は重複判定不可のため全て残す
  SELECT *
  FROM (
    SELECT
      s.*,
      CASE
        WHEN s.host IS NULL THEN NULL
        ELSE ROW_NUMBER() OVER (
               PARTITION BY s.host
               ORDER BY COALESCE(s.company_name, '') ASC,
                        COALESCE(s.company_url_norm, '') ASC,
                        COALESCE(s.postal_code, '') ASC,
                        COALESCE(s.tel, '') ASC,
                        COALESCE(s.location, '') ASC
             )
      END AS host_rank
    FROM src s
  ) x
  WHERE x.host_rank IS NULL OR x.host_rank = 1
),
to_insert AS (
  -- companies に同一ホストが存在しない行のみ残す（サブドメインは区別）
  SELECT ds.*
  FROM dedup_src ds
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.companies c
    WHERE c.company_url IS NOT NULL
      AND c.company_url <> ''
      AND LOWER(
            split_part(
              regexp_replace(
                regexp_replace(TRIM(c.company_url), '^([A-Za-z][A-Za-z0-9+.-]*://|//)', ''),
                '[/?#].*$', ''
              ),
              ':', 1
            )
          ) = ds.host
  )
),
 numbered AS (
  SELECT
    (SELECT max_id FROM base)
      + ROW_NUMBER() OVER (
          -- ORDER KEY (keep in sync with dedup_src):
          -- company_name, company_url_norm, postal_code, tel, location
          ORDER BY COALESCE(company_name, ''),
                   COALESCE(company_url_norm, ''),
                   COALESCE(postal_code, ''),
                   COALESCE(tel, ''),
                   COALESCE(location, '')
        ) AS new_id,
    *
  FROM to_insert
)
INSERT INTO public.companies (
  id,
  company_name,
  company_name_kana,
  listed,
  industry_category_major,
  industry_category_middle,
  industry_category_minor,
  industry_category_detail,
  industry_category_id,
  location,
  representative,
  established_year,
  established_month,
  detail_page,
  postal_code,
  company_url,
  tel,
  closing_month,
  average_age,
  average_salary,
  form_url,
  batch_id,
  prohibition_candidates,
  instruction_json,
  instruction_valid,
  form_found,
  form_finder_queued,
  form_analyzer_queued,
  prohibition_detected,
  prefecture,
  employee_count,
  capital,
  bot_protection_detected,
  national_id,
  analyzer_queued_at,
  fetch_detail_queued,
  duplication,
  black
)
SELECT
  new_id AS id,
  company_name,
  NULL AS company_name_kana,
  NULL AS listed,
  NULL AS industry_category_major,
  NULL AS industry_category_middle,
  NULL AS industry_category_minor,
  NULL AS industry_category_detail,
  NULL::bigint AS industry_category_id,
  location,
  NULL AS representative,
  NULL::integer AS established_year,
  NULL::smallint AS established_month,
  NULL AS detail_page,
  postal_code,
  company_url,
  tel,
  NULL::smallint AS closing_month,
  NULL::real AS average_age,
  NULL::bigint AS average_salary,
  NULL AS form_url,
  NULL AS batch_id,
  NULL AS prohibition_candidates,
  NULL AS instruction_json,
  NULL::boolean AS instruction_valid,
  NULL::boolean AS form_found,
  NULL::boolean AS form_finder_queued,
  NULL::boolean AS form_analyzer_queued,
  NULL::boolean AS prohibition_detected,
  prefecture,
  NULL::bigint AS employee_count,
  NULL::bigint AS capital,
  NULL::boolean AS bot_protection_detected,
  NULL AS national_id,
  NULL::timestamptz AS analyzer_queued_at,
  NULL::boolean AS fetch_detail_queued,
  NULL::boolean AS duplication,
  NULL::boolean AS black
FROM numbered;

-- End of script.
