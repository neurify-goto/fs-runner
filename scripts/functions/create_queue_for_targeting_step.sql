-- 部分投入用RPC: PostgRESTの短いstatement_timeoutを回避するため、
-- 企業IDの昇順で小さなチャンク単位にsend_queueへ投入する。
-- Stage1: 過去送信履歴なし（同targetingのsubmissionsが1件も無い）
-- Stage2: 成功履歴が無い（過去に試行はあるが success=true が一度も無い）かつ当日未送信
-- 両ステージで c.id > p_after_id の範囲に限定し、order by c.id asc limit p_limit で小さく実行時間を抑える。
create or replace function public.create_queue_for_targeting_step(
  p_target_date date,
  p_targeting_id bigint,
  p_targeting_sql text,
  p_ng_companies text,
  p_shards integer default 8,
  p_limit integer default 2000,
  p_after_id bigint default 0,
  p_stage smallint default 1, -- 1 or 2
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
begin
  -- 1回の呼び出しを短時間で終えるため、局所的にタイムアウトを短くしすぎない範囲で緩和
  perform set_config('statement_timeout', '120000', true);  -- 120s
  perform set_config('lock_timeout', '10000', true);        -- 10s

  -- NG社名
  if p_ng_companies is null or length(trim(p_ng_companies)) = 0 then
    v_ng_names := null;
  else
    v_ng_names := string_to_array(replace(replace(p_ng_companies, '，', ','), ' ', ''), ',')::text[];
  end if;

  -- shards
  v_shards := coalesce(p_shards, 8);
  if v_shards is null or v_shards <= 0 then v_shards := 8; end if;

  v_t0 := clock_timestamp();
  raise notice 'CQT_STEP:BEGIN stage=%, after_id=%, limit=%, id_window=%, shards=%, targeting_id=%, date=%', v_stage, p_after_id, p_limit, p_id_window, v_shards, p_targeting_id, p_target_date;

  if v_stage = 1 then
    -- Stage1: 未送信（履歴なし）
    v_sql :=
      'with candidates as (
         select c.id
           from public.companies c
           left join public.submissions s_hist
             on s_hist.targeting_id = $2 and s_hist.company_id = c.id
          where c.id > $6 and c.id <= ($6 + $7)
            and c.form_url is not null
            and coalesce(c.prohibition_detected, false) = false
            and c.duplication is null
            and s_hist.id is null';
    if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
      v_sql := v_sql || ' and (' || p_targeting_sql || ')';
    end if;
    v_sql := v_sql || ' and ( $3::text[] is null or array_length($3::text[],1) is null or not (c.company_name = any($3::text[])) )';
    v_sql := v_sql || ' order by c.id asc limit $5 ),
      bp as (
        select coalesce(max(priority), 0) as base
          from public.send_queue
         where target_date_jst = $1::date and targeting_id = $2
      ),
      cand_agg as (
        select count(*)::int as cand_count, coalesce(max(id), $6) as cand_max from candidates
      ),
      ins as (
        insert into public.send_queue(target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
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
    execute v_sql into v_inserted, v_last_id, v_has_more using p_target_date, p_targeting_id, v_ng_names, v_shards, p_limit, p_after_id, p_id_window;
    v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
    raise notice 'CQT_STEP:END stage=1 inserted=%, last_id=%, has_more=%, elapsed_ms=%', v_inserted, v_last_id, v_has_more, v_elapsed_ms::bigint;
    return query select v_inserted, v_last_id, v_has_more, v_stage;
  else
    -- Stage2: 成功履歴なし（当日未送信）
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
           from public.companies c
           left join public.submissions s_today
                  on s_today.targeting_id = $2
                 and s_today.company_id = c.id
                 and s_today.submitted_at >= ($1::timestamp AT TIME ZONE ''Asia/Tokyo'')
                 and s_today.submitted_at <  (($1::timestamp + interval ''1 day'') AT TIME ZONE ''Asia/Tokyo'')
           join hist h on h.company_id = c.id
         where c.id > $6 and c.id <= ($6 + $7)
            and c.form_url is not null
            and coalesce(c.prohibition_detected, false) = false
            and c.duplication is null
            and s_today.id is null
            and coalesce(h.has_success, false) = false';
    if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
      v_sql := v_sql || ' and (' || p_targeting_sql || ')';
    end if;
    v_sql := v_sql || ' and ( $3::text[] is null or array_length($3::text[],1) is null or not (c.company_name = any($3::text[])) )';
    v_sql := v_sql || ' order by c.id asc limit $5 ),
      bp as (
        select coalesce(max(priority), 0) as base
          from public.send_queue
         where target_date_jst = $1::date and targeting_id = $2
      ),
      cand_agg as (
        select count(*)::int as cand_count, coalesce(max(id), $6) as cand_max from candidates
      ),
      ins as (
        insert into public.send_queue(target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
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
    execute v_sql into v_inserted, v_last_id, v_has_more using p_target_date, p_targeting_id, v_ng_names, v_shards, p_limit, p_after_id, p_id_window;
    v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
    raise notice 'CQT_STEP:END stage=2 inserted=%, last_id=%, has_more=%, elapsed_ms=%', v_inserted, v_last_id, v_has_more, v_elapsed_ms::bigint;
    return query select v_inserted, v_last_id, v_has_more, v_stage;
  end if;
end;
$$;

grant execute on function public.create_queue_for_targeting_step(date, bigint, text, text, integer, integer, bigint, smallint, integer)
  to authenticated, service_role;
