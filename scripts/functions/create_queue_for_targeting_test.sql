-- 当日分キュー投入（GAS から targeting_sql / ng_companies を受け取る）
-- p_ng_companies は「社名」のカンマ区切り（空文字可、半角カンマ推奨。全角カンマも許容）
-- 注意: キュー作成上限はターゲット毎に一律10000件。
--       p_max_daily は互換性のため残置するが、上限には使用しない。
create or replace function public.create_queue_for_targeting_test(
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
  v_ng_names text[];
  v_limit integer := 10000; -- 一律上限（最大投入件数）
  v_need integer := 0;      -- 追加で投入すべき不足分
  v_current_total integer := 0;           -- 1段目投入後の現在総数（当日/同targeting）
  v_total_final integer := 0;             -- 最終総件数
  v_base_priority integer := 0;           -- 2段目付与用の基準priority
  v_shards integer := 8;                  -- shardsの検証/補正後の値
  v_t0 timestamp with time zone;          -- 計測用: 関数全体開始
  v_t1 timestamp with time zone;          -- 計測用: 各ステージ開始
  v_elapsed_ms numeric;                   -- 計測用: 経過ミリ秒
begin
  -- 実行時間が長めになるケース（複合条件 + 上限1万件）に備えて、局所的に statement_timeout を緩和
  -- 既存の 60 秒設定では intermittently timeout が発生していたため、当関数内のみ 180 秒へ延長
  -- 備考: GAS 側の UrlFetch 実行上限やワークフローフローを考慮し、過度に長くしすぎないバランス値
  perform set_config('statement_timeout', '180000', true);  -- milliseconds (3 minutes)
  -- 競合ロックによる待ちを短くするために lock_timeout も軽く設定（待ちすぎで statement_timeout に達しないように）
  perform set_config('lock_timeout', '10000', true);        -- milliseconds (10 seconds)
  -- NOTICE 出力レベルの変更は Supabase では権限不足になるため実施しない

  -- デバッグ計測用タイムスタンプ
  v_t0 := clock_timestamp();
  raise notice 'CQT:BEGIN date=%, targeting_id=%, shards=%', p_target_date, p_targeting_id, coalesce(p_shards, 8);

  -- 追加バリデーション: targeting_sql の危険断片を簡易拒否（防御的チェック）
  -- 備考: GAS側でもサニタイズ済みだが、サーバ側にも二重の防御を置く
  if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
    -- 危険キーワード/記号を禁止（大文字小文字無視）。最初にヒットしたパターンを抽出して例外メッセージに含める
    declare
      v_forbidden_token text;
      v_forbidden_stmt  text;
    begin
      select (regexp_matches(trim(p_targeting_sql), '(;|--|/\*|\*/|\\x00)', 'i'))[1]
        into v_forbidden_token;
      if v_forbidden_token is not null then
        raise exception 'Invalid targeting_sql contains forbidden token: %', v_forbidden_token;
      end if;

      -- 危険なステートメント単語（単語境界で判定）\m/\M はARE
      select (regexp_matches(trim(p_targeting_sql), '(\m(insert|update|delete|drop|alter|create|grant|revoke|truncate|commit|rollback|with|union)\M)', 'i'))[1]
        into v_forbidden_stmt;
      if v_forbidden_stmt is not null then
        raise exception 'Invalid targeting_sql contains forbidden statement: %', v_forbidden_stmt;
      end if;
    end;
  end if;
  -- NG社名リストを配列化（全角カンマを半角へ、空白除去）
  if p_ng_companies is null or length(trim(p_ng_companies)) = 0 then
    v_ng_names := null;
  else
    v_ng_names := string_to_array(replace(replace(p_ng_companies, '，', ','), ' ', ''), ',')::text[];
  end if;
  raise notice 'CQT:PARAMS targeting_sql_len=%, ng_names_len=%', length(coalesce(p_targeting_sql,'')), coalesce(array_length(v_ng_names,1),0);

  -- shards パラメータの検証/補正（0以下/NULLは既定値8へ）
  v_shards := coalesce(p_shards, 8);
  if v_shards is null or v_shards <= 0 then
    v_shards := 8;
  end if;

  -- Stage1:『過去送信履歴なし（同targetingで submissions が1件も無い）』の新規候補のみ
  v_sql :=
    'with candidates as (
       select c.id
       from public.companies c
       left join public.submissions_test s_hist
         on s_hist.targeting_id = $2
        and s_hist.company_id = c.id
       where c.form_url is not null
         and c.black is null
         and coalesce(c.prohibition_detected, false) = false
         and c.duplication is null
         and s_hist.id is null';

  -- targeting_sql は事前にGAS側でサニタイズ・整形済みを前提（WHERE句断片）
  if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
    v_sql := v_sql || ' and (' || p_targeting_sql || ')';
  end if;

  -- NG社名除外（配列が空/NULLならスキップ）
  v_sql := v_sql || ' and ( $3::text[] is null or array_length($3::text[],1) is null or not (c.company_name = any($3::text[])) )';

  -- 追加の名称ポリシー除外: 次の語を含む企業名は対象外
  --   「医療法人」「病院」「法律事務所」「弁護士」「税理士」「弁理士」「学校」
  -- 要件: send_queue 作成段階で除外し、後続処理に乗せない
  v_sql := v_sql || ' and (c.company_name not like ''%医療法人%''
                             and c.company_name not like ''%病院%''
                             and c.company_name not like ''%法律事務所%''
                             and c.company_name not like ''%弁護士%''
                             and c.company_name not like ''%税理士%''
                             and c.company_name not like ''%弁理士%''
                             and c.company_name not like ''%学校%'')';

  v_sql := v_sql || ' order by c.id asc limit $4 )
    insert into public.send_queue_test(
      target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
    select $1::date, $2::bigint, id,
           row_number() over (order by id),
           (id % $5),
           ''pending'', 0, now()
    from candidates
    on conflict (target_date_jst, targeting_id, company_id) do nothing;';

  -- p_max_daily は無視し、常に v_limit=10000 を使用
  v_t1 := clock_timestamp();
  execute v_sql using p_target_date, p_targeting_id, v_ng_names, v_limit, v_shards;
  get diagnostics v_ins = row_count;
  v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
  raise notice 'CQT:STAGE1 inserted=% rows in % ms', v_ins, v_elapsed_ms::bigint;

  -- 1段目投入後の現在総数を計測
  select count(*) into v_current_total
    from public.send_queue_test
   where target_date_jst = p_target_date
     and targeting_id    = p_targeting_id;

  -- 追加要件(改): 総数が上限(10000)に満たない場合のみ、
  -- 「過去に送信試行はあるが成功履歴がないもの」で不足分を補充。
  -- さらに、直近2週間（JST基準）に送信試行(submissions)がある企業は除外する。
  if coalesce(v_current_total, 0) < v_limit then
    raise notice 'CQT:STAGE2 need=% (current_total=%, limit=%)', (v_limit - coalesce(v_current_total,0)), coalesce(v_current_total,0), v_limit;
    v_need := v_limit - coalesce(v_current_total, 0);

    v_sql :=
      'with hist as (
         select company_id,
                bool_or(success = true) as has_success
           from public.submissions_test
          where targeting_id = $2
          group by company_id
       ),
       candidates as (
         select c.id
           from public.companies c
           left join public.submissions_test s_today
                  on s_today.targeting_id = $2
                 and s_today.company_id = c.id
                 and s_today.submitted_at >= ($1::timestamp AT TIME ZONE ''Asia/Tokyo'')
                 and s_today.submitted_at <  (($1::timestamp + interval ''1 day'') AT TIME ZONE ''Asia/Tokyo'')
           -- 直近2週間(JST)に送信試行があるものは除外
           left join public.submissions_test s_recent14
                  on s_recent14.targeting_id = $2
                 and s_recent14.company_id = c.id
                 and s_recent14.submitted_at >= (($1::timestamp - interval ''14 days'') AT TIME ZONE ''Asia/Tokyo'')
                 and s_recent14.submitted_at <  (($1::timestamp) AT TIME ZONE ''Asia/Tokyo'')
           join hist h
             on h.company_id = c.id
          where c.form_url is not null
            and c.black is null
            and coalesce(c.prohibition_detected, false) = false
            and c.duplication is null
            and s_today.id is null
            and s_recent14.id is null
            and coalesce(h.has_success, false) = false';

    -- targeting_sql を同様に適用
    if p_targeting_sql is not null and length(trim(p_targeting_sql)) > 0 then
      v_sql := v_sql || ' and (' || p_targeting_sql || ')';
    end if;

    -- NG社名除外（配列が空/NULLならスキップ）
    v_sql := v_sql || ' and ( $3::text[] is null or array_length($3::text[],1) is null or not (c.company_name = any($3::text[])) )';

    -- 追加の名称ポリシー除外（Stage2側も同様）
    v_sql := v_sql || ' and (c.company_name not like ''%医療法人%''
                               and c.company_name not like ''%病院%''
                               and c.company_name not like ''%法律事務所%''
                               and c.company_name not like ''%弁護士%''
                               and c.company_name not like ''%税理士%''
                               and c.company_name not like ''%弁理士%''
                               and c.company_name not like ''%学校%'')';

    v_sql := v_sql || ' order by c.id asc limit $4 )
      insert into public.send_queue_test(
        target_date_jst, targeting_id, company_id, priority, shard_id, status, attempts, created_at)
      select $1::date, $2::bigint, id,
             $6 + row_number() over (order by id),
             (id % $5),
             ''pending'', 0, now()
        from candidates
      on conflict (target_date_jst, targeting_id, company_id) do nothing;';

    -- 2段目の基準priorityを一度だけ取得（現在総数ベースの最大値）
    select coalesce(max(priority), 0) into v_base_priority
      from public.send_queue_test
     where target_date_jst = p_target_date
       and targeting_id    = p_targeting_id;

    v_t1 := clock_timestamp();
    execute v_sql using p_target_date, p_targeting_id, v_ng_names, v_need, v_shards, v_base_priority;
    get diagnostics v_ins2 = row_count;
    v_ins := coalesce(v_ins,0) + coalesce(v_ins2,0);
    v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t1)) * 1000;
    raise notice 'CQT:STAGE2 inserted=% rows in % ms', v_ins2, v_elapsed_ms::bigint;
  end if;

  -- 最終総件数を取得
  select count(*) into v_total_final
    from public.send_queue_test
   where target_date_jst = p_target_date
     and targeting_id    = p_targeting_id;

  -- 合計時間を先に算出し、NOTICEに埋め込む
  v_elapsed_ms := extract(epoch from (clock_timestamp() - v_t0)) * 1000;
  -- 観測用 NOTICE（件数内訳 + 総時間）
  raise notice 'CQT:END date=%, targeting_id=%, current_total_before_stage2=%, stage1_inserted=%, stage2_inserted=%, total_final=%, total_ms=%',
    p_target_date, p_targeting_id, v_current_total, (v_ins - coalesce(v_ins2,0)), coalesce(v_ins2,0), v_total_final, v_elapsed_ms::bigint;

  return v_ins;
end;
$$;

grant execute on function public.create_queue_for_targeting_test(date,bigint,text,text,integer,integer)
  to authenticated, service_role;
