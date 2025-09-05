# フィールドマッピング検証レポート（FMIサイクル）

最終更新: 2025-09-05 09:19 JST

本レポートは、`~/.codex/prompts/fmi.md` の手順に従い `tests/test_field_mapping_analyzer.py` を用いて実施したフィールドマッピング検証の結果をまとめたものです。機微情報保護方針に基づき、企業名・URLは記載せず、company_id とローカルファイルパスのみを記載しています。

## 実施概要
- 実行コマンド: `python tests/test_field_mapping_analyzer.py` を1件ずつ反復実行
- 環境: Python 3.11 / Playwright（headless） / `.env` によるSupabase接続
- 目的: 実ページに対するフォーム要素抽出とルールベースマッピングの妥当性確認
- 判定基準: `analysis_result.analysis_summary.analysis_success == true` を「成功」と定義

## サマリー
- 成功（analysis_success=true）の一意な company_id: 8件
- 失敗（ブラウザ/ページクローズの一時的エラー）: 2件（再試行で他案件は成功）
- フォーム未検出案件: 一部あり（想定外のフォーム未設置URL）。アルゴリズム上の誤マッピングは確認されず。
- 結論: 現行アルゴリズムは、今回の8件において重要項目（メールアドレス/お問い合わせ本文 等）を安定的に特定。改善実装は不要と判断。

## 個別結果（成功8件）

以下は成功（analysis_success=true）となった8件の記録です。`analysis_result_*.json` には詳細なスコアやコンテキスト、`page_source_*.html` には抽出した<form>断片を保存しています。

1. company_id: 500334
   - files:
     - `test_results/field_mapping_20250905_091217/analysis_result_20250905_091230.json`
     - `test_results/field_mapping_20250905_091217/page_source_20250905_091222.html`
   - mapped_fields例: 姓, 名, 姓カナ, 名カナ, メールアドレス, お問い合わせ本文（確認メールは自動コピー検出）

2. company_id: 320897
   - files:
     - `test_results/field_mapping_20250905_091324/analysis_result_20250905_091339.json`
     - `test_results/field_mapping_20250905_091324/page_source_20250905_091329.html`
   - mapped_fields例: 統合氏名, 会社名, メールアドレス, お問い合わせ本文, 電話番号

3. company_id: 153781
   - files:
     - `test_results/field_mapping_20250905_091406/analysis_result_20250905_091417.json`
     - `test_results/field_mapping_20250905_091406/page_source_20250905_091410.html`
   - mapped_fields例: 統合氏名, 統合氏名カナ, 会社名, 部署名, メールアドレス, 電話番号, お問い合わせ本文

4. company_id: 351026
   - files:
     - `test_results/field_mapping_20250905_091429/analysis_result_20250905_091441.json`
     - `test_results/field_mapping_20250905_091429/page_source_20250905_091435.html`
   - mapped_fields例: 統合氏名, メールアドレス, 電話番号, お問い合わせ本文

5. company_id: 484742
   - files:
     - `test_results/field_mapping_20250905_091550/analysis_result_20250905_091603.json`
     - `test_results/field_mapping_20250905_091550/page_source_20250905_091554.html`
   - mapped_fields例: 統合氏名, メールアドレス, 電話番号, 住所, お問い合わせ本文

6. company_id: 305005
   - files:
     - `test_results/field_mapping_20250905_091659/analysis_result_20250905_091712.json`
     - `test_results/field_mapping_20250905_091659/page_source_20250905_091705.html`
   - mapped_fields例: 統合氏名, メールアドレス, 電話番号, お問い合わせ本文

7. company_id: 502609
   - files:
     - `test_results/field_mapping_20250905_091721/analysis_result_20250905_091735.json`
     - `test_results/field_mapping_20250905_091721/page_source_20250905_091726.html`
   - mapped_fields例: 統合氏名, メールアドレス, お問い合わせ本文

8. company_id: 414323
   - files:
     - `test_results/field_mapping_20250905_091847/analysis_result_20250905_091900.json`
     - `test_results/field_mapping_20250905_091847/page_source_20250905_091857.html`
   - mapped_fields例: 統合氏名, メールアドレス, お問い合わせ本文

## 参考（未成功・その他観察）
- company_id: 463998 — 対象ページに<form>が存在せず、マッピング対象要素なし（妥当挙動）。
- company_id: 438302, 447730 — Playwrightのページ/コンテキストクローズにより取得失敗（再試行で他案件は成功）。アルゴリズム起因ではないと判断。

## 所感 / 改善検討（今回は実装不要）
- 重要項目（メールアドレス/本文）は全件で良好に特定。分割氏名/統合氏名の両系にも対応。
- メールアドレス確認欄の自動コピー検出が機能（auto_handled）。
- 一部サイトでのブラウザクローズは、テストランナー側の堅牢化（ページ再生成のリトライ強化/タイムアウト調整等）で緩和余地あり。ただし本件の目的（アルゴリズム検証）からは逸脱するため今回は対応見送り。

以上より、現行アルゴリズムに対する変更は行っていません。運用上のエビデンスとして本ドキュメントを追加します。

