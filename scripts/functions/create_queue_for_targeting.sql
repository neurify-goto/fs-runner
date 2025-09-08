-- 当日分キュー投入（GAS から targeting_sql / ng_companies を受け取る）
-- p_ng_companies はカンマ区切り ID 群（空文字可）
-- 注意: キュー作成上限はターゲット毎に一律10000件。
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
  v_ins2 integer := 0;
  v_ng_ids bigint[];
  v_limit integer := 10000; -- 一律上限（最大投入件数）
  v_need integer := 0;      -- 追加で投入すべき不足分
  v_total integer := 0;     -- 合計投入件数（観測用）
begin
  -- 追加バリデーション: targeting_sql の危険断片を簡易拒否（防御的チェック）
  -- 備考: GAS側でもサニタイズ済みだが、サーバ側にも二重の防御を置く
  if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
    -- 危険キーワード/記号を禁止（大文字小文字無視）
    -- 1) 区切り記号/コメント/ヌルバイト
    if trim(p_targeting_sql) ~* '(;|--|/\*|\*/|\\x00)' then
      raise exception 'Invalid targeting_sql: contains forbidden tokens';
    end if;
    -- 2) 危険なステートメント単語（単語境界で判定）
    --    \m / \M は PostgreSQL の単語境界（ARE）
    if trim(p_targeting_sql) ~* '(\m(insert|update|delete|drop|alter|create|grant|revoke|truncate|commit|rollback|with|union)\M)' then
      raise exception 'Invalid targeting_sql: contains forbidden statements';
    end if;
  end if;
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
         on s.targeting_id = $2
        and s.company_id = c.id
        and s.submitted_at >= ($1::timestamp AT TIME ZONE ''Asia/Tokyo'')
        and s.submitted_at <  (($1::timestamp + interval ''1 day'') AT TIME ZONE ''Asia/Tokyo'')
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

  -- p_max_daily は無視し、常に v_limit=10000 を使用
  execute v_sql using p_target_date, p_targeting_id, v_ng_ids, v_limit, p_shards;
  get diagnostics v_ins = row_count;

  -- 追加要件(改): 1段目の投入件数が上限(10000)に満たない場合、
  -- 「過去に送信試行はあるが成功履歴がないもの」で不足分を最大10000件まで補充
  if coalesce(v_ins, 0) < v_limit then
    v_need := v_limit - coalesce(v_ins, 0);

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
           from public.companies c
           left join public.submissions s_today
                  on s_today.targeting_id = $2
                 and s_today.company_id = c.id
                 and s_today.submitted_at >= ($1::timestamp AT TIME ZONE ''Asia/Tokyo'')
                 and s_today.submitted_at <  (($1::timestamp + interval ''1 day'') AT TIME ZONE ''Asia/Tokyo'')
           join hist h
             on h.company_id = c.id
          where c.form_url is not null
            and coalesce(c.prohibition_detected, false) = false
            and s_today.id is null
            and coalesce(h.has_success, false) = false';

    -- targeting_sql を同様に適用
    if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
      v_sql := v_sql || ' and (' || p_targeting_sql || ')';
    end if;

    -- NG会社ID除外（配列が空/NULLならスキップ）
    v_sql := v_sql || ' and ( $3::bigint[] is null or array_length($3::bigint[],1) is null or not (c.id = any($3::bigint[])) )';

    v_sql := v_sql || ' order by c.id asc limit $4 )
      insert into public.send_queue(
        target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
      select $1::date, $2::bigint, id,
             (select coalesce(max(priority),0) from public.send_queue where target_date_jst = $1::date and targeting_id = $2::bigint) + row_number() over (order by id),
             (id % $5),
             ''pending'', 0, now()
        from candidates
      on conflict (target_date_jst, targeting_id, company_id) do nothing;';

    execute v_sql using p_target_date, p_targeting_id, v_ng_ids, v_need, p_shards;
    get diagnostics v_ins2 = row_count;
    v_ins := coalesce(v_ins,0) + coalesce(v_ins2,0);
  end if;

  -- 観測用: 合計0件であれば NOTICE（開発/検証用。実行環境での影響は極小）
  v_total := coalesce(v_ins, 0);
  if v_total = 0 then
    raise notice 'create_queue_for_targeting: no candidates for date=%, targeting_id=%', p_target_date, p_targeting_id;
  end if;

  return v_ins;
end;
$$;
