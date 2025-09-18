-- 部分投入用RPC（companies_extra / send_queue_extra 用）
create or replace function public.create_queue_for_targeting_step_extra(
  p_target_date date,
  p_targeting_id bigint,
  p_targeting_sql text,
  p_ng_companies text,
  p_client_name text default null,
  p_shards integer default 8,
  p_limit integer default 2000,
  p_after_id bigint default 0,
  p_stage smallint default 1,
  p_id_window integer default 50000
)
returns table(inserted integer, last_id bigint, has_more boolean, stage smallint)
language plpgsql
as $$
declare
  v_sql text;
  v_ng_names text[];
  v_shards integer := 8;
  v_inserted integer := 0;
  v_last_id bigint := coalesce(p_after_id, 0);
  v_has_more boolean := false;
  v_stage smallint := coalesce(p_stage, 1);
  v_t0 timestamp with time zone;
  v_t1 timestamp with time zone;
  v_elapsed_ms numeric;
  v_client_name text;
begin
  perform set_config('statement_timeout', '120000', true);
  perform set_config('lock_timeout', '10000', true);

  if p_ng_companies is null or length(trim(p_ng_companies)) = 0 then
    v_ng_names := null;
  else
    v_ng_names := string_to_array(replace(replace(p_ng_companies, '，', ','), ' ', ''), ',')::text[];
  end if;

  v_shards := coalesce(p_shards, 8);
  if v_shards is null or v_shards <= 0 then v_shards := 8; end if;

  v_client_name := nullif(btrim(coalesce(p_client_name, '')), '');
  if v_client_name is null then
    raise exception 'p_client_name is required for create_queue_for_targeting_step_extra';
  end if;

  v_t0 := clock_timestamp();
  raise notice 'CQT_STEP_EXTRA:BEGIN stage=%, after_id=%, limit=%, id_window=%, shards=%, targeting_id=%, date=%', v_stage, p_after_id, p_limit, p_id_window, v_shards, p_targeting_id, p_target_date;

  if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
    declare
      v_forbidden_token text;
      v_forbidden_stmt  text;
    begin
      select (regexp_matches(trim(p_targeting_sql), '(;|--|/\*|\*/|\\x00)', 'i'))[1]
        into v_forbidden_token;
      if v_forbidden_token is not null then
        raise exception 'Invalid targeting_sql contains forbidden token: %', v_forbidden_token;
      end if;
      select (regexp_matches(trim(p_targeting_sql), '(\m(insert|update|delete|drop|alter|create|grant|revoke|truncate|commit|rollback|with|union)\M)', 'i'))[1]
        into v_forbidden_stmt;
      if v_forbidden_stmt is not null then
        raise exception 'Invalid targeting_sql contains forbidden statement: %', v_forbidden_stmt;
      end if;
    end;
  end if;

  if v_stage = 1 then
    v_sql :=
      'with candidates as (
         select c.id
           from public.companies_extra c
           left join public.submissions s_hist
             on s_hist.targeting_id = $2 and s_hist.company_id = c.id
          where c.id > $6 and c.id <= ($6 + $7)
            and c.form_url is not null
            and c.black is null
            and coalesce(c.prohibition_detected, false) = false
            and c.duplication is null
            and s_hist.id is null';
    if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
      v_sql := v_sql || ' and (' || p_targeting_sql || ')';
    end if;
    v_sql := v_sql || ' and ( $3::text[] is null or array_length($3::text[],1) is null or not (c.company_name = any($3::text[])) )';
    v_sql := v_sql || ' and (c.company_name not like ''%医療法人%''
                               and c.company_name not like ''%病院%''
                               and c.company_name not like ''%法律事務所%''
                               and c.company_name not like ''%弁護士%''
                               and c.company_name not like ''%税理士%''
                               and c.company_name not like ''%弁理士%''
                               and c.company_name not like ''%学校%'')';
    v_sql := v_sql || ' and (c.client = $8)';
    v_sql := v_sql || ' and (c.client = $8)';
    v_sql := v_sql || ' and (c.client = $8)';
    v_sql := v_sql || ' and (c.client = $8)';
    v_sql := v_sql || ' order by c.id asc limit $5 ),
      bp as (
        select coalesce(max(priority), 0) as base
          from public.send_queue_extra
         where target_date_jst = $1::date and targeting_id = $2
      ),
      cand_agg as (
        select count(*)::int as cand_count, coalesce(max(id), $6) as cand_max from candidates
      ),
      ins as (
        insert into public.send_queue_extra(target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
        select $1::date, $2::bigint, id,
               (select base from bp) + row_number() over (order by id),
               (id % $4),
               ''pending'', 0, now()
          from candidates
        on conflict (target_date_jst, targeting_id, company_id) do nothing
        returning company_id
      ),
      ins_agg as (
        select count(*)::int as ins_count, coalesce(max(company_id), $6) as ins_max from ins
      )
      select ins_agg.ins_count as inserted,
             greatest(ins_agg.ins_max, cand_agg.cand_max) as last_id,
             (cand_agg.cand_count = $5 OR cand_agg.cand_max >= ($6 + $7)) as has_more
      from ins_agg, cand_agg;';
    v_t1 := clock_timestamp();
    execute v_sql into v_inserted, v_last_id, v_has_more using p_target_date, p_targeting_id, v_ng_names, v_shards, p_limit, p_after_id, p_id_window, v_client_name;
    v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
    raise notice 'CQT_STEP_EXTRA:END stage=1 inserted=%, last_id=%, has_more=%, elapsed_ms=%', v_inserted, v_last_id, v_has_more, v_elapsed_ms::bigint;
    return query select v_inserted, v_last_id, v_has_more, v_stage;
  else
    v_sql :=
      'with hist as (
         select company_id, bool_or(success = true) as has_success
           from public.submissions
          where targeting_id = $2
            and company_id > $6 and company_id <= ($6 + $7)
          group by company_id
       ),
       candidates as (
         select c.id
           from public.companies_extra c
           left join public.submissions s_today
                  on s_today.targeting_id = $2
                 and s_today.company_id = c.id
                 and s_today.submitted_at >= ($1::timestamp AT TIME ZONE ''Asia/Tokyo'')
                 and s_today.submitted_at <  (($1::timestamp + interval ''1 day'') AT TIME ZONE ''Asia/Tokyo'')
           left join public.submissions s_recent14
                  on s_recent14.targeting_id = $2
                 and s_recent14.company_id = c.id
                 and s_recent14.submitted_at >= (($1::timestamp - interval ''14 days'') AT TIME ZONE ''Asia/Tokyo'')
                 and s_recent14.submitted_at <  (($1::timestamp) AT TIME ZONE ''Asia/Tokyo'')
           join hist h on h.company_id = c.id
         where c.id > $6 and c.id <= ($6 + $7)
            and c.form_url is not null
            and c.black is null
            and coalesce(c.prohibition_detected, false) = false
            and c.duplication is null
            and s_today.id is null
            and s_recent14.id is null
            and coalesce(h.has_success, false) = false';
    if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
      v_sql := v_sql || ' and (' || p_targeting_sql || ')';
    end if;
    v_sql := v_sql || ' and ( $3::text[] is null or array_length($3::text[],1) is null or not (c.company_name = any($3::text[])) )';
    v_sql := v_sql || ' and (c.company_name not like ''%医療法人%''
                               and c.company_name not like ''%病院%''
                               and c.company_name not like ''%法律事務所%''
                               and c.company_name not like ''%弁護士%''
                               and c.company_name not like ''%税理士%''
                               and c.company_name not like ''%弁理士%''
                               and c.company_name not like ''%学校%'')';
    v_sql := v_sql || ' order by c.id asc limit $5 ),
      bp as (
        select coalesce(max(priority), 0) as base
          from public.send_queue_extra
         where target_date_jst = $1::date and targeting_id = $2
      ),
      cand_agg as (
        select count(*)::int as cand_count, coalesce(max(id), $6) as cand_max from candidates
      ),
      ins as (
        insert into public.send_queue_extra(target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
        select $1::date, $2::bigint, id,
               (select base from bp) + row_number() over (order by id),
               (id % $4),
               ''pending'', 0, now()
          from candidates
        on conflict (target_date_jst, targeting_id, company_id) do nothing
        returning company_id
      ),
      ins_agg as (
        select count(*)::int as ins_count, coalesce(max(company_id), $6) as ins_max from ins
      )
      select ins_agg.ins_count as inserted,
             greatest(ins_agg.ins_max, cand_agg.cand_max) as last_id,
             (cand_agg.cand_count = $5 OR cand_agg.cand_max >= ($6 + $7)) as has_more
      from ins_agg, cand_agg;';

    v_t1 := clock_timestamp();
    execute v_sql into v_inserted, v_last_id, v_has_more using p_target_date, p_targeting_id, v_ng_names, v_shards, p_limit, p_after_id, p_id_window, v_client_name;
    v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
    raise notice 'CQT_STEP_EXTRA:END stage=2 inserted=%, last_id=%, has_more=%, elapsed_ms=%', v_inserted, v_last_id, v_has_more, v_elapsed_ms::bigint;
    return query select v_inserted, v_last_id, v_has_more, v_stage;
  end if;
end;
$$;

grant execute on function public.create_queue_for_targeting_step_extra(date,bigint,text,text,text,integer,integer,bigint,smallint,integer)
  to authenticated, service_role;
