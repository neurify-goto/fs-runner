# Form Analyzer システム

企業フォームページの解析と営業メール用プロンプト生成を行うシステム。

## システム概要

Form Analyzerは、企業のお問い合わせフォームページを解析し、営業活動の可否判定とプロンプト生成を自動化するシステムです。fetch_detailと同じGAS+GitHub Actionsアーキテクチャを採用しています。

## アーキテクチャ

```
[GAS Orchestrator] → [GitHub Actions Worker] → [Supabase Database]
       ↓                        ↓                        ↑
   バッチ管理             フォーム解析処理            結果保存
   ワークフロー呼び出し    営業禁止検出               ステータス更新
   結果確認              プロンプト生成              ログ記録
```

### コンポーネント

1. **GAS Orchestrator** (`gas/form-analyzer/`)
   - バッチ処理の制御
   - GitHub Actions Workflowの呼び出し
   - 処理結果の確認と集計

2. **GitHub Actions Worker** (`.github/workflows/form-analyzer.yml`)
   - Python Workerの実行環境
   - 処理結果のartifacts管理
   - Supabaseへの結果書き込み

3. **Python Worker** (`src/form_analyzer_worker.py`)
   - フォームページの解析
   - 営業禁止文言の検出
   - システム/ユーザープロンプトの生成

4. **Supabase Writer** (`src/form_analyzer_supabase_writer.py`)
   - 処理結果のデータベース保存
   - 企業ステータスの更新
   - バッチログの作成

## 処理フロー

### 1. バッチ開始
```javascript
// GAS関数実行
startFormAnalyzerBatch(10); // 10件のバッチ
```

1. 対象企業を取得（form_page_url有り、未解析）
2. バッチIDを生成
3. GitHub Actions Workflowをトリガー
4. 企業ステータスを「処理中」に更新

### 2. フォーム解析処理
```python
# GitHub Actions内でPython Workerが実行
python src/form_analyzer_worker.py
```

各企業に対して：
1. フォームページにアクセス
2. HTMLコンテンツを取得・解析
3. 営業禁止文言を検出
4. フォーム要素を抽出
5. プロンプトを生成（営業禁止でない場合）

### 3. 結果保存
```python
# 処理結果をSupabaseに保存
python src/form_analyzer_supabase_writer.py --batch-id xxx --results-file artifacts/processing_results.json
```

1. 企業テーブルの更新
2. バッチログの作成
3. 統計情報の集計

### 4. 結果確認
```javascript
// 処理完了後の結果確認
checkFormAnalyzerBatchResult('batch-id');
```

## データ構造

### 企業テーブル更新項目

| カラム名 | データ型 | 説明 |
|---------|---------|------|
| form_analyzer_system_prompt | TEXT | システムプロンプト |
| form_analyzer_user_prompt | TEXT | ユーザープロンプト |
| form_analyzer_prohibition_phrases | JSONB | 営業禁止文言 |
| form_analyzer_form_elements_count | INTEGER | フォーム要素数 |
| form_analyzer_error | TEXT | エラーメッセージ |
| form_analyzer_updated_at | TIMESTAMP | 更新日時 |

### 処理ステータス

- `form_analysis_processing`: 処理中
- `prompt_generated`: プロンプト生成完了
- `prohibition_detected`: 営業禁止検出
- `analysis_failed`: 解析失敗

### バッチログテーブル

`form_analyzer_batch_logs`テーブル：
- バッチごとの処理結果サマリー
- 成功率、営業禁止検出率
- 実行時間、処理日時

## 営業禁止文言検出

以下のパターンで営業禁止文言を検出：

```python
prohibition_patterns = [
    r'営業.*?お断り',
    r'セールス.*?お断り', 
    r'営業.*?禁止',
    r'営業電話.*?お断り',
    r'迷惑.*?メール.*?禁止',
    # その他20パターン
]
```

営業禁止が検出された企業は：
- プロンプト生成をスキップ
- `prohibition_detected`ステータスに設定
- 検出された文言をJSONで保存

## プロンプト生成

営業禁止でない企業に対して：

### システムプロンプト
- 企業情報（名前、URL）
- フォーム情報（要素数、必須フィールド等）
- 営業メール作成指示

### ユーザープロンプト
- 企業の詳細情報
- フォーム要素の一覧
- 具体的な営業メール作成依頼

## 設定とセットアップ

### GAS環境変数

```javascript
// スクリプトプロパティに設定
GITHUB_TOKEN: "ghp_xxxxxxxxxxxxx"
SUPABASE_URL: "https://xxx.supabase.co"  
SUPABASE_SERVICE_ROLE_KEY: "eyJxxxxx"
```

### バッチサイズ設定

```javascript
const CONFIG = {
  BATCH: {
    DEFAULT_SIZE: 10,  // デフォルト
    MAX_SIZE: 25,      // 最大サイズ
    TIMEOUT_MINUTES: 120  // タイムアウト
  }
};
```

フォーム解析は重い処理のため、fetch_detailより小さいバッチサイズを推奨。

### GitHub Secrets

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## モニタリングと運用

### 統計情報確認

```javascript
const stats = getFormAnalyzerStatistics();
console.log(`未解析企業: ${stats.not_analyzed}件`);
console.log(`成功率: ${stats.recent_batches[0]?.success_rate}%`);
```

### 定期実行設定

Google Apps Scriptのトリガー：
- 関数: `scheduledFormAnalyzerExecution`
- 間隔: 6時間ごと（推奨）

### エラー対応

1. **ブラウザエラー**: 
   - Playwright初期化失敗
   - 個別企業でリトライ実行

2. **ネットワークエラー**:
   - タイムアウト設定の調整
   - バッチサイズの縮小

3. **解析エラー**:
   - フォーム要素未検出
   - HTML構造の変更対応

### ログ確認

- **GAS**: Google Cloud Loggingで確認
- **GitHub Actions**: Actions tab でワークフロー実行ログ
- **Supabase**: batch_logsテーブルで処理結果

## パフォーマンス

### 処理時間目安

- 1企業あたり: 10-30秒
- 10件バッチ: 5-15分
- 25件バッチ: 15-30分

### 最適化ポイント

1. バッチサイズの調整
2. タイムアウト設定の最適化
3. 不要なDOM操作の削減
4. ページキャッシュの活用

## fetch_detailとの違い

| 項目 | fetch_detail | form-analyzer |
|------|-------------|---------------|
| 処理内容 | 企業詳細情報取得 | フォーム解析・プロンプト生成 |
| 処理時間 | 5-10秒/件 | 10-30秒/件 |
| バッチサイズ | 20-50件 | 10-25件 |
| 出力 | 構造化データ | テキストプロンプト |
| エラー要因 | ページ構造変更 | 営業禁止文言変化 |

## 今後の拡張

1. **AI API連携**: ChatGPT/Claude API でプロンプト品質向上
2. **フォーム送信テスト**: 実際のフォーム動作確認
3. **業界別カスタマイズ**: 業界に応じたプロンプト調整
4. **A/Bテスト**: プロンプトパターンの効果測定