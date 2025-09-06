# Form Sender セットアップ手順（GAS直キュー作成＋自走Runner）

最終更新: 2025-09-06 (JST)

この文書は、日次送信キュー（GAS→Supabase直呼び出し）と、GitHub Actions Runner（4ワーカー自走）を本番運用/ローカル検証するためのセットアップ手順をまとめる。

---

## 1. Supabase セットアップ

1) テーブル/関数の適用（順序厳守）
- テーブル: `scripts/table_schema/send_queue.sql`
- 関数: `scripts/functions/reset_send_queue_all.sql`
- 関数: `scripts/functions/create_queue_for_targeting.sql`
- 関数: `scripts/functions/claim_next_batch.sql`
- 関数: `scripts/functions/mark_done.sql`
- 関数: `scripts/functions/requeue_stale_assigned.sql`

psql 例（DATABASE_URL 使用）:
```
psql "$DATABASE_URL" -f scripts/table_schema/send_queue.sql
psql "$DATABASE_URL" -f scripts/functions/reset_send_queue_all.sql
psql "$DATABASE_URL" -f scripts/functions/create_queue_for_targeting.sql
psql "$DATABASE_URL" -f scripts/functions/claim_next_batch.sql
psql "$DATABASE_URL" -f scripts/functions/mark_done.sql
psql "$DATABASE_URL" -f scripts/functions/requeue_stale_assigned.sql
```

2) API/RPC の公開
- Supabase ダッシュボード → API → テーブル/関数の公開設定で、上記テーブル/関数が REST/RPC で呼べることを確認。
- 本システムはサービスロールキー（Server Key）を使用するため RLS の有無に依らず動作するが、不要なエンドポイントの公開は避ける。

3) タイムゾーン
- 送信結果の `submitted_at` は JST（Runner側で ISO8601 with TZ）を渡しており、そのまま保存される。

---

## 2. GAS セットアップ（スプレッドシート側）

1) Script Properties
- `SUPABASE_URL`（例: https://xxxx.supabase.co）
- `SUPABASE_SERVICE_ROLE_KEY`（Server Key）

2) コード反映
- `gas/form-sender/SupabaseClient.gs` をGASプロジェクトに追加
- `gas/form-sender/Code.gs` の追記関数を反映（既存とマージ済み）

3) 定期実行トリガー（JST）
- 06:25: `resetSendQueueAllDaily`（完全リセット。必要時は手動実行も可）
- 06:35–06:50: `buildSendQueueForAllTargetings()` を1回実行（スプレッドシートのアクティブ行を一括処理）
- 07:00–19:00: targeting ごとに `form_sender_task` の repository_dispatch を多数送信（Runner起動）。

注意
- GAS ログに企業名/URL等を出さない（既存のポリシー準拠）。
- `targeting_sql` は WHERE 断片として送る。GAS 側で最低限の整形/検証（危険句拒否）を行う。
 - 送信済みは `submissions.success=true` により自動除外されるため、日中にリセットを実行しても致命的な重複送信は発生しません（必要時の手動実行を許容）。

---

## 3. GitHub Actions（Runner）

1) Secrets
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`

2) Workflow
- `.github/workflows/form-sender.yml` は Runner を `src/form_sender_runner.py` に切替済み。
- 実行モード: 各ワークフローで 4 ワーカー（`--num-workers 4`）。
- Playwright: 既存ステップで `playwright install chromium` を実行。必要に応じて公式コンテナ/キャッシュ導入を検討。

3) 併走戦略
- 同一 targeting-id で多数のワークフローを同時起動して良い（`send_queue` が原子的専有で重複排他）。
- 任意で `shard_id` をワークフロー入力として固定すると、衝突がさらに減る。

---

## 4. ローカル検証

1) 事前準備
- `.env`（プロジェクト直下）に Supabase 接続情報を記載:
```
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
```
- 依存インストール:
```
pip install -r requirements.txt
playwright install chromium
```

2) 実行（GUIモード、1件のみで終了）
```
python tests/test_form_sender_local.py
```
- 既定: 1ワーカー・1送信のみで自動終了。
- 特定企業を試す:
```
python tests/test_form_sender_local.py --company-id 123
```
- 本番相当（4ワーカー・上限なし）:
```
python tests/test_form_sender_local.py --nolimit
```

3) 仕組み
- `tests/test_form_sender_local.py` は `src/form_sender_runner.py` を起動。
- デフォルトでは `--num-workers 1 --max-processed 1` を付与して 1件のみ実行。
- `--company-id` 指定時は send_queue を使わず直接その `companies.id` を処理（`mark_done` で記録。キュー更新は0件でも問題なし）。

---

## 5. 運用のヒント

- 速度改善
  - `config/worker_config.json` で画像ブロッキングを ON（既定でON）。
  - CI の slow_mo を無効（既定）。GUI 検証時のみ `PLAYWRIGHT_SLOW_MO_MS` を設定。
- 監視
  - `send_queue` の `status` 遷移（pending→assigned→done/failed）をダッシュボード化。
  - `assigned` が滞留する場合は Runner 異常の可能性。`requeue_stale_assigned` を昼休憩時などに実行。
- セキュリティ/ログ
  - CI上は企業名/URL/メール等を必ずマスク（既存 LogSanitizer 準拠）。

---

## 6. よくある質問

Q. 旧オーケストレーター版はどうしますか？
- 段階移行中は残置。安定後に無効化/削除してください。

Q. targeting-id ごとにテーブルは分けますか？
- いいえ。`send_queue` 1テーブルで十分です（毎朝リセット運用）。

Q. GAS から直接 Supabase を触るのは安全ですか？
- Script Properties に Server Key を持つ設計です。プロジェクト内で厳重管理し、ログへの出力を禁止してください。

---

以上。
