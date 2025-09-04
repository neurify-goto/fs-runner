-- Form Sender SQL WHERE句直接実行用 RPC関数
-- targeting_sql, ng_companies等の条件を標準SQLで処理

-- 既存関数を削除（戻り値型変更のため）
DROP FUNCTION IF EXISTS get_target_companies_with_sql(TEXT, TEXT, BIGINT, INTEGER, BIGINT[]);
DROP FUNCTION IF EXISTS get_target_companies_with_sql_and_submissions(TEXT, TEXT, INTEGER, BIGINT, INTEGER, BIGINT[], BOOLEAN);

-- メイン企業取得関数：SQL WHERE句をそのまま実行
CREATE OR REPLACE FUNCTION get_target_companies_with_sql(
    targeting_sql TEXT,
    ng_companies TEXT,
    start_id BIGINT,
    limit_count INTEGER,
    exclude_ids BIGINT[]
)
RETURNS TABLE(
    id BIGINT,
    company_name TEXT,
    form_url TEXT,
    instruction_json TEXT,
    instruction_valid BOOLEAN
) AS $$
DECLARE
    full_query TEXT;
    where_conditions TEXT[];
    final_where_clause TEXT;
BEGIN
    -- 基本条件（必須）
    where_conditions := ARRAY[
        'form_url IS NOT NULL',
        'instruction_json IS NOT NULL', 
        '(instruction_valid IS NULL OR instruction_valid = true)',
        'bot_protection_detected IS NULL',
        'id >= ' || start_id
    ];
    
    -- targeting_sql条件の追加（シンプル版）
    IF targeting_sql IS NOT NULL AND LENGTH(TRIM(targeting_sql)) > 0 THEN
        -- WHERE句プレフィックス除去
        IF UPPER(LEFT(TRIM(targeting_sql), 6)) = 'WHERE ' THEN
            targeting_sql := TRIM(SUBSTRING(targeting_sql FROM 7));
        END IF;
        
        -- targeting_sqlをWHERE句に直接追加
        where_conditions := where_conditions || ('(' || targeting_sql || ')');
        
        RAISE NOTICE 'Applied targeting_sql: %', targeting_sql;
    END IF;
    
    -- ng_companies条件（シンプル版）
    IF ng_companies IS NOT NULL AND LENGTH(TRIM(ng_companies)) > 0 THEN
        DECLARE
            ng_pattern TEXT;
        BEGIN
            ng_pattern := TRIM(ng_companies);
            
            -- カンマ区切りをOR条件に変換
            ng_pattern := REPLACE(ng_pattern, ',', '|');
            ng_pattern := REPLACE(ng_pattern, '，', '|');  -- 全角カンマ対応
            
            -- シングルクォートエスケープ
            ng_pattern := REPLACE(ng_pattern, '''', '''''');
            
            where_conditions := where_conditions || ('company_name !~ ''' || ng_pattern || '''');
            
            RAISE NOTICE 'Applied ng_companies exclusion: %', ng_pattern;
        END;
    END IF;
    
    -- 除外ID条件
    IF exclude_ids IS NOT NULL AND array_length(exclude_ids, 1) > 0 THEN
        where_conditions := where_conditions || ('id != ALL(ARRAY[' || array_to_string(exclude_ids, ',') || '])');
        
        RAISE NOTICE 'Applied exclude_ids: %', array_to_string(exclude_ids, ',');
    END IF;
    
    -- 最終WHERE句構築
    final_where_clause := array_to_string(where_conditions, ' AND ');
    
    -- 完全なクエリ構築
    full_query := 'SELECT id, company_name, form_url, instruction_json, instruction_valid FROM companies WHERE ' 
                || final_where_clause 
                || ' ORDER BY id LIMIT ' || limit_count;
    
    -- デバッグ用ログ出力
    RAISE NOTICE '=== EXECUTING SQL QUERY ===';
    RAISE NOTICE 'Query: %', SUBSTRING(full_query, 1, 500);
    
    -- クエリ実行
    RETURN QUERY EXECUTE full_query;
    
    RAISE NOTICE '=== SQL QUERY COMPLETED ===';

-- EXCEPTION
--     WHEN OTHERS THEN
--         RAISE WARNING 'get_target_companies_with_sql error: %, query: %', SQLERRM, SUBSTRING(full_query, 1, 200);
--         -- 空の結果を返す
--         RETURN;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 送信履歴フィルタリング付き企業取得関数
CREATE OR REPLACE FUNCTION get_target_companies_with_sql_and_submissions(
    targeting_sql TEXT,
    ng_companies TEXT,
    targeting_id_param INTEGER,
    start_id BIGINT,
    limit_count INTEGER,
    exclude_ids BIGINT[],
    allow_failed BOOLEAN DEFAULT false
)
RETURNS TABLE(
    id BIGINT,
    company_name TEXT,
    form_url TEXT,
    instruction_json TEXT,
    instruction_valid BOOLEAN,
    has_success_record BOOLEAN,
    has_failed_record BOOLEAN
) AS $$
DECLARE
    full_query TEXT;
    where_conditions TEXT[];
    final_where_clause TEXT;
BEGIN
    -- 基本条件（必須）
    where_conditions := ARRAY[
        'c.form_url IS NOT NULL',
        'c.instruction_json IS NOT NULL', 
        '(c.instruction_valid IS NULL OR c.instruction_valid = true)',
        'c.bot_protection_detected IS NULL',
        'c.id >= ' || start_id
    ];
    
    -- targeting_sql条件の追加（シンプル版 + テーブルエイリアス c. 付与）
    IF targeting_sql IS NOT NULL AND LENGTH(TRIM(targeting_sql)) > 0 THEN
        -- WHERE句プレフィックス除去
        IF UPPER(LEFT(TRIM(targeting_sql), 6)) = 'WHERE ' THEN
            targeting_sql := TRIM(SUBSTRING(targeting_sql FROM 7));
        END IF;
        
        -- カラム名にエイリアス c. を自動付与（既にある場合は除く）
        targeting_sql := regexp_replace(
            targeting_sql,
            '\b(id|company_name|form_url|instruction_json|instruction_valid|created_at|updated_at|representative|capital|employee_count|postal_code|tel|established_year|established_month|closing_month|average_age|average_salary|detail_page|company_url|fetch_detail_queued|bot_protection_detected)\b',
            'c.\1',
            'g'
        );
        
        -- 重複するc.c.を修正
        targeting_sql := regexp_replace(targeting_sql, '\bc\.c\.', 'c.', 'g');
        
        where_conditions := where_conditions || ('(' || targeting_sql || ')');
        
        RAISE NOTICE 'Applied targeting_sql with table alias: %', targeting_sql;
    END IF;
    
    -- ng_companies条件（シンプル版）
    IF ng_companies IS NOT NULL AND LENGTH(TRIM(ng_companies)) > 0 THEN
        DECLARE
            ng_pattern TEXT;
        BEGIN
            ng_pattern := TRIM(ng_companies);
            
            -- カンマ区切りをOR条件に変換
            ng_pattern := REPLACE(ng_pattern, ',', '|');
            ng_pattern := REPLACE(ng_pattern, '，', '|');  -- 全角カンマ対応
            
            -- シングルクォートエスケープ
            ng_pattern := REPLACE(ng_pattern, '''', '''''');
            
            where_conditions := where_conditions || ('c.company_name !~ ''' || ng_pattern || '''');
            
            RAISE NOTICE 'Applied ng_companies exclusion with table alias: %', ng_pattern;
        END;
    END IF;
    
    -- 除外ID条件
    IF exclude_ids IS NOT NULL AND array_length(exclude_ids, 1) > 0 THEN
        where_conditions := where_conditions || ('c.id != ALL(ARRAY[' || array_to_string(exclude_ids, ',') || '])');
        
        RAISE NOTICE 'Applied exclude_ids: %', array_to_string(exclude_ids, ',');
    END IF;
    
    -- 最終WHERE句構築
    final_where_clause := array_to_string(where_conditions, ' AND ');
    
    -- 送信履歴を含む完全なクエリ構築
    full_query := 'SELECT 
        c.id, 
        c.company_name, 
        c.form_url, 
        c.instruction_json, 
        c.instruction_valid,
        COALESCE(s.has_success, false) as has_success_record,
        COALESCE(s.has_failed, false) as has_failed_record
    FROM companies c 
    LEFT JOIN (
        SELECT 
            company_id,
            bool_or(success = true) as has_success,
            bool_or(success = false) as has_failed
        FROM submissions 
        WHERE targeting_id = ' || targeting_id_param || '
        GROUP BY company_id
    ) s ON c.id = s.company_id
    WHERE ' || final_where_clause || '
    AND (
        s.company_id IS NULL OR 
        (s.has_success = false AND (' || CASE WHEN allow_failed THEN 'true' ELSE 'false' END || ' OR s.has_failed = false))
    )
    ORDER BY 
        CASE WHEN s.company_id IS NULL THEN 0 ELSE 1 END,
        c.id ASC
    LIMIT ' || limit_count;
    
    -- デバッグ用ログ出力
    RAISE NOTICE '=== EXECUTING SQL QUERY WITH SUBMISSIONS ===';
    RAISE NOTICE 'Targeting ID: %, Allow Failed: %', targeting_id_param, allow_failed;
    RAISE NOTICE 'Query: %', SUBSTRING(full_query, 1, 500);
    
    -- クエリ実行
    RETURN QUERY EXECUTE full_query;
    
    RAISE NOTICE '=== SQL QUERY WITH SUBMISSIONS COMPLETED ===';

-- EXCEPTION
--     WHEN OTHERS THEN
--         RAISE WARNING 'get_target_companies_with_sql_and_submissions error: %, targeting_id: %, query: %', 
--             SQLERRM, targeting_id_param, SUBSTRING(full_query, 1, 200);
--         -- 空の結果を返す
--         RETURN;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- 関数の権限設定
GRANT EXECUTE ON FUNCTION get_target_companies_with_sql(TEXT, TEXT, BIGINT, INTEGER, BIGINT[]) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION get_target_companies_with_sql_and_submissions(TEXT, TEXT, INTEGER, BIGINT, INTEGER, BIGINT[], BOOLEAN) TO authenticated, service_role;

-- 【パフォーマンス最適化】インデックス戦略（B-tree制限対応版）
-- 基本フィルタリング用インデックス（instruction_json除外でサイズ制限回避）
CREATE INDEX IF NOT EXISTS idx_companies_targeting_base 
ON companies (id, form_url, instruction_valid) 
WHERE form_url IS NOT NULL AND bot_protection_detected IS NULL;

-- instruction_json存在チェック用インデックス（軽量）
CREATE INDEX IF NOT EXISTS idx_companies_has_instruction 
ON companies (id) 
WHERE instruction_json IS NOT NULL;

-- よく使用される条件用の特化インデックス
CREATE INDEX IF NOT EXISTS idx_companies_employee_count 
ON companies (employee_count) 
WHERE employee_count IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_companies_capital_established 
ON companies (capital, established_year) 
WHERE capital IS NOT NULL AND established_year IS NOT NULL;

-- 会社名検索用（GINインデックスは大きなデータも対応可能）
-- 注意: 'japanese'設定が存在しない場合があるため'simple'設定を使用
CREATE INDEX IF NOT EXISTS idx_companies_company_name_text 
ON companies USING gin(to_tsvector('simple', company_name))
WHERE company_name IS NOT NULL;

-- 部分インデックス（大きなカラムを除外してサイズ制限回避）
CREATE INDEX IF NOT EXISTS idx_companies_valid_records_only
ON companies (id, employee_count, capital, established_year, average_salary)
WHERE form_url IS NOT NULL 
  AND instruction_json IS NOT NULL 
  AND (instruction_valid IS NULL OR instruction_valid = true)
  AND bot_protection_detected IS NULL;

-- postal_code専用インデックス（地域フィルター用）
CREATE INDEX IF NOT EXISTS idx_companies_postal_code 
ON companies (postal_code) 
WHERE postal_code IS NOT NULL;

-- 【統計情報更新】最適なクエリプランの生成を促進
-- ANALYZE companies;

-- 【重要】PostgreSQLインデックス制限・設定について
-- B-treeインデックス制限:
--   - 1行あたり2704バイト制限があります
--   - instruction_json, form_urlなど大きなJSONB/TEXTカラムは制限を超過する可能性
--   - 対策: 大きなカラムを除外した複合インデックス、個別の軽量インデックス作成
-- 全文検索設定:
--   - GINインデックスはサイズ制限なし
--   - 'japanese'設定が存在しない環境では'simple'設定を使用
--   - 'simple'設定でも日本語テキストの基本検索は可能

-- テスト用クエリとサンプル実行例
/*
-- 1. 基本的なtargeting_sql条件テスト
SELECT * FROM get_target_companies_with_sql(
    'employee_count < 100 OR employee_count IS NULL',  -- targeting_sql
    'テスト会社,サンプル',                              -- ng_companies  
    1,                                                  -- start_id
    10,                                                 -- limit_count
    ARRAY[999, 1000]::BIGINT[]                         -- exclude_ids
);

-- 2. 複雑な条件のテスト
SELECT * FROM get_target_companies_with_sql(
    'capital > 10000000 AND established_year >= 2000 AND (employee_count BETWEEN 50 AND 500)',
    NULL,
    1,
    20,
    NULL
);

-- 3. 送信履歴フィルタリング付きテスト
SELECT * FROM get_target_companies_with_sql_and_submissions(
    'employee_count < 100',
    'テスト',
    1,  -- targeting_id
    1,
    10,
    NULL,
    false  -- allow_failed
);

-- 4. SQL安全性検証テスト
SELECT * FROM validate_targeting_sql_safety('employee_count < 100 AND capital > 5000000');
SELECT * FROM validate_targeting_sql_safety('DROP TABLE companies'); -- 危険例（拒否される）

-- 5. 実用的な条件パターン
SELECT * FROM get_target_companies_with_sql(
    'established_year BETWEEN 1990 AND 2020 AND (average_salary > 3000000 OR average_salary IS NULL)',
    '株式会社テスト,有限会社サンプル',
    1,
    50,
    NULL
);
*/