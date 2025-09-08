-- companies統計取得用のRPC関数定義
-- gas/stats システムでのgetCompaniesStats()処理の最適化
-- 7つの個別クエリを1つのRPC関数に統合してSupabase負荷軽減

-- 1. 全統計を一括取得するメイン関数
-- 現在のgetCompaniesStats()の7つのクエリを1つに統合
CREATE OR REPLACE FUNCTION get_companies_stats_all()
RETURNS TABLE (
    total_count bigint,
    with_company_url_count bigint,
    form_not_explored_count bigint,
    with_form_url_count bigint,
    form_not_analyzed_count bigint,
    form_analyzed_count bigint,
    valid_instruction_count bigint
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        -- 1. 全企業数
        COUNT(*)::bigint AS total_count,
        
        -- 2. 企業URLあり
        COUNT(CASE WHEN c.company_url IS NOT NULL THEN 1 END)::bigint AS with_company_url_count,
        
        -- 3. フォーム未探索（企業URLあり かつ form_foundがnull かつ duplication is null）
        COUNT(CASE WHEN c.company_url IS NOT NULL AND c.form_found IS NULL AND c.duplication IS NULL THEN 1 END)::bigint AS form_not_explored_count,
        
        -- 4. フォームURLあり（duplication is null）
        COUNT(CASE WHEN c.form_url IS NOT NULL AND c.duplication IS NULL THEN 1 END)::bigint AS with_form_url_count,
        
        -- 5. フォーム未解析（フォームURLありかつinstruction_jsonがnullかつprohibition_detectedがtrueでない）
        COUNT(CASE WHEN c.form_url IS NOT NULL 
                  AND c.instruction_json IS NULL 
                  AND (c.prohibition_detected IS NULL OR c.prohibition_detected != true) 
              THEN 1 END)::bigint AS form_not_analyzed_count,
        
        -- 6. フォーム解析済（instruction_jsonあり）
        COUNT(CASE WHEN c.instruction_json IS NOT NULL THEN 1 END)::bigint AS form_analyzed_count,
        
        -- 7. 有効指示書JSONあり（instruction_jsonありかつ（instruction_validがnullまたはtrue））
        COUNT(CASE WHEN c.instruction_json IS NOT NULL 
                  AND (c.instruction_valid IS NULL OR c.instruction_valid = true)
              THEN 1 END)::bigint AS valid_instruction_count
                  
    FROM companies c;
END;
$$ LANGUAGE plpgsql;

-- パフォーマンス最適化のためのコメント
-- 既存の部分インデックスを活用して効率的にクエリを実行
-- - idx_companies_company_url_notnull: company_url IS NOT NULL 条件用
-- - idx_companies_has_form_url: form_url IS NOT NULL 条件用  
-- - idx_companies_has_instruction: instruction_json IS NOT NULL 条件用
-- - idx_companies_instruction_valid: instruction_valid 条件用
-- - idx_companies_prohibition_detected: prohibition_detected 条件用

-- 関数の権限設定（セキュリティ: 適切な権限のみ付与）
GRANT EXECUTE ON FUNCTION get_companies_stats_all() TO authenticated, service_role;

-- 実行例とテスト用クエリ
/*
-- 統計一括取得テスト
SELECT * FROM get_companies_stats_all();

-- パフォーマンステスト用: 実行計画の確認
EXPLAIN ANALYZE SELECT * FROM get_companies_stats_all();

-- 従来の個別クエリとの結果比較用
-- 1. 全企業数
SELECT COUNT(*) AS total_count FROM companies;

-- 2. 企業URLあり  
SELECT COUNT(*) AS with_company_url_count FROM companies WHERE company_url IS NOT NULL;

-- 3. フォーム未探索
SELECT COUNT(*) AS form_not_explored_count FROM companies 
WHERE company_url IS NOT NULL AND form_found IS NULL;

-- 4. フォームURLあり
SELECT COUNT(*) AS with_form_url_count FROM companies WHERE form_url IS NOT NULL;

-- 5. フォーム未解析
SELECT COUNT(*) AS form_not_analyzed_count FROM companies 
WHERE form_url IS NOT NULL 
AND instruction_json IS NULL 
AND (prohibition_detected IS NULL OR prohibition_detected != true);

-- 6. フォーム解析済
SELECT COUNT(*) AS form_analyzed_count FROM companies WHERE instruction_json IS NOT NULL;

-- 7. 有効指示書JSONあり
SELECT COUNT(*) AS valid_instruction_count FROM companies 
WHERE instruction_json IS NOT NULL 
AND (instruction_valid IS NULL OR instruction_valid = true);
*/
