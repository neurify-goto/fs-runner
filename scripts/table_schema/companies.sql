create table public.companies (
  id bigint not null,
  company_name text null,
  company_name_kana text null,
  listed text null,
  industry_category_major text null,
  industry_category_middle text null,
  industry_category_minor text null,
  industry_category_detail text null,
  industry_category_id bigint null,
  location text null,
  representative text null,
  established_year integer null,
  established_month smallint null,
  detail_page text null,
  company_id text null,
  postal_code text null,
  company_url text null,
  tel text null,
  closing_month smallint null,
  average_age real null,
  average_salary bigint null,
  form_url text null,
  batch_id text null,
  prohibition_candidates text null,
  instruction_json text null,
  instruction_valid boolean null,
  fetch_detail_queued boolean null,
  form_found boolean null,
  form_finder_queued boolean null,
  form_analyzer_queued boolean null,
  prohibition_detected boolean null,
  prefecture text null,
  employee_count bigint null,
  capital bigint null,
  bot_protection_detected boolean null,
  national_id text null,
  analyzer_queued_at timestamp with time zone null,
  constraint companies_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_companies_company_url_form_found on public.companies using btree (company_url, form_found) TABLESPACE pg_default
where
  (company_url is not null);

create index IF not exists idx_companies_industry_category_major on public.companies using btree (industry_category_major) TABLESPACE pg_default;

create index IF not exists idx_companies_industry_category_middle on public.companies using btree (industry_category_middle) TABLESPACE pg_default;

create index IF not exists idx_companies_stats_optimized on public.companies using btree (company_url, form_found) TABLESPACE pg_default
where
  (company_url is not null);

create index IF not exists idx_companies_form_unanalyzed on public.companies using btree (form_url, prohibition_detected) TABLESPACE pg_default
where
  (form_url is not null);

create index IF not exists idx_companies_valid_instruction on public.companies using btree (instruction_valid) TABLESPACE pg_default
where
  (instruction_json is not null);

create index IF not exists idx_companies_company_url_notnull on public.companies using btree (company_url) TABLESPACE pg_default
where
  (company_url is not null);

create index IF not exists idx_companies_instruction_valid on public.companies using btree (instruction_valid) TABLESPACE pg_default;

create index IF not exists idx_companies_processing_status on public.companies using btree (company_url, fetch_detail_queued, id) TABLESPACE pg_default
where
  (
    (company_url is null)
    and (fetch_detail_queued is null)
  );

create index IF not exists idx_companies_completion_status on public.companies using btree (company_url) TABLESPACE pg_default
where
  (
    (company_url is not null)
    and (company_url <> ''::text)
  );

create index IF not exists idx_companies_fetch_status on public.companies using btree (fetch_detail_queued) TABLESPACE pg_default
where
  (fetch_detail_queued = true);

create index IF not exists idx_companies_form_found_stats on public.companies using btree (form_found) TABLESPACE pg_default
where
  (company_url is not null);

create index IF not exists idx_companies_url_id_stats on public.companies using btree (company_url, id) TABLESPACE pg_default
where
  (company_url is not null);

create index IF not exists idx_companies_has_instruction_json on public.companies using btree (id) TABLESPACE pg_default
where
  (instruction_json is not null);

create index IF not exists idx_companies_form_found on public.companies using btree (form_found) TABLESPACE pg_default;

create index IF not exists idx_companies_prohibition_detected on public.companies using btree (prohibition_detected) TABLESPACE pg_default;

create index IF not exists idx_companies_form_sender_basic on public.companies using btree (
  id,
  form_url,
  instruction_valid,
  bot_protection_detected
) TABLESPACE pg_default
where
  (
    (form_url is not null)
    and (instruction_json is not null)
  );

create index IF not exists idx_companies_instruction_exists on public.companies using btree (id) TABLESPACE pg_default
where
  (instruction_json is not null);

create index IF not exists idx_companies_no_instruction_json on public.companies using btree (id) TABLESPACE pg_default
where
  (instruction_json is null);

create index IF not exists idx_companies_has_form_url on public.companies using btree (form_url) TABLESPACE pg_default
where
  (form_url is not null);

create index IF not exists idx_companies_has_instruction on public.companies using btree (id) TABLESPACE pg_default
where
  (instruction_json is not null);

create index IF not exists idx_companies_ready_for_analysis on public.companies using btree (form_url, id) TABLESPACE pg_default
where
  (form_url is not null);

create index IF not exists idx_companies_needs_analysis on public.companies using btree (id) TABLESPACE pg_default
where
  (
    (form_url is not null)
    and (instruction_json is null)
  );

create index IF not exists idx_companies_targeting_base on public.companies using btree (id, form_url, instruction_valid) TABLESPACE pg_default
where
  (
    (form_url is not null)
    and (bot_protection_detected is null)
  );

create index IF not exists idx_companies_employee_count on public.companies using btree (employee_count) TABLESPACE pg_default
where
  (employee_count is not null);

create index IF not exists idx_companies_capital_established on public.companies using btree (capital, established_year) TABLESPACE pg_default
where
  (
    (capital is not null)
    and (established_year is not null)
  );

create index IF not exists idx_companies_company_name_text on public.companies using gin (to_tsvector('simple'::regconfig, company_name)) TABLESPACE pg_default
where
  (company_name is not null);

create index IF not exists idx_companies_valid_records_only on public.companies using btree (
  id,
  employee_count,
  capital,
  established_year,
  average_salary
) TABLESPACE pg_default
where
  (
    (form_url is not null)
    and (instruction_json is not null)
    and (
      (instruction_valid is null)
      or (instruction_valid = true)
    )
    and (bot_protection_detected is null)
  );

create index IF not exists idx_companies_postal_code on public.companies using btree (postal_code) TABLESPACE pg_default
where
  (postal_code is not null);
