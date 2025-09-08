# FMI マッピング精度改善タスク（引き継ぎメモ）

本ドキュメントは、`~/.codex/prompts/fmi.md` に基づく「12件全件で正確なマッピングを確認しPR作成」タスクの現状を、次セッションへ引き継ぐための要約です。記載のIDは companies.id（数値）のみで、企業名・URL は記載しません（ログポリシー準拠）。

## 概要 / 現状
- 目的: 12件すべての処理対象で、フィールドマッピングが正確（必須充足・不適切入力なし）であることを確認し、最終PRを作成する。
- 現状: 12件すべて再検証を完了し、いずれも要件を満たす結果を得た（下記参照）。最終PRは未作成。
- 作業ブランチ: `fix/mapping-required-address-postal-form-select-20250908`（未プッシュ）

## 12件のIDと状況
- 73189: OK（総合良好。お問い合わせ内容チェックボックス自動処理・同意欄処理も適切）
- 23862: OK（氏名・会社名・メール・電話・住所・本文が適切）
- 381859: OK（市区町村/番地の取りこぼしは再現せず。都道府県/郵便/本文/氏名/電話を正しく確保）
- 332295: OK（複数フォーム混在ページで購読フォームのみを選択→メール単独入力の仕様に合致）
- 377368: OK（郵便番号1/2・住所・電話1/2/3 の分割を正しく昇格。必須救済が安定稼働）
- 218809: OK（必須充足、カバレッジ良好）
- 148170: OK（メール確認欄の自動コピー含め適切）
- 338290: OK（旧式table行ラベルのメール欄をオンラインでも安定検出。force_table_label 救済が動作）
- 444485: OK（フォーム仕様上メール欄なし／本文のみ。評価上適切）
- 254611: OK（必須select/textarea を含め妥当）
- 220945: OK（任意項目は採用せず、氏名/会社/メール/本文のみ。過剰入力なし）
- 149847: OK（「マッピングは適切」判定）

補足: ログには 443080 の実行痕跡あり（評価未確認のため上記12件には含めず）。

## ここまでの改善内容（主な変更点）

- 必須検知の強化（`src/form_sender/analyzer/element_scorer.py` ほか）
  - `※` マーカーの許可、dt/dd・th/td・aria-labelledby・祖先class（required系）を広く検出。
  - 近傍・位置ベース（上/左・距離閾値）の厳格判定を強化。
- 住所/郵便/都道府県の誤分類是正（`field_mapper.py`, `unmapped_element_handler.py`）
  - 住所救済: ラベル/placeholder/属性ヒントから『住所』→『住所_補助*』の段階昇格、取りこぼし対策。
  - 郵便番号: split（1/2）を優先、自動昇格・誤って住所_補助* へ入らない補正。
  - 都道府県: select優先の昇格ロジック追加。input[text] の昇格条件を属性（pref/prefecture）必須に厳格化。
- メール欄救済の多層化（`field_mapper.py`, `required_rescue.py`）
  - 厳格属性救済: name/id==email、確認系（confirm/check/再入力 等）は除外。
  - ラベル文脈救済: Context + 旧式 table 行（左TDラベル）の直接抽出で『メール』強一致を検出し採用（確認系は除外）。
  - 最終救済の追加: テーブル行左セルラベルがメール語の場合の強制採用ルート（score底上げ）を実装。
- 複数フォーム混在ページの選択改善（`form_structure_analyzer.py`, テスト側の抽出）
  - スコアリングに textarea 有無、必須数、subscribe加点・unsubscribe減点、ボタン文言を反映。
  - `tests/test_field_mapping_analyzer.py` の form抽出ロジックでも最適なformのみ選択するように調整。
- outside必須の自動処理を安全側に限定（`unmapped_element_handler.py`）
  - 選択フォーム外は「同意/ポリシー系 checkbox」のみに限定（副作用抑止）。
- 回帰安全策 / 入力値割り当て（`input_value_assigner.py`）
  - 都道府県の空値補完、住所パーツの文脈割当などの微修正。
- 評価補助スクリプト追加（`scripts/`）
  - `scripts/claude_eval.py`（Claude評価の引用問題回避）
  - `scripts/run_claude_eval.sh`（ログからパス抽出→評価の簡略化）

## 既知の課題 / 次アクション

- 旧式テーブルレイアウトのメール欄（例: id=338290）
  - 現状: オンライン・オフラインともに安定検出を確認（force_table_label 系救済が有効）。
  - 対応方針: さらなる変更は不要。将来の回帰に備え、`email_fallback_min_score` と実行順序は現行設定を維持。

- 住所の市区町村/番地の取りこぼし（例: id=381859）
  - 現状: 再現せず。住所/住所_補助* の昇格ロジックにより2フィールドまで安定採用。
  - 対応方針: 現行ロジック維持（追加変更不要）。

- 役職フィールド（id=220945）
  - 現状: 任意項目は採用せず、過剰入力なし。
  - 対応方針: 現行設定維持（追加変更不要）。

## 実行・評価手順（引き継ぎ用）

- 1件ずつの実行（既定）
  - `source /Users/taikigoto/form_sales/fs-runner/.venv/bin/activate`
  - `python tests/test_field_mapping_analyzer.py` （ランダム1件）
  - 任意ID指定: `python tests/test_field_mapping_analyzer.py --company-id <ID>`
  - 実行後の保存先: `test_results/field_mapping_YYYYMMDD_HHMMSS/`
    - ページソース: `page_source_*.html`
    - 解析結果: `analysis_result_*.json`

- Claude評価（オフライン比較推奨）
  - 直近ログから自動: `bash scripts/run_claude_eval.sh /tmp/mapping_test_*.log`
  - 既存成果物で評価: `claude -p "..." --model sonnet` にて、Mapping/HTMLのパスを投入

- オフライン解析（オンライン差分の切り分け用）
  - `python tests/run_offline_mapping.py test_results/.../page_source_*.html`
  - 結果JSON: `test_results/offline/offline_mapping_*.json`

## 成果物パス（抜粋）
- 2025-09-08 実行
  - 338290: `test_results/field_mapping_20250908_210224/analysis_result_20250908_210243.json`（メール=force_table_label で採用）
  - 381859: `test_results/field_mapping_20250908_210324/analysis_result_20250908_210342.json`
  - 377368: `test_results/field_mapping_20250908_210409/analysis_result_20250908_210426.json`
  - そのほか: `test_results/field_mapping_20250908_2105**/analysis_result_*.json`（全12件分）
  - オフライン例（338290）: `test_results/offline/offline_mapping_page_source_20250908_192454.json`

## ブランチ / 変更ファイル（代表）
- ブランチ: `fix/mapping-required-address-postal-form-select-20250908`
- 代表的な変更ファイル:
  - `src/form_sender/analyzer/field_mapper.py`
  - `src/form_sender/analyzer/unmapped_element_handler.py`
  - `src/form_sender/analyzer/form_structure_analyzer.py`
  - `src/form_sender/analyzer/context_text_extractor.py`
  - `src/form_sender/analyzer/element_scorer.py`
  - `src/form_sender/analyzer/required_rescue.py`
  - `src/form_sender/analyzer/input_value_assigner.py`
  - `tests/test_field_mapping_analyzer.py`（抽出/保存/ログ整備）
  - `scripts/claude_eval.py`, `scripts/run_claude_eval.sh`

## 注記（ログ・セキュリティ）
- 社名・URL はログ/ドキュメントへ記載しない（`***COMPANY_REDACTED***` / `***URL_REDACTED***` 相当）。
- 解析時の出力は既定で要約（SummaryOnlyFilter）。詳細が必要な場合のみ `--debug` を使用。

---
次のオペレーションは「PR作成」です。コード変更は追加不要のため、現ブランチをPR化し、12件の再検証結果（企業名・URLは記載せずIDのみ）を添えます。
