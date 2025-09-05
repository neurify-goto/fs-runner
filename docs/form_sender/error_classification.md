# フォーム送信エラー分類（拡張版）

本ドキュメントは `src/form_sender/utils/error_classifier.py` の拡張により採用されたエラー分類の方針とコード一覧をまとめます。

## 目的
- 送信失敗時の原因をより正確・具体に記録し、再試行ポリシーや運用対応を最適化する。
- 既存の `error_type` 文字列を維持しつつ、将来の構造化出力に備えた詳細 API を追加。

## 主要カテゴリと代表コード
- NETWORK: `TIMEOUT`, `DNS_ERROR`, `TLS_ERROR`, `CONNECTION_RESET`, `BLOCKED_BY_CLIENT`, `PAGE_CLOSED`
- HTTP: `RATE_LIMIT`(429), `ACCESS`(403), `UNAUTHORIZED`(401), `SERVER_ERROR`(5xx), `NOT_FOUND`(404), `METHOD_NOT_ALLOWED`(405)
- WAF: `WAF_CHALLENGE`（Cloudflare/Akamai/Imperva などの人間確認/チャレンジ）
- VALIDATION: `MAPPING`（必須未入力）, `VALIDATION_FORMAT`（形式不正）, `FORM_VALIDATION_ERROR`
- SECURITY: `CSRF_ERROR`
- BUSINESS: `DUPLICATE_SUBMISSION`, `PROHIBITION_DETECTED`
- UI/DOM: `ELEMENT_NOT_FOUND`, `ELEMENT_NOT_INTERACTABLE`, `SUBMIT_BUTTON_NOT_FOUND`, `SUBMIT_BUTTON_SELECTOR_MISSING`
- その他: `ELEMENT_EXTERNAL`, `INPUT_EXTERNAL`, `SYSTEM`

## 互換性
- 既存の `classify_error_type` / `classify_form_submission_error` は従来どおり `str` を返します。
- 新規 `classify_detail` は `{code, category, retryable, cooldown_seconds, confidence}` を返す補助APIです。

## 外部設定
- 追加のパターンは `config/error_classification.json` の `extra_patterns` に正規表現で追記できます。
- 設定が無くても動作します（内蔵パターンのみ使用）。

## ログ/個人情報
- 当システムは LogSanitizer により URL・企業名等を自動マスクします。
- 本分類モジュールは詳細ログを最小化し、エラーメッセージ/本文の生出力を避けます。

## 再試行ポリシーの目安
- 再試行推奨: `TIMEOUT`, `DNS_ERROR`, `TLS_ERROR`, `CONNECTION_RESET`, `RATE_LIMIT(クールダウン推奨)`, `SERVER_ERROR`, `ACCESS`
- 再試行非推奨: `WAF_CHALLENGE`（人的/UA切替/送信間隔調整を検討）, `MAPPING`, `VALIDATION_FORMAT`, `CSRF_ERROR`, `DUPLICATE_SUBMISSION`

## 既知の限界
- 一部のサイト固有文言は外部パターンへ追加登録が必要です。
- HTTP ステータスが取得できないワークフローでは本文/メッセージのヒューリスティックに依存します。

