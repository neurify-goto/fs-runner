# Form Sender スループット拡張 設計計画（GAS直キュー作成＋日次リセット＋自走ワーカー）

最終更新: 2025-09-08 (JST)

この文書は、現行の Form Sender（GAS→GitHub Actions→Python）を高スループット化するための、実装計画の全体像を示す。ここに書かれた内容だけで、目標・仕様・実装タスク・移行手順・検証方法まで一貫して参照できる。

本版の要点（前版からの修正）
- 送信キューの作成は GitHub Actions を介さず、GAS から Supabase を直接操作する。
- send_queue は毎朝のキュー作成前に「完全リセット（全削除/TRUNCATE）」する（保持なし）。

- 非目的（スコープ外）: 送信精度の引き下げ、既存の RuleBasedAnalyzer / SuccessJudge のロジックの簡略化。
- 前提: すべてのタイムスタンプは JST で記録する。Secrets は .env/GitHub Secrets で管理し、追跡ファイルに記載しない。

---

## 1. 背景と目標

### 背景
- 現状は「GAS起点 → repository_dispatch → GHA 内で `form_sender_worker.py`（多プロセス）実行」だが、無料枠の制約下で単位時間あたりの送信成功件数が不足。
- さらに、同じ targeting-id の複数ワークフローを同時実行した際に、処理対象の重複や競合のない安全な分散が難しい。

### 目標（定量）
- 1ワークフロー（1 Runner）あたり、4ワーカー並列で「1.5〜2.5倍/ワーカー」の時間短縮と合わせて、総合 3〜6倍 のスループット向上を目指す。
- 同一 targeting-id に対して多数のワークフローを併走しても、DBレベルで重複なく配分できること。
- 送信精度は現状維持（SuccessJudge/Analyzerの厳格性維持）。

### 目標（定性）
- 複雑なオーケストレーターを撤廃し、「各ワーカーが自走で『取得→送信→保存』」する構造へ移行。
- GAS 側の targeting 別パラメータ（targeting_sql / ng_companies / 営業時間・日次上限など）をもとに、当日分の送信キューを「事前整列」して Supabase に投入。
- 7:00–19:00 の営業時間帯に、Runner が 4 ワーカーでキューを「原子的に専有→処理→保存」していく。

---

## 2. 全体アーキテクチャ（新）

```
[Spreadsheet]
   |
   | 06:25 JST  GAS: reset_send_queue_all()  // 完全リセット（保持なし）
   |
   | 06:35–06:50 JST  GAS: create_queue_for_targeting() × 各targeting
   v
Supabase: send_queue（当日分のみ）
   |
   | 07:00–19:00 JST  targetingごとに多数の Runner を起動（GAS→repository_dispatch）
   v
[GHA form-sender Runner × 多数/targeting]
   └─ 4 workers / Runner
       ├─ claim_next_batch()  // 原子的専有
       ├─ Playwright送信
       ├─ submissions へ即時INSERT (JST)
       └─ mark_done()/mark_failed()
```

- 「事前整列キュー」を中心に据えることで、複数ワークフロー/複数Runner/複数ワーカーが同一 targeting-id を同時に処理しても重複しない。
- キューの専有は「ロック」ではなく、`UPDATE ... WHERE status='pending' ... LIMIT n RETURNING` による **原子的専有** で軽量化（短時間で競合解決）。
- 将来、targeting 間での総量調整/優先度制御も、キュー生成段階で実現可能。

---

## 3. Supabase 設計

### 3.1 send_queue テーブル（毎朝完全リセット方針）

- 1テーブルで全 targeting を扱う（targeting-id ごとにテーブルを分けない）。
- 本テーブルは「当日用の一時ワークキュー」としてのみ使用し、毎朝の生成前に**完全リセット**する（保持不要）。パーティショニングは不要。

主なカラム（案）:
- `id` (bigint, identity, PK)
- `target_date_jst` (date, NOT NULL)
- `targeting_id` (bigint, NOT NULL)
- `company_id` (bigint, NOT NULL)
- `priority` (int, NOT NULL, default 0) — 小さいほど優先
- `shard_id` (int, NOT NULL) — 水平分散用（`hash(company_id) % S`）
- `status` (enum text: 'pending' | 'assigned' | 'done' | 'failed')
- `assigned_by` (text, NULLABLE) — `github.run_id` 等
- `assigned_at` (timestamptz, NULLABLE, JST)
- `attempts` (int, NOT NULL, default 0)
- `created_at` (timestamptz, default now())

制約/インデックス（例）:
- `UNIQUE (target_date_jst, targeting_id, company_id)`
- 取得用: `(target_date_jst, targeting_id, status, shard_id, priority, id)`
- 回収用: `(target_date_jst, targeting_id, status, assigned_at)`
- 集計用: `(target_date_jst, targeting_id, status)`

保持/メンテ:
- 06:25 JST 時点で `TRUNCATE TABLE send_queue` または `DELETE FROM send_queue` を実行（GAS発）。
- 同日内の再配布のみ必要に応じて実施（詳細は 3.2 の `requeue_stale_assigned()`）。

### 3.2 RPC/関数（最小セット）

0) `reset_send_queue_all()`
- キュー完全リセット（保持無し）。原則 06:25 JST に GAS から1回だけ実行。

1) `create_queue_for_targeting(target_date_jst, targeting_id, targeting_sql, ng_companies, max_daily_sends, extra_priority_key)`
- 当日分の候補を抽出し、`submissions(success=true)` の既存成功や `prohibition_detected=true` を除外。
- `targeting_sql`/`ng_companies` はホワイトリスト整形&簡易検証（WHERE重複の除去、危険句は拒否）。
- `priority` と `shard_id` を付与。
- 上限は「ターゲットあたり一律 5000 件」で `pending` で投入（`max_daily_sends` は送信成功数の上限であり、キュー上限には使用しない）。

2) `claim_next_batch(target_date_jst, targeting_id, shard_id, run_id, limit)`
- 原子的専有: `UPDATE send_queue SET status='assigned', assigned_by=:run_id, assigned_at=now() AT TIME ZONE 'Asia/Tokyo' WHERE ... AND status='pending' AND (shard_id=:shard_id OR :shard_id IS NULL) ORDER BY priority, id LIMIT :limit RETURNING company_id, priority, shard_id;`

3) `mark_done(target_date_jst, targeting_id, company_id, success, error_type, classify_detail)`
- `submissions` に JST でINSERT。`send_queue` は `done`/`failed` 更新、`attempts` インクリメント。

4) `requeue_stale_assigned(target_date_jst, targeting_id, stale_minutes)`
- 同日運用中の救済として、`assigned` かつ `assigned_at` が古い行を `pending` に戻す（Runner異常停止に備える）。

---

## 4. GAS（スプレッドシート）拡張

- シークレット運用: GAS の Script Properties に `SUPABASE_URL`・`SUPABASE_SERVICE_ROLE_KEY` を格納し、UrlFetchApp で Supabase RPC を直接呼び出す（キーはログ出力禁止）。
- 06:25 JST: `reset_send_queue_all()` を1回だけ実行（エラーハンドリングつき。07:00 以降は安全のためリセット禁止ガード）。
- 06:35–06:50 JST: アクティブな各 targeting について、スプレッドシートから `targeting_sql` / `ng_companies` などを読み、`create_queue_for_targeting()` を実行（1ターゲットあたり上限5000件固定）。
- 07:00 JST 以降: 各 targeting-id ごとに**多数の Runner** を repository_dispatch（`form_sender_task`）で起動。
- ログ出力は CIポリシー・社内ポリシーに準拠し、企業名・URLはマスク。

---

## 5. GitHub Actions 構成

### 5.1 送信 Runner（`.github/workflows/form-sender.yml`）
- 目的: `src/form_sender_runner.py` を起動し、4ワーカーで自走。
- 並列: targeting ごとに多数のワークフローを同時起動しても、キューが原子的専有で重複を防ぐ。
- 最適化: Playwright コンテナ or キャッシュ、pip キャッシュ、ヘッドレス強制、`slow_mo=0`。
- 環境: `TZ=Asia/Tokyo`、Secrets（Supabase）を注入。
- Concurrency: 送信中断を避けるため**キャンセル系コンカレンシーは未設定**（必要なら group を run_id ベースにし、`cancel-in-progress=false`）。

---

## 6. Python 実装

### 6.1 新エントリ: `src/form_sender_runner.py`
- 役割: 1プロセス=1ワーカー×4 プロセスで、**各ワーカーが自走**して以下を繰り返す。
  1) 営業時間ゲート（`send_days_of_week`, `send_start_time`, `send_end_time`）と日次上限チェック。
  2) `claim_next_batch(..., limit=1)` で原子的専有。
  3) `IsolatedFormWorker` を用いてフォーム送信（RuleBasedAnalyzer+SuccessJudge）。
  4) `submissions` へINSERT（JST）、`mark_done(success/failed)`。
  5) キュー枯渇時は指数バックオフ（最大60秒）。
- リソース最適化:
  - Playwright `slow_mo=0`（現行CIの100msは廃止）。
  - 画像ブロックON（フォントは既にON）。
  - 入力後待機 500ms→200ms + 検証NG時のみ短リトライ。
- 送信後、SuccessJudge の「早期成功/失敗」検出で固定待機をスキップ。確定不可時のみフォールバック（3s + networkidle + 1s）。
- コンテキスト再利用（企業ごとに `new_page`、エラー時に context 再生成）。
 - タイムスタンプ保存: Supabase には tz 付き timestamp（例: `2025-09-06 10:08:57.901707+00`）が保存される前提。Runner はJST日付境界をUTCに変換して当日集計を行う。

### 6.2 キュー作成（GAS実装）
- GAS に Supabase クライアント（`SupabaseClient.gs` 追加）を実装し、RPC を直接呼び出す。
- 手順は 3.2 の RPC に準拠（抽出・優先度付与・NG適用・上限投入）。

### 6.3 既存コードの扱い（更新）
- 2025-09-08 時点で旧 `form_sender_worker.py` と関連オーケストレーター/DBモジュールは削除済みです。
- ロールバック手順は廃止し、Runner を単一経路として運用します。

---

## 7. 並列戦略（多数ワークフローを想定）

- targeting ごとに**多数のワークフロー**を同時起動して良い。
- 重複防止は `send_queue` の **原子的専有** が担保。
- シャーディング:
  - `shard_id = hash(company_id) % S`（S=8/16 推奨）で平準化。
  - ワークフロー引数で `shard_subset` を渡せば、担当シャードを固定でき、衝突がさらに減る（任意）。
- ワーカー数は 1 Runner あたり 4 固定（4C/16GB の汎用Runnerに最適）。多数のRunnerで水平スケール。

---

## 8. ビジネス制御（営業時間・上限）

- 営業時間: シートの `send_start_time`〜`send_end_time`（JST）を Runner 側でも常時確認。
- 曜日: `send_days_of_week`（0=Mon ... 6=Sun）。
- 日次上限: `max_daily_sends` は送信成功数の上限。キュー作成時には使用せず（上限5000固定）、Runner 側で当日(JST)成功数をDB集計しダブルゲートを担保。

---

## 9. エラー/再配布/冪等

- 失敗時は `mark_done(..., success=false, error_type, classify_detail)` を記録。
- `attempts` をインクリメントし、閾値超過は最終的に `failed` 固定（再試行方針は運用で定義）。
- ワーカー落ちによる取り残しは、夜間 `requeue_stale_assigned()` で `pending` に戻す。
- 1日・1targeting・1company の重複投入は `UNIQUE (date, targeting_id, company_id)` で抑止。

---

## 10. ロギング/セキュリティ/プライバシー

- CI上のログは既存の LogSanitizer を継続利用。企業名/URL/メール/住所/個人名はマスク。
- GitHub Actions では会社名→`***COMPANY_REDACTED***`、URL→`***URL_REDACTED***`。
- 機密情報（Supabase URL/Key）は GHA Secrets に限定。GAS は設定値のみを送る。
- すべてのDB時刻は JST で記録。

---

## 11. パフォーマンス最適化（変更点の要約）

- Playwright `slow_mo=0`（CIの100msは廃止）。
- 入力後待機 500ms→200ms + 検証NG時のみ短リトライ。
- 送信後は SuccessJudge の「早期成功/失敗」で直ちに判定、未確定時のみ 3s + networkidle(10s) + 1s。
- 画像ブロックON、コンテキスト再利用。
- GHA では Playwright コンテナ／ブラウザキャッシュを採用し、初期化時間を短縮。

---

## 12. テスト計画

- 単体テスト: 
  - `create_queue_for_targeting` RPC の抽出・優先度・NG適用のロジック（擬似データ）。
  - `claim_next_batch` の原子的専有（同時取得の擬似テスト）。
- 結合テスト:
  - 小規模ターゲティングで、実行順（priority昇順）・重複無し・上限遵守を検証。
  - 4ワーカー×複数Runnerでの競合が**発生しない**こと（件数一致）。
- ローカル検証:
  - `.env` に Supabase 接続を設定（ローカル専用）。
  - Playwright はGUIで動作確認（Guidelines: ローカルはGUIモード）。
- 本番ローンチ前の A/B：
  - 旧経路（オーケストレーター）と新Runnerを同一 targeting で並走し、重複無し・速度・成功率を比較。

---

## 13. 段階導入・ロールバック

1) Phase 1（最速効果）
- 新Runner（4ワーカー）を追加。Playwright最適化（slow_mo/待機/画像ブロック/再利用）。
- 既存と並存可能にしておく（切り替えフラグで制御）。

2) Phase 2（基盤）
- `send_queue` テーブル・インデックス・RPCを追加。GAS に `SupabaseClient.gs` とキュー作成/リセット実装を追加。
- 1 targeting でパイロット運用。

3) Phase 3（拡大）
- 全 targeting に展開。GAS からの dispatch を切替。

4) Phase 4（整備）
- 旧経路の停止・コード整理・不要モジュールの削除（技術的負債の回収）。

ロールバック:
- 新Runner/キューを停止し、旧 `form_sender_worker.py` を再有効化。テーブルは残置可能（読み出されない）。

---

## 14. 期待KPI/モニタリング

- 1件あたり平均処理時間（p50/p95）。
- Runnerあたりの送信成功数/時間。
- 重複割当ゼロ（検出用に `send_queue` の `assigned_by` 監視）。
- `assigned→done/failed` の遷移率、stale再配布件数。

---

## 15. 今後のPR単位（追跡が容易な塊）

1) PR-A: 新Runner + Playwright最適化（実装最小）
- 新規: `src/form_sender_runner.py`
- 変更: `src/form_sender/browser/manager.py`（slow_mo=0 切替）
- 変更: `src/form_sender/worker/input_handler.py`（待機短縮/可変）
- 変更: `config/worker_config.json`（画像ブロックON, 参照値）
- 変更: `.github/workflows/form-sender.yml`（エントリ差し替え・最適化）

2) PR-B: キュー基盤 + GAS 直実装
- 新規: `scripts/table_schema/send_queue.sql`
- 新規: `scripts/functions/reset_send_queue_all.sql`
- 新規: `scripts/functions/create_queue_for_targeting.sql`
- 新規: `scripts/functions/claim_next_batch.sql`
- 新規: `scripts/functions/mark_done.sql`
- 新規: `scripts/functions/requeue_stale_assigned.sql`
- 新規: `gas/form-sender/SupabaseClient.gs`（REST/RPC呼び出し）
- 変更: `gas/form-sender/Code.gs`（06:25リセット、06:35–06:50作成、07:00以降Runner起動）

3) PR-C: 移行・清掃
- 旧経路の停止/削除、テスト拡充、ドキュメント更新。

各PRの説明には、Guidelines のファイル変更一覧セクション（新規/編集/削除）を必ず含める。

---

## 16. 設計上のトレードオフと判断

- テーブル分割 vs 1テーブル: 運用・集計・拡張性から 1テーブルを採用（保持しないためパーティショニング不要）。
- ロック vs 原子的専有: 高負荷時のスループットと衝突解消時間から、`UPDATE...RETURNING` による原子的専有を採用。
- シャーディング: 任意（無くても安全に動く）が、多数ワークフロー時の平滑化・可観測性のため採用可能に。

---

## 17. セキュリティ/コンフィグ運用

- GAS Script Properties に `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` を設定（リポジトリに追跡しない）。
- GitHub Actions は送信Runnerのみで Secrets を使用。
- ローカル検証は `.env` のみ使用（Guidelines 準拠）。
- その他の非機密定数は `config/*.json` に配置。既存の `worker_config.json` を継続利用。

---

## 18. FAQ

Q. targeting-id ごとのテーブルは必要？
- A. 不要。`send_queue` 1テーブルに `targeting_id` を持たせ、`UNIQUE(date, targeting_id, company_id)` と適切なインデックスで運用。

Q. 既存のオーケストレーターは削除？
- A. 段階移行中は残置。安定後に削除（PR-C）。

Q. GAS は何を送る？
- A. 抽出に必要な targeting パラメータ（targeting_sql / ng_companies / 上限・時間帯など）。Secrets は送らない。

Q. 同一 targeting を多数のワークフローで走らせても大丈夫？
- A. `send_queue` の原子的専有 +（任意で）シャード固定により重複せず安全にスケール。

---

以上。
