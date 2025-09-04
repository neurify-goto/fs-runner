-- Form Finder用RPC関数群
-- Disk I/O負荷削減のための効率的な一括更新機能
-- 
-- レビュー対応による改善点:
-- 1. セキュリティ強化: JSONB参照の最適化
-- 2. パフォーマンス改善: ?演算子による効率的なキー存在チェック
-- 3. 可読性向上: CTE（Common Table Expression）の使用

-- 成功結果の一括更新RPC関数（整合性修正版）
CREATE OR REPLACE FUNCTION bulk_update_form_finder_success(
    record_ids BIGINT[],
    form_url_mapping JSONB
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    -- 整合性を保った一括更新実行
    -- form_urlが有効な場合のみform_found=trueに設定
    -- 処理完了時にform_finder_queuedをnullにリセット
    UPDATE companies 
    SET 
        form_found = CASE
            WHEN form_url_mapping ? id::text 
                 AND form_url_mapping->>id::text IS NOT NULL 
                 AND TRIM(form_url_mapping->>id::text) != '' 
                 AND (form_url_mapping->>id::text LIKE 'http://%' OR form_url_mapping->>id::text LIKE 'https://%')
                 AND LENGTH(form_url_mapping->>id::text) <= 2048
                 AND form_url_mapping->>id::text NOT LIKE '%about:%'
            THEN true   -- 有効なform_urlが存在する場合のみtrue
            ELSE false  -- 無効またはnullの場合はfalse
        END,
        form_url = CASE 
            WHEN form_url_mapping ? id::text 
                 AND form_url_mapping->>id::text IS NOT NULL 
                 AND TRIM(form_url_mapping->>id::text) != '' 
                 AND (form_url_mapping->>id::text LIKE 'http://%' OR form_url_mapping->>id::text LIKE 'https://%')
                 AND LENGTH(form_url_mapping->>id::text) <= 2048
                 AND form_url_mapping->>id::text NOT LIKE '%about:%'
            THEN form_url_mapping->>id::text  -- 有効なURLを設定
            ELSE NULL  -- 無効な場合はNULLに設定
        END,
        form_finder_queued = null  -- 処理完了でキューステータスをリセット
    WHERE id = ANY(record_ids);
    
    -- 更新件数を取得
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    
    -- 結果を返す
    RETURN updated_count;
END;
$$;

-- 失敗結果の一括更新RPC関数  
CREATE OR REPLACE FUNCTION bulk_update_form_finder_failure(
    record_ids BIGINT[]
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    -- 一括更新実行
    -- 処理完了時にform_finder_queuedをnullにリセット
    UPDATE companies 
    SET 
        form_found = false,
        form_url = NULL,
        form_finder_queued = null  -- 処理完了でキューステータスをリセット
    WHERE id = ANY(record_ids);
    
    -- 更新件数を取得
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    
    -- 結果を返す
    RETURN updated_count;
END;
$$;

-- 統計情報取得用の単一クエリRPC関数（CTE最適化版）
CREATE OR REPLACE FUNCTION get_form_finder_stats()
RETURNS TABLE (
    total_companies INTEGER,
    form_found_count INTEGER, 
    pending_count INTEGER,
    progress_rate NUMERIC(5,2)
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    -- CTE（Common Table Expression）で可読性と保守性を向上
    WITH company_stats AS (
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN form_found = true THEN 1 END) as found_count,
            COUNT(CASE WHEN form_found IS NULL THEN 1 END) as pending_count
        FROM companies
        WHERE company_url IS NOT NULL  -- 企業URL存在企業のみを対象
    )
    SELECT 
        total::INTEGER as total_companies,
        found_count::INTEGER as form_found_count,
        pending_count::INTEGER as pending_count,
        -- 安全な進捗率計算（ゼロ除算回避）
        CASE 
            WHEN total > 0 THEN 
                ROUND((found_count * 100.0 / total)::NUMERIC, 2)
            ELSE 0::NUMERIC
        END as progress_rate
    FROM company_stats;
END;
$$;

-- 関数の権限設定
GRANT EXECUTE ON FUNCTION bulk_update_form_finder_success(BIGINT[], JSONB) TO anon;
GRANT EXECUTE ON FUNCTION bulk_update_form_finder_failure(BIGINT[]) TO anon;  
GRANT EXECUTE ON FUNCTION get_form_finder_stats() TO anon;