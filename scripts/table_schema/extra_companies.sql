create table public.extra_companies (
  id bigint not null,
  client text null,
  company_name text null,
  postal_code text null,
  location text null,
  prefecture text null,
  tel text null,
  company_url text null,
  constraint extra_companies_pkey primary key (id)
) TABLESPACE pg_default;