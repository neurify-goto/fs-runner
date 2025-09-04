-- SupabaseのRPC関数定義
-- fetch-detailシステムのI/O負荷軽減のための最適化関数

-- 1. 統計取得の集約関数
-- 全体統計、完了統計、キューイング統計を一度に取得
CREATE OR REPLACE FUNCTION get_processing_stats()
RETURNS TABLE (
    total_count integer,
    completed_count integer,
    queued_count integer
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        COUNT(*)::integer AS total_count,
        COUNT(CASE WHEN company_url IS NOT NULL AND company_url != '' THEN 1 END)::integer AS completed_count,
        COUNT(CASE WHEN fetch_detail_queued = true THEN 1 END)::integer AS queued_count
    FROM companies;
END;
$$ LANGUAGE plpgsql;

-- 2. 原子的ステータス更新関数
-- detail_fetchedは廃止され、fetch_detail_queuedに統一されました
-- 旧関数は互換性のため残していますが、使用は推奨されません

-- 3. バッチ処理用の効率的なデータ取得と更新
-- データ取得と同時にステータスを更新する原子的操作
CREATE OR REPLACE FUNCTION get_and_lock_pending_batch(
    task_type text,
    batch_size integer DEFAULT 20
)
RETURNS TABLE (
    id bigint,
    company_name text,
    detail_page text
) AS $$
BEGIN
    -- トランザクション内でデータ取得とロック更新を同時実行
    RETURN QUERY
    WITH selected_rows AS (
        SELECT c.id, c.company_name, c.detail_page
        FROM companies c
        WHERE c.company_url IS NULL 
        AND c.fetch_detail_queued IS NULL
        ORDER BY c.id ASC  -- created_atの代わりにidで並び順指定
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED  -- 並行処理時の競合回避
    ),
    updated_rows AS (
        UPDATE companies
        SET fetch_detail_queued = true
        WHERE id IN (SELECT selected_rows.id FROM selected_rows)
        RETURNING companies.id
    )
    SELECT sr.id, sr.company_name, sr.detail_page
    FROM selected_rows sr
    INNER JOIN updated_rows ur ON sr.id = ur.id;
END;
$$ LANGUAGE plpgsql;

-- 4. バッチ結果の効率的な一括更新
-- 複数企業の詳細情報を一括で更新（エラーハンドリング強化版）
CREATE OR REPLACE FUNCTION batch_update_company_details(
    updates jsonb
)
RETURNS TABLE (
    success boolean,
    processed_count integer,
    error_count integer,
    error_message text
) AS $$
DECLARE
    update_count integer := 0;
    error_count integer := 0;
    record jsonb;
    record_id bigint;
    error_details text := '';
BEGIN
    -- JSONB配列内の各レコードを処理
    FOR record IN SELECT * FROM jsonb_array_elements(updates)
    LOOP
        BEGIN
            record_id := (record->>'record_id')::bigint;
            
            UPDATE companies 
            SET 
                company_url = COALESCE((record->>'company_url')::text, company_url),
                representative = COALESCE((record->>'representative')::text, representative),
                capital = COALESCE((record->>'capital')::bigint, capital),
                employee_count = COALESCE((record->>'employee_count')::bigint, employee_count),
                postal_code = COALESCE((record->>'postal_code')::text, postal_code),
                tel = COALESCE((record->>'tel')::text, tel),
                established_year = COALESCE((record->>'established_year')::integer, established_year),
                established_month = COALESCE((record->>'established_month')::smallint, established_month),
                closing_month = COALESCE((record->>'closing_month')::smallint, closing_month),
                average_age = COALESCE((record->>'average_age')::real, average_age),
                average_salary = COALESCE((record->>'average_salary')::bigint, average_salary),
                fetch_detail_queued = null  -- 処理完了でキューステータスをリセット
            WHERE id = record_id;
            
            IF FOUND THEN
                update_count := update_count + 1;
            ELSE
                error_count := error_count + 1;
                error_details := error_details || 'Record not found: ' || record_id || '; ';
            END IF;
            
        EXCEPTION
            WHEN OTHERS THEN
                -- 個別レコードのエラーをログに記録
                error_count := error_count + 1;
                error_details := error_details || 'Error on record ' || COALESCE(record_id::text, 'unknown') || ': ' || SQLERRM || '; ';
                
                -- エラーログをPostgreSQLログに出力
                RAISE WARNING 'batch_update_company_details: Error processing record_id=%, error=%', 
                    COALESCE(record_id, -1), SQLERRM;
                
                CONTINUE;
        END;
    END LOOP;
    
    -- 最終結果を返す
    RETURN QUERY SELECT 
        true, 
        update_count, 
        error_count, 
        CASE 
            WHEN error_details = '' THEN 'All records processed successfully'::text
            ELSE SUBSTRING(error_details, 1, 500)  -- エラー詳細を最大500文字に制限
        END;
    
EXCEPTION
    WHEN OTHERS THEN
        -- 致命的エラー時の処理
        RAISE WARNING 'batch_update_company_details: Fatal error=%', SQLERRM;
        RETURN QUERY SELECT false, 0, 0, ('Fatal error: ' || SQLERRM)::text;
END;
$$ LANGUAGE plpgsql;

-- 5. インデックス最適化のための推奨インデックス
-- 必要に応じて以下のインデックスを作成（既存の場合はスキップ）

-- 処理状態による検索を最適化
CREATE INDEX IF NOT EXISTS idx_companies_processing_status 
ON companies (company_url, fetch_detail_queued, id) 
WHERE company_url IS NULL AND fetch_detail_queued IS NULL;

-- 統計取得を最適化
CREATE INDEX IF NOT EXISTS idx_companies_completion_status 
ON companies (company_url) 
WHERE company_url IS NOT NULL AND company_url != '';

CREATE INDEX IF NOT EXISTS idx_companies_fetch_status 
ON companies (fetch_detail_queued) 
WHERE fetch_detail_queued = true;

-- Fetch Detail用RPC関数群
-- 成功結果の詳細データ一括更新RPC関数（全取得データ対応版）
CREATE OR REPLACE FUNCTION bulk_update_fetch_detail_success_with_data(
    success_data JSONB[]
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER := 0;
    record_data JSONB;
    record_id BIGINT;
BEGIN
    -- JSONB配列内の各成功レコードを処理
    FOREACH record_data IN ARRAY success_data
    LOOP
        BEGIN
            record_id := (record_data->>'record_id')::BIGINT;
            
            -- 取得した全詳細データを更新
            UPDATE companies 
            SET 
                company_url = COALESCE(record_data->>'company_url', company_url),
                representative = COALESCE(record_data->>'representative', representative),
                capital = CASE 
                    WHEN record_data->>'capital' IS NOT NULL AND record_data->>'capital' != '' 
                    THEN (record_data->>'capital')::BIGINT 
                    ELSE capital 
                END,
                employee_count = CASE 
                    WHEN record_data->>'employee_count' IS NOT NULL AND record_data->>'employee_count' != '' 
                    THEN (record_data->>'employee_count')::BIGINT 
                    ELSE employee_count 
                END,
                postal_code = COALESCE(record_data->>'postal_code', postal_code),
                tel = COALESCE(record_data->>'tel', tel),
                established_year = CASE 
                    WHEN record_data->>'established_year' IS NOT NULL AND record_data->>'established_year' != '' 
                    THEN (record_data->>'established_year')::INTEGER 
                    ELSE established_year 
                END,
                established_month = CASE 
                    WHEN record_data->>'established_month' IS NOT NULL AND record_data->>'established_month' != '' 
                    THEN (record_data->>'established_month')::SMALLINT 
                    ELSE established_month 
                END,
                closing_month = CASE 
                    WHEN record_data->>'closing_month' IS NOT NULL AND record_data->>'closing_month' != '' 
                    THEN (record_data->>'closing_month')::SMALLINT 
                    ELSE closing_month 
                END,
                average_age = CASE 
                    WHEN record_data->>'average_age' IS NOT NULL AND record_data->>'average_age' != '' 
                    THEN (record_data->>'average_age')::REAL 
                    ELSE average_age 
                END,
                average_salary = CASE 
                    WHEN record_data->>'average_salary' IS NOT NULL AND record_data->>'average_salary' != '' 
                    THEN (record_data->>'average_salary')::BIGINT 
                    ELSE average_salary 
                END
                -- 成功時はfetch_detail_queuedを変更せず、現在の状態を維持
            WHERE id = record_id;
            
            IF FOUND THEN
                updated_count := updated_count + 1;
            END IF;
            
        EXCEPTION
            WHEN OTHERS THEN
                -- 個別レコードのエラーをログに記録
                RAISE WARNING 'bulk_update_fetch_detail_success_with_data: Error processing record_id=%, error=%', 
                    COALESCE(record_id, -1), SQLERRM;
                CONTINUE;
        END;
    END LOOP;
    
    -- 結果を返す
    RETURN updated_count;
END;
$$;

-- 旧関数は互換性のため残すが、問題のある処理を修正
CREATE OR REPLACE FUNCTION bulk_update_fetch_detail_success(
    record_ids BIGINT[]
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    -- 一括更新実行（問題のあったcompany_name代入を削除）
    -- 処理完了時にfetch_detail_queuedをnullにリセットのみ実行
    UPDATE companies 
    SET 
        fetch_detail_queued = null   -- 処理完了でキューステータスをリセット
    WHERE id = ANY(record_ids);
    
    -- 更新件数を取得
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    
    -- 結果を返す
    RETURN updated_count;
END;
$$;

-- 失敗結果の一括更新RPC関数  
CREATE OR REPLACE FUNCTION bulk_update_fetch_detail_failure(
    record_ids BIGINT[]
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    -- 一括更新実行
    -- 処理完了時にfetch_detail_queuedをnullにリセット
    UPDATE companies 
    SET 
        fetch_detail_queued = null  -- 処理完了でキューステータスをリセット
        -- 失敗時はcompany_urlはnullのまま
    WHERE id = ANY(record_ids);
    
    -- 更新件数を取得
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    
    -- 結果を返す
    RETURN updated_count;
END;
$$;

-- 早期終了時の未処理レコードリセット用RPC関数
CREATE OR REPLACE FUNCTION reset_unprocessed_fetch_detail_queue(
    processed_record_ids BIGINT[]
)
RETURNS TABLE (
    reset_count INTEGER,
    processed_records INTEGER,
    error_message TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
    reset_count INTEGER := 0;
    processed_count INTEGER := 0;
BEGIN
    -- 処理済みレコード数を計算
    processed_count := array_length(processed_record_ids, 1);
    IF processed_count IS NULL THEN
        processed_count := 0;
    END IF;
    
    -- 未処理レコード（fetch_detail_queued=trueだが、処理済みリストに含まれない）をリセット
    UPDATE companies 
    SET fetch_detail_queued = null
    WHERE fetch_detail_queued = true 
    AND (processed_count = 0 OR id != ALL(processed_record_ids));
    
    -- リセット件数を取得
    GET DIAGNOSTICS reset_count = ROW_COUNT;
    
    -- 成功結果を返す
    RETURN QUERY SELECT 
        reset_count,
        processed_count,
        'Success'::TEXT;
    
EXCEPTION
    WHEN OTHERS THEN
        -- エラー時の処理
        RAISE WARNING 'reset_unprocessed_fetch_detail_queue: Error=%', SQLERRM;
        RETURN QUERY SELECT 
            0,
            processed_count,
            ('Error: ' || SQLERRM)::TEXT;
END;
$$;

-- 関数の権限設定（セキュリティ強化: anonユーザーのアクセス制限）
GRANT EXECUTE ON FUNCTION get_processing_stats() TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION get_and_lock_pending_batch(text, integer) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION batch_update_company_details(jsonb) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION bulk_update_fetch_detail_success_with_data(JSONB[]) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION bulk_update_fetch_detail_success(BIGINT[]) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION bulk_update_fetch_detail_failure(BIGINT[]) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION reset_unprocessed_fetch_detail_queue(BIGINT[]) TO authenticated, service_role;

-- 実行例とテスト用クエリ
/*
-- 統計取得テスト
SELECT * FROM get_processing_stats();

-- Fetch Detail成功結果更新テスト  
SELECT * FROM bulk_update_fetch_detail_success(ARRAY[1,2,3]::bigint[]);

-- 早期終了時のリセット処理テスト
SELECT * FROM reset_unprocessed_fetch_detail_queue(ARRAY[1,2,3]::bigint[]);

-- バッチ取得テスト
SELECT * FROM get_and_lock_pending_batch('fuma_detail', 5);

-- バッチ更新テスト（エラーハンドリング強化版）
SELECT * FROM batch_update_company_details('[
    {"record_id": 1, "company_url": "https://example.com", "representative": "田中太郎"},
    {"record_id": 2, "company_url": "https://example2.com", "capital": 10000000}
]'::jsonb);
*/