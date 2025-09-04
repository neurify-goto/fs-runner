-- デバッグ用RPC関数 - targeting_sql機能の0件問題診断
-- RPC関数内で実際に生成・実行されるクエリを確認

-- デバッグ用メイン関数
CREATE OR REPLACE FUNCTION debug_get_target_companies_with_sql(
    targeting_sql TEXT,
    ng_companies TEXT,
    start_id BIGINT,
    limit_count INTEGER,
    exclude_ids BIGINT[]
)
RETURNS TABLE(
    step_name TEXT,
    debug_info TEXT,
    query_part TEXT,
    row_count BIGINT
) AS $$
DECLARE
    where_conditions TEXT[];
    final_where_clause TEXT;
    full_query TEXT;
    count_result BIGINT;
    safety_result RECORD;
    ng_pattern TEXT;
BEGIN
    -- Step 1: 基本テーブル行数確認
    EXECUTE 'SELECT COUNT(*) FROM companies' INTO count_result;
    RETURN QUERY SELECT 'step1_total'::TEXT, 'Total companies in table'::TEXT, 'SELECT COUNT(*) FROM companies'::TEXT, count_result;
    
    -- Step 2: 基本条件の段階的テスト
    where_conditions := ARRAY['form_url IS NOT NULL'];
    final_where_clause := array_to_string(where_conditions, ' AND ');
    full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
    EXECUTE full_query INTO count_result;
    RETURN QUERY SELECT 'step2_form_url'::TEXT, 'Companies with form_url'::TEXT, full_query, count_result;
    
    -- Step 3: instruction_json条件追加
    where_conditions := where_conditions || ARRAY['instruction_json IS NOT NULL'];
    final_where_clause := array_to_string(where_conditions, ' AND ');
    full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
    EXECUTE full_query INTO count_result;
    RETURN QUERY SELECT 'step3_instruction'::TEXT, 'Companies with instruction_json'::TEXT, full_query, count_result;
    
    -- Step 4: instruction_valid条件追加
    where_conditions := where_conditions || ARRAY['(instruction_valid IS NULL OR instruction_valid = true)'];
    final_where_clause := array_to_string(where_conditions, ' AND ');
    full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
    EXECUTE full_query INTO count_result;
    RETURN QUERY SELECT 'step4_valid'::TEXT, 'Companies with valid instruction'::TEXT, full_query, count_result;
    
    -- Step 5: bot_protection_detected条件追加（これが問題の可能性）
    where_conditions := where_conditions || ARRAY['bot_protection_detected IS NULL'];
    final_where_clause := array_to_string(where_conditions, ' AND ');
    full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
    EXECUTE full_query INTO count_result;
    RETURN QUERY SELECT 'step5_bot_protection'::TEXT, 'Companies without bot protection'::TEXT, full_query, count_result;
    
    -- Step 6: ID条件追加
    where_conditions := where_conditions || ARRAY['id >= ' || start_id];
    final_where_clause := array_to_string(where_conditions, ' AND ');
    full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
    EXECUTE full_query INTO count_result;
    RETURN QUERY SELECT 'step6_id_filter'::TEXT, format('Companies with ID >= %s', start_id), full_query, count_result;
    
    -- Step 7: targeting_sql条件の処理確認
    IF targeting_sql IS NOT NULL AND LENGTH(TRIM(targeting_sql)) > 0 THEN
        BEGIN
            -- バリデーション実行
            SELECT * INTO safety_result FROM validate_targeting_sql_safety(targeting_sql);
            RETURN QUERY SELECT 'step7a_validation'::TEXT, 
                format('Validation result: safe=%s, sanitized=%s, error=%s', 
                    safety_result.is_safe, 
                    COALESCE(safety_result.sanitized_sql, 'NULL'), 
                    COALESCE(safety_result.error_message, 'NULL')), 
                targeting_sql, 0::BIGINT;
                
            IF safety_result.is_safe THEN
                -- targeting_sql条件追加
                where_conditions := where_conditions || ARRAY['(' || safety_result.sanitized_sql || ')'];
                final_where_clause := array_to_string(where_conditions, ' AND ');
                full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
                EXECUTE full_query INTO count_result;
                RETURN QUERY SELECT 'step7b_targeting'::TEXT, 'With targeting_sql condition'::TEXT, full_query, count_result;
            END IF;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN QUERY SELECT 'step7_error'::TEXT, format('Targeting SQL error: %s', SQLERRM), targeting_sql, -1::BIGINT;
        END;
    ELSE
        RETURN QUERY SELECT 'step7_skip'::TEXT, 'No targeting_sql provided'::TEXT, ''::TEXT, 0::BIGINT;
    END IF;
    
    -- Step 8: ng_companies条件の処理確認
    IF ng_companies IS NOT NULL AND LENGTH(TRIM(ng_companies)) > 0 THEN
        BEGIN
            ng_pattern := TRIM(ng_companies);
            ng_pattern := regexp_replace(ng_pattern, '[\\\^$.+*?{}[\]|()\\\]', '\\\&', 'g');
            ng_pattern := REPLACE(ng_pattern, ',', '|');
            ng_pattern := REPLACE(ng_pattern, '，', '|');
            ng_pattern := REPLACE(ng_pattern, '''', '''''');
            
            where_conditions := where_conditions || ARRAY['company_name !~ ''' || ng_pattern || ''''];
            final_where_clause := array_to_string(where_conditions, ' AND ');
            full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
            EXECUTE full_query INTO count_result;
            RETURN QUERY SELECT 'step8_ng_companies'::TEXT, 'With ng_companies exclusion'::TEXT, full_query, count_result;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN QUERY SELECT 'step8_error'::TEXT, format('NG companies error: %s', SQLERRM), ng_companies, -1::BIGINT;
        END;
    ELSE
        RETURN QUERY SELECT 'step8_skip'::TEXT, 'No ng_companies provided'::TEXT, ''::TEXT, 0::BIGINT;
    END IF;
    
    -- Step 9: exclude_ids条件
    IF exclude_ids IS NOT NULL AND array_length(exclude_ids, 1) > 0 THEN
        where_conditions := where_conditions || ARRAY['id != ALL(ARRAY[' || array_to_string(exclude_ids, ',') || '])'];
        final_where_clause := array_to_string(where_conditions, ' AND ');
        full_query := 'SELECT COUNT(*) FROM companies WHERE ' || final_where_clause;
        EXECUTE full_query INTO count_result;
        RETURN QUERY SELECT 'step9_exclude_ids'::TEXT, format('With exclude_ids (%s items)', array_length(exclude_ids, 1)), full_query, count_result;
    ELSE
        RETURN QUERY SELECT 'step9_skip'::TEXT, 'No exclude_ids provided'::TEXT, ''::TEXT, 0::BIGINT;
    END IF;
    
    -- Step 10: 最終クエリとLIMIT
    full_query := 'SELECT COUNT(*) FROM (SELECT id, company_name, form_url, instruction_json, instruction_valid FROM companies WHERE ' 
                || final_where_clause 
                || ' ORDER BY id LIMIT ' || limit_count || ') AS limited_results';
    
    BEGIN
        EXECUTE full_query INTO count_result;
        RETURN QUERY SELECT 'step10_final'::TEXT, 'Final query with LIMIT'::TEXT, full_query, count_result;
    EXCEPTION
        WHEN OTHERS THEN
            RETURN QUERY SELECT 'step10_error'::TEXT, format('Final query error: %s', SQLERRM), full_query, -1::BIGINT;
    END;
    
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- デバッグ関数の権限設定
GRANT EXECUTE ON FUNCTION debug_get_target_companies_with_sql(TEXT, TEXT, BIGINT, INTEGER, BIGINT[]) TO authenticated, service_role;

-- 基本的なデータ状況確認用関数
CREATE OR REPLACE FUNCTION check_companies_data_status()
RETURNS TABLE(
    check_name TEXT,
    count_result BIGINT,
    sample_ids TEXT
) AS $$
BEGIN
    -- 総数
    RETURN QUERY 
    SELECT 'total_companies'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies ORDER BY id LIMIT 5), ',')
    FROM companies;
    
    -- form_url有り
    RETURN QUERY 
    SELECT 'with_form_url'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE form_url IS NOT NULL ORDER BY id LIMIT 5), ',')
    FROM companies WHERE form_url IS NOT NULL;
    
    -- instruction_json有り
    RETURN QUERY 
    SELECT 'with_instruction_json'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE instruction_json IS NOT NULL ORDER BY id LIMIT 5), ',')
    FROM companies WHERE instruction_json IS NOT NULL;
    
    -- instruction_valid状況
    RETURN QUERY 
    SELECT 'instruction_valid_null'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE instruction_valid IS NULL ORDER BY id LIMIT 5), ',')
    FROM companies WHERE instruction_valid IS NULL;
    
    RETURN QUERY 
    SELECT 'instruction_valid_true'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE instruction_valid = true ORDER BY id LIMIT 5), ',')
    FROM companies WHERE instruction_valid = true;
    
    RETURN QUERY 
    SELECT 'instruction_valid_false'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE instruction_valid = false ORDER BY id LIMIT 5), ',')
    FROM companies WHERE instruction_valid = false;
    
    -- bot_protection_detected状況
    RETURN QUERY 
    SELECT 'bot_protection_null'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE bot_protection_detected IS NULL ORDER BY id LIMIT 5), ',')
    FROM companies WHERE bot_protection_detected IS NULL;
    
    RETURN QUERY 
    SELECT 'bot_protection_true'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE bot_protection_detected = true ORDER BY id LIMIT 5), ',')
    FROM companies WHERE bot_protection_detected = true;
    
    RETURN QUERY 
    SELECT 'bot_protection_false'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE bot_protection_detected = false ORDER BY id LIMIT 5), ',')
    FROM companies WHERE bot_protection_detected = false;
    
    -- employee_count状況
    RETURN QUERY 
    SELECT 'employee_count_under_100'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE employee_count < 100 ORDER BY id LIMIT 5), ',')
    FROM companies WHERE employee_count < 100;
    
    RETURN QUERY 
    SELECT 'employee_count_null'::TEXT, 
           COUNT(*)::BIGINT,
           array_to_string(ARRAY(SELECT id::TEXT FROM companies WHERE employee_count IS NULL ORDER BY id LIMIT 5), ',')
    FROM companies WHERE employee_count IS NULL;
    
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

GRANT EXECUTE ON FUNCTION check_companies_data_status() TO authenticated, service_role;

-- テスト用クエリ例
/*
-- 基本データ状況確認
SELECT * FROM check_companies_data_status();

-- デバッグ実行例 1: targeting_sql有り
SELECT * FROM debug_get_target_companies_with_sql(
    '(employee_count < 100 or employee_count is NULL)',
    '',
    1,
    10,
    NULL
);

-- デバッグ実行例 2: targeting_sql無し
SELECT * FROM debug_get_target_companies_with_sql(
    '',
    '',
    1,
    10,
    NULL
);

-- デバッグ実行例 3: 高いstart_id
SELECT * FROM debug_get_target_companies_with_sql(
    '(employee_count < 100 or employee_count is NULL)',
    '',
    64274,
    1000,
    NULL
);
*/