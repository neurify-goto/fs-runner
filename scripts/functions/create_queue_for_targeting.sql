-- 当日分キュー投入（GAS から targeting_sql / ng_companies を受け取る）
-- p_ng_companies はカンマ区切り ID 群（空文字可）
-- 注意: キュー作成上限はターゲット毎に一律5000件。
--       p_max_daily は互換性のため残置するが、上限には使用しない。
create or replace function public.create_queue_for_targeting(
  p_target_date date,
  p_targeting_id bigint,
  p_targeting_sql text,
  p_ng_companies text,
  p_max_daily integer,
  p_shards integer default 8
)
returns integer
language plpgsql
as $$
declare
  v_sql text;
  v_ins integer := 0;
  v_ng_ids bigint[];
  v_limit integer := 5000; -- 一律上限
begin
  -- NGリストを配列化
  if p_ng_companies is null or length(trim(p_ng_companies)) = 0 then
    v_ng_ids := null;
  else
    v_ng_ids := string_to_array(replace(p_ng_companies,' ','') , ',')::bigint[];
  end if;

  v_sql :=
    'with candidates as (
       select c.id
       from public.companies c
       left join public.submissions s
         on s.targeting_id = $2 and s.company_id = c.id and s.success = true
       where c.form_url is not null
         and coalesce(c.prohibition_detected, false) = false
         and s.id is null';

  -- targeting_sql は事前にGAS側でサニタイズ・整形済みを前提（WHERE句断片）
  if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
    v_sql := v_sql || ' and (' || p_targeting_sql || ')';
  end if;

  -- NG会社ID除外（配列が空/NULLならスキップ）
  v_sql := v_sql || ' and ( $3::bigint[] is null or array_length($3::bigint[],1) is null or not (c.id = any($3::bigint[])) )';

  v_sql := v_sql || ' order by c.id asc limit $4 )
    insert into public.send_queue(
      target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
    select $1::date, $2::bigint, id,
           row_number() over (order by id),
           (id % $5),
           ''pending'', 0, now()
    from candidates
    on conflict (target_date_jst, targeting_id, company_id) do nothing;';

  -- p_max_daily は無視し、常に v_limit=5000 を使用
  execute v_sql using p_target_date, p_targeting_id, v_ng_ids, v_limit, p_shards;
  get diagnostics v_ins = row_count;
  return v_ins;
end;
$$;
