# Supabase I/O負荷軽減最適化

このドキュメントでは、fetch-detailシステムにおけるSupabaseのdisk I/O負荷を軽減するために実装した最適化について説明します。

## 実装した最適化

### 1. GAS SupabaseClient.gs の改善

#### 統計取得の効率化
- **変更前**: 3回の個別クエリ（全体統計、完了統計、処理開始済み統計）
- **変更後**: 1回のRPC関数呼び出しで全統計を取得
- **削減効果**: クエリ数を66%削減

#### バッチデータ取得の改善
- **変更前**: データ取得→ステータス更新の2段階処理
- **変更後**: RPC関数による原子的な取得とステータス更新
- **削減効果**: クエリ数を50%削減

#### SELECT文の最適化
- **変更前**: `select=id,company_name,company_url,detail_page`
- **変更後**: `select=id,company_name,detail_page` (不要フィールドを除去)
- **削減効果**: データ転送量を約25%削減

### 2. Python Supabase Writer の強化

#### バッチupsertの信頼性向上
- **チャンク処理**: 50件ずつの小さなチャンクに分割してエラー率を低下
- **リトライ機構**: 指数バックオフによる3回リトライ
- **部分成功の受け入れ**: 完全失敗を回避

#### 個別更新の最小化
- **失敗レコード特定**: バッチ更新で失敗したレコードのみを個別処理
- **無駄な処理の削減**: 成功済みレコードの再処理を回避

### 3. Supabase RPC関数の実装

#### 集約統計関数 (`get_processing_stats`)
```sql
SELECT * FROM get_processing_stats();
```
- 1回のクエリで全統計データを取得
- 複数のCOUNT処理を1つのクエリに統合

#### 原子的更新関数 (`bulk_update_fetch_detail_success/failure`)
```sql
SELECT * FROM bulk_update_fetch_detail_success(ARRAY[1,2,3]::bigint[]);
SELECT * FROM bulk_update_fetch_detail_failure(ARRAY[4,5,6]::bigint[]);
```
- 複数レコードの一括ステータス更新
- トランザクション保証による整合性確保

#### バッチ取得・更新関数 (`get_and_lock_pending_batch`)
```sql
SELECT * FROM get_and_lock_pending_batch('fuma_detail', 20);
```
- データ取得とステータス更新の原子的実行
- `FOR UPDATE SKIP LOCKED`による並行処理対応

#### 一括詳細更新関数 (`batch_update_company_details`)
```sql
SELECT * FROM batch_update_company_details('[...]'::jsonb);
```
- JSONB形式での一括企業詳細更新
- 部分失敗を許容する堅牢な処理

## セットアップ手順

### 1. Supabase RPC関数の実行
```bash
# Supabaseダッシュボードで以下のSQLファイルを実行
cat scripts/supabase_rpc_functions.sql
```

### 2. インデックス作成（推奨）
RPC関数ファイルに含まれるインデックス作成文を実行:
- `idx_companies_processing_status`
- `idx_companies_completion_status` 
- `idx_companies_fetch_status`

### 3. 権限設定確認
RPC関数に適切な実行権限が設定されていることを確認してください。

## 期待される効果

### I/O負荷削減
- **統計クエリ**: 3回 → 1回（66%削減）
- **バッチ取得**: 2回 → 1回（50%削減）
- **デバッグクエリ**: count=exact除去でI/O削減
- **データ転送量**: 不要フィールド除去で25%削減

### パフォーマンス向上
- **応答時間**: 複数クエリの待機時間削減
- **同時実行性**: `SKIP LOCKED`による並行処理改善
- **エラー耐性**: チャンク処理とリトライによる成功率向上

### 運用面での改善
- **ログ品質**: より詳細な処理状況の把握
- **デバッグ効率**: 最適化されたデバッグ関数
- **保守性**: RPC関数による処理のカプセル化

## 互換性とフォールバック

最適化は段階的に適用され、RPC関数が利用できない場合は自動的に従来の処理にフォールバックします：

1. **RPC関数利用**: 最適化されたパス
2. **最適化クエリ**: limit=1でのContent-Range利用
3. **従来処理**: 段階的データ取得

これにより、Supabase環境に関わらず安定した動作を保証します。

## モニタリング

最適化の効果を確認するため、以下のログメッセージに注目してください：

- `集約統計取得成功` - RPC関数による統計取得
- `原子的fetch_detail_queuedステータス更新完了` - 原子的更新の成功
- `バッチ更新完全成功` - チャンク処理の成功
- `個別更新対象: X件（全体のY%）` - フォールバック処理の効率

## トラブルシューティング

### RPC関数が利用できない場合
ログに「RPC関数利用不可」と表示される場合は、`supabase_rpc_functions.sql`の実行を確認してください。

### パフォーマンスが改善しない場合
推奨インデックスが作成されているか確認し、必要に応じて手動で作成してください。

### エラー率が高い場合
チャンクサイズ（デフォルト50件）を小さくすることを検討してください。