-- 当日分キュー投入（companies_extra / send_queue_extra 用）
create or replace function public.create_queue_for_targeting_extra(
  p_target_date date,
  p_targeting_id bigint,
  p_targeting_sql text,
  p_ng_companies text,
  p_client_name text default null,
  p_max_daily integer default null,
  p_shards integer default 8
)
returns integer
language plpgsql
as $$
declare
  v_sql text;
  v_ins integer := 0;
  v_ins2 integer := 0;
  v_ng_names text[];
  v_limit integer := 10000;
  v_need integer := 0;
  v_current_total integer := 0;
  v_total_final integer := 0;
  v_base_priority integer := 0;
  v_shards integer := 8;
  v_t0 timestamp with time zone;
  v_t1 timestamp with time zone;
  v_elapsed_ms numeric;
  v_client_name text;
begin
  perform set_config('statement_timeout', '180000', true);
  perform set_config('lock_timeout', '10000', true);

  v_t0 := clock_timestamp();
  raise notice 'CQT_EXTRA:BEGIN date=%, targeting_id=%, shards=%', p_target_date, p_targeting_id, coalesce(p_shards, 8);

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

  if p_ng_companies is null or length(trim(p_ng_companies)) = 0 then
    v_ng_names := null;
  else
    v_ng_names := string_to_array(replace(replace(p_ng_companies, '，', ','), ' ', ''), ',')::text[];
  end if;
  raise notice 'CQT_EXTRA:PARAMS targeting_sql_len=%, ng_names_len=%', length(coalesce(p_targeting_sql,'')), coalesce(array_length(v_ng_names,1),0);

  v_shards := coalesce(p_shards, 8);
  if v_shards is null or v_shards <= 0 then v_shards := 8; end if;

  v_client_name := nullif(btrim(coalesce(p_client_name, '')), '');
  if v_client_name is null then
    raise exception 'p_client_name is required for create_queue_for_targeting_extra';
  end if;

  -- Stage1
  v_sql :=
    'with candidates as (
       select c.id
      from public.companies_extra c
      left join public.submissions s_hist
        on s_hist.targeting_id = $2
       and s_hist.company_id = c.id
      left join public.submissions s_fail_recent
        on s_fail_recent.company_id = c.id
       and s_fail_recent.submitted_at >= (($1::timestamp - interval ''30 days'') AT TIME ZONE ''Asia/Tokyo'')
       and s_fail_recent.submitted_at <  (($1::timestamp + interval ''1 day'') AT TIME ZONE ''Asia/Tokyo'')
       and coalesce(s_fail_recent.success, false) = false
      where c.form_url is not null
        and c.black is null
        and coalesce(c.prohibition_detected, false) = false
        and c.duplication is null
        and s_hist.id is null
        and s_fail_recent.id is null';
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
  v_sql := v_sql || ' and (c.client = $6)';
  v_sql := v_sql || ' order by c.id asc limit $4 )
    insert into public.send_queue_extra(
      target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
    select $1::date, $2::bigint, id,
           row_number() over (order by id),
           (id % $5),
           ''pending'', 0, now()
      from candidates
    on conflict (target_date_jst, targeting_id, company_id) do nothing;';
  v_t1 := clock_timestamp();
  execute v_sql using p_target_date, p_targeting_id, v_ng_names, v_limit, v_shards, v_client_name;
  get diagnostics v_ins = row_count;
  v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
  raise notice 'CQT_EXTRA:STAGE1 inserted=% rows in % ms', v_ins, v_elapsed_ms::bigint;

  select count(*) into v_current_total
    from public.send_queue_extra
   where target_date_jst = p_target_date
     and targeting_id    = p_targeting_id;

  if coalesce(v_current_total, 0) < v_limit then
    raise notice 'CQT_EXTRA:STAGE2 need=% (current_total=%, limit=%)', (v_limit - coalesce(v_current_total,0)), coalesce(v_current_total,0), v_limit;
    v_need := v_limit - coalesce(v_current_total, 0);
    v_sql :=
      'with hist as (
         select company_id,
                bool_or(success = true) as has_success
           from public.submissions
          where targeting_id = $2
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
           left join public.submissions s_fail_recent_all
                  on s_fail_recent_all.company_id = c.id
                 and s_fail_recent_all.submitted_at >= (($1::timestamp - interval ''30 days'') AT TIME ZONE ''Asia/Tokyo'')
                 and s_fail_recent_all.submitted_at <  (($1::timestamp + interval ''1 day'') AT TIME ZONE ''Asia/Tokyo'')
                 and coalesce(s_fail_recent_all.success, false) = false
           join hist h on h.company_id = c.id
          where c.form_url is not null
            and c.black is null
            and coalesce(c.prohibition_detected, false) = false
            and c.duplication is null
            and s_today.id is null
            and s_recent14.id is null
            and s_fail_recent_all.id is null
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
    v_sql := v_sql || ' and (c.client = $6)';
    v_sql := v_sql || ' order by c.id asc limit $4 )
      insert into public.send_queue_extra(
        target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
      select $1::date, $2::bigint, id,
             $7 + row_number() over (order by id),
             (id % $5),
             ''pending'', 0, now()
        from candidates
      on conflict (target_date_jst, targeting_id, company_id) do nothing;';

    select coalesce(max(priority), 0) into v_base_priority
      from public.send_queue_extra
     where target_date_jst = p_target_date
       and targeting_id    = p_targeting_id;

    v_t1 := clock_timestamp();
    execute v_sql using p_target_date, p_targeting_id, v_ng_names, v_need, v_shards, v_client_name, v_base_priority;
    get diagnostics v_ins2 = row_count;
    v_ins := coalesce(v_ins,0) + coalesce(v_ins2,0);
    v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
    raise notice 'CQT_EXTRA:STAGE2 inserted=% rows in % ms', v_ins2, v_elapsed_ms::bigint;
  end if;

  select count(*) into v_total_final
    from public.send_queue_extra
   where target_date_jst = p_target_date
     and targeting_id    = p_targeting_id;

  v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t0)) * 1000;
  raise notice 'CQT_EXTRA:END date=%, targeting_id=%, current_total_before_stage2=%, stage1_inserted=%, stage2_inserted=%, total_final=%, total_ms=%',
    p_target_date, p_targeting_id, v_current_total, (v_ins - coalesce(v_ins2,0)), coalesce(v_ins2,0), v_total_final, v_elapsed_ms::bigint;

  return v_ins;
end;
$$;

grant execute on function public.create_queue_for_targeting_extra(date,bigint,text,text,text,integer,integer)
  to authenticated, service_role;
