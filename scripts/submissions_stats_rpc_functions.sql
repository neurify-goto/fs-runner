-- submissions統計取得用のRPC関数定義
-- gas/stats システムでのtargetingシート更新処理の最適化
-- 既存関数との競合を根本的に解決するため、完全に新しい関数名を使用

-- 既存関数を安全に削除（戻り値型変更のため）
DROP FUNCTION IF EXISTS get_targeting_submissions_stats_all(integer[]);
DROP FUNCTION IF EXISTS get_targeting_submissions_stats_today(integer[]);
DROP FUNCTION IF EXISTS get_jst_date_range();

-- 1. 通算統計取得（全期間）の一括関数
-- 完全に新しい関数名で既存関数との競合を回避
CREATE OR REPLACE FUNCTION get_targeting_submissions_stats_all(
    targeting_ids integer[]
)
RETURNS TABLE (
    targeting_id integer,
    total_count integer,
    success_count integer
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        s.targeting_id::integer,
        COUNT(*)::integer AS total_count,
        COUNT(CASE WHEN s.success = true THEN 1 END)::integer AS success_count
    FROM submissions s
    WHERE s.targeting_id = ANY(targeting_ids::bigint[])
    GROUP BY s.targeting_id
    ORDER BY s.targeting_id;
END;
$$ LANGUAGE plpgsql;

-- 2. 本日統計取得（UTC→JST変換対応）の一括関数
-- submitted_at（UTC）を日本時間（JST）に変換して本日（00:00:00〜23:59:59）でフィルタリング  
-- 完全に新しい関数名で既存関数との競合を回避
CREATE OR REPLACE FUNCTION get_targeting_submissions_stats_today(
    targeting_ids integer[]
)
RETURNS TABLE (
    targeting_id integer,
    total_count_today integer,
    success_count_today integer
) AS $$
DECLARE
    jst_today_start timestamptz;
    jst_today_end timestamptz;
BEGIN
    -- 日本時間（JST）での本日00:00:00から翌日00:00:00未満の範囲を計算
    -- submitted_atはUTCで保存されているため、JST基準の日付範囲をUTC時間で表現
    -- DST遷移時にも安定して動作するロジック
    jst_today_start := date_trunc('day', NOW() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo';
    jst_today_end := jst_today_start + INTERVAL '1 day';
    
    RETURN QUERY
    SELECT 
        s.targeting_id::integer,
        COUNT(*)::integer AS total_count_today,
        COUNT(CASE WHEN s.success = true THEN 1 END)::integer AS success_count_today
    FROM submissions s
    WHERE s.targeting_id = ANY(targeting_ids::bigint[])
    AND s.submitted_at >= jst_today_start
    AND s.submitted_at < jst_today_end
    GROUP BY s.targeting_id
    ORDER BY s.targeting_id;
END;
$$ LANGUAGE plpgsql;

-- 3. デバッグ用: 日本時間での日付範囲確認関数
-- JST変換が正しく動作しているかテスト用
CREATE OR REPLACE FUNCTION get_jst_date_range()
RETURNS TABLE (
    current_utc timestamptz,
    current_jst timestamptz,
    jst_today_start timestamptz,
    jst_today_end timestamptz,
    timezone_info text
) AS $$
DECLARE
    jst_start timestamptz;
    jst_end timestamptz;
    current_jst_time timestamptz;
BEGIN
    -- 実際に使用している変換ロジックと同じ方式でJST範囲を計算（DST対応版）
    jst_start := date_trunc('day', NOW() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo';
    jst_end := jst_start + INTERVAL '1 day';
    current_jst_time := NOW() AT TIME ZONE 'Asia/Tokyo';
    
    RETURN QUERY
    SELECT 
        NOW()::timestamptz AS current_utc,
        current_jst_time AS current_jst,
        jst_start AS jst_today_start,
        jst_end AS jst_today_end,
        ('JST today range: ' || jst_start::text || ' to ' || jst_end::text)::text AS timezone_info;
END;
$$ LANGUAGE plpgsql;

-- パフォーマンス最適化のための追加インデックス（必要に応じて）
-- 本日統計クエリ用の複合インデックス（targeting_id + submitted_at + success）
CREATE INDEX IF NOT EXISTS idx_submissions_targeting_submitted_success 
ON public.submissions USING btree (targeting_id, submitted_at, success) 
TABLESPACE pg_default;

-- 本日統計クエリ用のインデックス（submitted_at単体）
CREATE INDEX IF NOT EXISTS idx_submissions_submitted_at 
ON public.submissions USING btree (submitted_at) 
TABLESPACE pg_default;

-- 関数の権限設定（セキュリティ: 適切な権限のみ付与）
GRANT EXECUTE ON FUNCTION get_targeting_submissions_stats_all(integer[]) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION get_targeting_submissions_stats_today(integer[]) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION get_jst_date_range() TO authenticated, service_role;

-- 実行例とテスト用クエリ
/*
-- 通算統計取得テスト（新関数）
SELECT * FROM get_targeting_submissions_stats_all(ARRAY[1,2,3,4,5]::integer[]);

-- 本日統計取得テスト（UTC→JST変換）
SELECT * FROM get_targeting_submissions_stats_today(ARRAY[1,2,3,4,5]::integer[]);

-- 日本時間での日付範囲確認（デバッグ用）
SELECT * FROM get_jst_date_range();

-- 本日の全submissions確認（DST対応版ロジックでデバッグ用）
WITH jst_range AS (
    SELECT 
        date_trunc('day', NOW() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo' as start_time,
        date_trunc('day', NOW() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo' + INTERVAL '1 day' as end_time
)
SELECT targeting_id, submitted_at, success, 
       (submitted_at AT TIME ZONE 'Asia/Tokyo')::timestamp as submitted_jst
FROM submissions s, jst_range jr
WHERE s.submitted_at >= jr.start_time
AND s.submitted_at < jr.end_time
ORDER BY targeting_id, submitted_at;

-- submitted_atの最新データ確認（デバッグ用）
SELECT targeting_id, submitted_at, success,
       (submitted_at AT TIME ZONE 'Asia/Tokyo')::timestamp as submitted_jst
FROM submissions 
ORDER BY submitted_at DESC 
LIMIT 20;

-- パフォーマンステスト用: 大量データでの実行時間測定
EXPLAIN ANALYZE SELECT * FROM get_targeting_submissions_stats_today(ARRAY[1,2,3,4,5,6,7,8,9,10]::integer[]);
*/