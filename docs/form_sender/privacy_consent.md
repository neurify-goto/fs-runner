# プライバシー同意チェックの専用機構

本機能は、フォーム送信時に「プライバシーポリシー・個人情報の取り扱い・利用規約への同意」チェックボックスを**マッピングと独立して**確実にONにするためのものです。必須判定に関わらず、該当の同意項目が検出された場合は押下します。

## 仕組み概要
- 実装: `src/form_sender/utils/privacy_consent_handler.py`
- 設定: `config/consent_agreement.json`
- 呼び出し箇所:
  - 初回送信前: `FormSenderWorker._submit_form()` 内（送信ボタン決定後）
  - 確認ページ最終送信前: `PageManager._find_and_submit_final_button()` 内

## 検出アルゴリズム
- スコアリング要素:
  - キーワード `must`（例: 同意/consent/agree）を含む
  - 文脈キーワード `context`（例: 個人情報/プライバシ/privacy/policy/規約/terms）を含む
  - 送信/確認ボタンとの**近接**（既定 600px 以内、上方向優先）
  - `negative`（例: メルマガ/newsletter/配信/案内/広告）を含む場合は除外
- 対象範囲:
  - 送信ボタンの祖先 `form` 内を優先（設定で切替可能）
  - `input[type=checkbox]` と `role=checkbox` を対象
  - ラベル/親要素/アクセシブルネームを統合してテキストを評価

## クリック戦略（フォールバック）
1. `Locator.check()` による通常チェック
2. 関連ラベルクリック（`label[for]` / 親`label` / 近接要素）
3. `element.click()` を JavaScript 実行で強制

## 設定
`config/consent_agreement.json`
- `enabled`: 機能の有効/無効（既定: true）
- `log_only_mode`: ログのみで押下しない検証モード（既定: false）
- `keywords.must/context/negative`: 語彙の調整
- `proximity_px`: ボタンからの距離しきい値
- `vertical_offset_px`: 送信ボタンより下側を無視する縦方向オフセット（px）
- `max_scan_candidates`: 候補探索数の上限
- `max_to_check`: 押下する最大チェック数（複数同意があるページ向け）
- `min_score`: 押下対象とみなす最小スコア
- `ensure_within_same_form`: ボタン直上の `form` 内に限定するか

## ログとセキュリティ
- URLや企業名は出力しません（既存のロガーポリシーに準拠）
- 押下結果はスコアと距離のみを INFO ログで記録
