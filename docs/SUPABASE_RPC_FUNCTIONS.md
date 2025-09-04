# Supabase RPC Functions for Form Sender

## 概要

FORM_SENDER.md の仕様に完全準拠するため、複雑な WHERE 句を持つクエリを効率的に実行するための Supabase RPC 関数の定義。

## get_target_companies_advanced

### 目的
`targeting_sql` 条件、`ng_companies` 正規表現除外、送信済み企業除外、ランダムID開始点を組み合わせた高度な企業抽出クエリを実行。

### SQL 関数定義

```sql
CREATE OR REPLACE FUNCTION get_target_companies_advanced(
  where_conditions TEXT,
  limit_count INTEGER DEFAULT 50
)
RETURNS TABLE(
  id INTEGER,
  name TEXT,
  form_url TEXT,
  instruction_json JSONB,
  instruction_valid BOOLEAN,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  RETURN QUERY EXECUTE format(
    'SELECT c.id, c.name, c.form_url, c.instruction_json, c.instruction_valid, c.created_at, c.updated_at 
     FROM companies c 
     LEFT JOIN submissions s ON c.id = s.company_id AND s.targeting_id = (
       SELECT DISTINCT targeting_id FROM submissions WHERE company_id = c.id LIMIT 1
     )
     WHERE %s 
     ORDER BY c.id ASC
     LIMIT %s',
    where_conditions,
    limit_count
  );
END;
$$;
```

### 使用例

```python
# Python での呼び出し例（ランダムID開始点対応）
import random

# ランダム開始点を生成
random_start_id = random.randint(1, 536156)

result = self.supabase.rpc('get_target_companies_advanced', {
    'where_conditions': f"""
        form_url IS NOT NULL 
        AND instruction_json IS NOT NULL 
        AND (instruction_valid IS NULL OR instruction_valid = true)
        AND c.id >= {random_start_id}
        AND (industry = 'IT' OR size > 100)
        AND name !~ '株式会社A|合同会社B|○○商事'
        AND s.company_id IS NULL
    """,
    'limit_count': 50
}).execute()
```

### セキュリティ考慮事項

- **SQL インジェクション対策**: WHERE 条件は事前に検証された `targeting_sql` から構築
- **アクセス権限**: `SECURITY DEFINER` で関数実行時の権限を制限
- **入力検証**: 呼び出し側で WHERE 句の妥当性を事前チェック

## 代替実装

RPC 関数が利用できない環境では、`form_sender_worker.py` の `_get_companies_with_basic_query` メソッドによるフォールバック処理を実行。

## 実装状況

- ✅ Python 側の RPC 呼び出し実装完了（ランダムID開始点対応）
- ✅ ランダムID開始点による疑似ランダム抽出機能
- ✅ 不足時の自動補完機能（ID >= 1からの追加取得）
- ✅ 動的MAX_COMPANY_ID取得（キャッシュ機能付き）
- ✅ 効率的重複除去（データベースレベル）
- ✅ フォールバック処理統一化
- ✅ SQL条件構築の安全性強化
- ⚠️ Supabase 側の関数定義は環境に応じて別途実装が必要
- ✅ フォールバック機能による互換性確保

## 新機能：軽量疑似ランダム抽出 v2.0

### 概要
RANDOM()関数を使わずに疑似ランダム抽出を実現（堅牢性強化版）：

1. **動的最大ID取得**: データベースから最大IDを自動取得（24時間キャッシュ）
2. **効率的抽出**: `WHERE c.id >= random_id` でインデックス最適化
3. **スマート補完**: 既処理済みIDを除外した効率的な追加取得
4. **統一フィルタ**: RPC・フォールバック共通の処理ロジック
5. **セキュリティ強化**: SQL条件の安全性検証

### 性能メリット v2.0
- **主キーインデックス最大活用**: 高速なID範囲クエリ
- **RANDOM()関数不使用**: CPU負荷を大幅削減
- **効率的重複除去**: データベースレベルでの重複排除
- **動的データ対応**: 企業データ増加に自動対応
- **メモリ効率**: 不要な重複処理を削減

### セキュリティ強化
- **SQL条件検証**: 危険なキーワードの検出・除外
- **入力エスケープ**: SQLインジェクション対策
- **長さ制限**: 過度に長い条件の制限