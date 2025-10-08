# Form Sender GCP Spot Migration Plan

最終更新: 2025-10-07 (JST)
作成者: オートメーションチーム
対象範囲: GAS `form-sender` モジュール / `.github/workflows/form-sender.yml` / `src/form_sender_runner.py` 系列一式（GCP 対応）
- 主要ファイル: `gas/form-sender/Code.gs`, `.github/workflows/form-sender.yml`, `src/form_sender_runner.py`

---

## 1. 背景と目的
- **現行運用:** GAS → Cloud Tasks → Cloud Run dispatcher（`src/dispatcher/*`）→ Cloud Run Jobs → Python Runner の構成を維持しつつ、Repository Dispatch 経由で GitHub Actions を併用している。Playwright 依存ライブラリを都度セットアップするため実行時間と課金が大きく、GitHub Actions 側では 360 分タイムアウトに達するケースもある。
- **Cloud Run ジョブの課題:** Cloud Run Jobs へ集約した構成は既に本番運用しているが、長時間多数並列ジョブを想定すると従量課金が高止まりする。Cloud Run はプリエンプションによるコスト圧縮ができないため、Spot 前提のコンピュートが必要になっている。
- **スポット環境への適合:** Runner は Supabase `job_executions` を中心に状態管理しているため、プリエンプト発生後の再実行に強い。既存の冪等性ロジックを活かし、Spot VM を前提としたバッチ実行環境に移行する。
- **採用方針:** 既存の Cloud Run dispatcher を拡張し、GAS → Cloud Tasks → Cloud Run dispatcher → Cloud Batch → Spot VM という経路へ切り替える。Dispatcher は Supabase 連携・`JOB_EXECUTION_META` 生成など現行ロジックを再利用しつつ、Cloud Batch SubmitJob API を新たに呼び出す。必要に応じて Supabase を us-east-1 へ移し、GCP からの通信コストとのバランスを最適化する。

---

## 2. 目標と非目標
### 2.1 目標
1. GAS から Cloud Batch へジョブ投入する経路を新設し、100 targeting 並列 / 各 4 ワーカー構成を安定運用できること。
2. Docker イメージを Artifact Registry に配置し、Spot 中断時でも冪等性を維持する Runner 起動フローを実装。
3. Secrets / 設定値を Secret Manager + Cloud Storage で安全に注入。
4. Cloud Logging / Cloud Monitoring を活用してログマスキング・アラートを整備する。
5. Cloud Batch のプリエンプト通知を Runner に取り込み、Supabase 側に retry メタデータを記録できる仕組みを明文化する。

### 2.2 非目標
- Supabase RPC、テーブル構造の大幅変更。
- Playwright ベースのフォーム送信アルゴリズムの改修。
- targeting スプレッドシートの既存列削除・並び替えなど大規模リファクタリング（※本計画では必要最小限の列追加にとどめる）。

---

## 3. 想定アーキテクチャ概要

```
[GAS form-sender Trigger]
   │ (targeting_id, client_config_object, execution.run_total, shards, table_mode, test_mode)
   ▼
[Cloud Tasks queue (Script Property `FORM_SENDER_TASKS_QUEUE`)]
   │ - GAS `StorageClient` が client_config を GCS へアップロードし V4 署名 URL を生成
   ▼
[Cloud Run Dispatcher API (src/dispatcher/*)]
   │ 1. client_config 署名 URL を検証し、必要に応じて再署名
   │ 2. Supabase で重複チェック / `job_executions` 登録 / `JOB_EXECUTION_META` エンコード
   │ 3. Cloud Batch SubmitJob (taskCount = run_total, Spot 優先)
   ▼
[Cloud Batch Job]
   ▼
[Compute Pool: Spot VM (n2d-custom-4-10240) + On-demand fallback]
   │ - ワーカー 4 並列を前提とした 2 GiB/ワーカー (=8 GiB) に、Chromium 再起動や Supabase クライアントの瞬間的スパイクを吸収する 2 GiB の共用バッファを加え、合計 10 GiB を標準値とする。軽量案件ではワーカー数を減らして 4–8 GiB へ縮退できるよう可変化を検討し、後述の `batch.machine_type` 自動算出ポリシーで CPU / メモリを調整する。
   │ - タスク毎に `run_index = JOB_EXECUTION_META.run_index_base + BATCH_TASK_INDEX + 1`
   │ - Supabase URL / KEY を Secret Manager から注入
   ▼
[Docker コンテナ (Playwright ランナー)]
   │ - Cloud Storage の署名 URL から client_config を取得
   │ - src/form_sender_runner.py が Supabase RPC を実行
   ▼
[Supabase job_executions / send_queue]
```

補足:
- Cloud Tasks → Cloud Run Jobs の既存経路 (`docs/form_sender_serverless_setup_guide.md`) はフォールバックとして維持し、`USE_SERVERLESS_FORM_SENDER` / `USE_GCP_BATCH` の優先順位を GAS Script Properties で制御する。
- Cloud Batch はジョブとタスク単位のリトライ制御を提供する。既存 Python ロジックで attempt/metadata を扱えるよう `FORM_SENDER_ENV=gcp_batch` を追加する。

---

## 4. コスト最適化戦略
1. **インスタンスタイプ選定**: ベースラインは `n2d-custom-4-10240` (4 vCPU / 10 GiB) の Spot VM。国内案件向けのレイテンシ最優先のため **asia-northeast1 (東京)** を本番リージョンとして固定する（コスト比較や DR の検討は将来タスクとする）。フォーム送信ワーカーは 1 本あたり 2 GiB を上限に運用する方針で、4 ワーカーで 8 GiB を使用するため、Chromium 再起動や Supabase RPC の再試行を吸収する 2 GiB の共用バッファを追加して 10 GiB を標準値とする。targeting によっては 1〜2 ワーカー構成もあるため、`batch.machine_type` を `workers_per_workflow` に基づいて `vCPU = workers_per_workflow`、`memory = workers_per_workflow × 2 GiB + 2 GiB`（下限 4 GiB、上限 10 GiB、256 MiB 単位で切り上げ）として自動算出し、GAS 側で Script Property `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` が指定された場合は明示値を優先するガードを実装する。結果として軽量 targeting は `n2d-custom-2-6144` などへ縮退でき、大規模ジョブは 10 GiB を維持する。
2. **Spot 優先**: Cloud Batch の `allocationPolicy.instances` 先頭エントリを `provisioning_model: SPOT` に固定し、ターゲティングごとの実効同時実行数は `TaskGroup.parallelism` で制御する。targeting シートの `batch_max_parallelism` 列（未指定時は Script Property `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT`, 既定 100）と `run_total` / `workers_per_workflow` から `min` を取り、1 タスクあたりの vCPU 消費は `FORM_SENDER_BATCH_VCPU_PER_WORKER`（既定 1, n2d-custom-4-10240 を前提）で換算して GAS 側で上限チェックする。Cloud Batch にはジョブ全体の `maxCpu` フィールドが存在しないため、並列度の上限管理はこの計算ロジックで担保する。
3. **オンデマンドフォールバック**: 同一ジョブの `allocationPolicy.instances` に `provisioning_model: STANDARD` のエントリを追加し、Spot 枯渇時のみ利用するかを `batch_allow_on_demand_fallback`（Script Property 既定は `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT`）で制御する。費用高騰時は GAS のターゲティング同時数を下げるオペレーションルールを用意。
4. **ネットワークコスト**: Supabase / Cloud Batch / Cloud Storage をすべて asia-northeast1 に統一するため、フォーム送信時の RPC や GCS 署名 URL 取得で外向き課金は発生しない。Cloud Monitoring で RTT を継続観測し、将来的に他リージョンへ移す際は別途コスト試算を実施する。
5. **ログコスト**: Cloud Logging の保持期間を 14 日に設定し、BigQuery シンクは必要最小限に抑える。LogSanitizer に `gcp_batch` モードを追加し、個人情報や企業名をマスクする。
6. **ストレージ**: client_config は Cloud Storage `STANDARD` クラスで十分。Lifecycle ルールで 7 日削除を設定し、クリーンアップ失敗時もコストが膨らまないよう 30 日バックアップ削除を併用。

---

## 5. 実装変更一覧 (現状ギャップと対応方針)

| 領域 | 現状 (2025-10-07 時点) | 対応方針 | 対象ファイル |
| --- | --- | --- | --- |
| Python Runner | `FORM_SENDER_ENV` は `cloud_run` / `github_actions` / `local` のみ。Cloud Batch 固有の環境変数は未対応。 | `gcp_batch` 環境を追加し、Cloud Batch のタスクインデックス (`BATCH_TASK_INDEX` など) と `JOB_EXECUTION_META.run_index_base` を組み合わせて run_id / shard を決定できるユーティリティを実装。 | `src/form_sender_runner.py`, `bin/form_sender_job_entry.py`, `src/utils/env.py`, 新規 `src/utils/gcp_batch.py`, `src/dispatcher/schemas.py` |
| GAS | Cloud Tasks → Cloud Run dispatcher（`CloudRunDispatcherClient`）経由で Cloud Run Jobs / GitHub Actions を起動。Cloud Batch 切替フラグは未実装。 | `USE_GCP_BATCH` ScriptProperty と targeting 列 `useGcpBatch`, `batch_max_parallelism`, `batch_prefer_spot`, `batch_allow_on_demand_fallback` を追加し、CloudRunDispatcherClient へ Cloud Batch 用ペイロードを組み立てるオプションを渡す。既存 StorageClient の署名 URL 生成は流用しつつ、派生した並列度を payload に埋め込み dispatcher が Cloud Batch Submit API に転送できるようにする。payload には `mode: "batch"` と Batch 固有フィールド（`batch_max_parallelism`, `batch_prefer_spot`, `batch_allow_on_demand_fallback`, `batch_machine_type` 等）を明示的に埋め込み、Cloud Run 経路との後方互換キー（`execution`, `tables`）は維持したまま分岐できる構造に更新する。SpreadsheetClient が追加列を読み出して欠損時は既定値へフォールバックし、GitHubClient のブランチ／手動テスト経路も Cloud Batch モードを尊重する。 | `gas/form-sender/Code.gs`, `gas/form-sender/CloudRunDispatcherClient.gs`, `gas/form-sender/StorageClient.gs`, `gas/form-sender/SpreadsheetClient.gs`, `gas/form-sender/GitHubClient.gs` |
| Dispatcher | Cloud Run 上の FastAPI サービス（`src/dispatcher/*`）が Cloud Tasks からのリクエストを受け、Cloud Run Job を起動。Cloud Batch Submit は未対応。 | 既存 dispatcher を拡張し、Cloud Batch SubmitJob API をコールするモードを追加。Supabase 連携・`JOB_EXECUTION_META` 生成ロジックを共有しつつ、Spot ジョブ設定を付与する。`DispatcherService._build_env()` に Cloud Batch 用の環境変数 (`FORM_SENDER_ENV=gcp_batch`, `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_DISPATCHER_AUDIENCE`) を注入して Runner が再署名 API を叩けるようにし、Cloud Run 経路との差分は `execution_mode` で切り替える。必要に応じてモジュール分割（例: `src/dispatcher/gcp_batch.py`）を行う。 | `src/dispatcher/app.py`, `src/dispatcher/service.py`, `src/dispatcher/gcp.py`, 新規 `src/dispatcher/gcp_batch.py` |
| インフラ | Cloud Run dispatcher / Cloud Tasks / Artifact Registry は現在手動構築で、Terraform による管理は未導入。Cloud Batch / Secret Manager 連携も未定義。 | Terraform で Cloud Batch Job Template・Compute Pool・Service Account・Secret Manager アクセスを定義し、既存 Cloud Run dispatcher / Cloud Tasks リソースも IaC 化して一元管理する。 | `infrastructure/gcp/batch/*.tf`, `infrastructure/gcp/dispatcher/*.tf` |
| CI/CD | GitHub Actions は dispatcher 用のビルド/デプロイが限定的。Cloud Batch リソース適用パイプラインは未整備。 | GitHub Actions ワークフローで `docker build/push` → Terraform Plan/Apply → Cloud Run dispatcher デプロイを自動化し、Cloud Batch リソース更新をカバーする。 | `.github/workflows/deploy-gcp-batch.yml` |
| 監視/運用 | AWS 向けメトリクスのみ想定。 | Cloud Logging / Cloud Monitoring で Supabase RPC 失敗率、Cloud Batch ジョブ失敗数、Spot 割り込み回数を監視するダッシュボード・アラートを作成。 | `gcp/monitoring/` (新設), `src/form_sender/security/log_sanitizer.py` |
| Supabase | 現在 tokyo。 | tokyo を維持する場合はリージョン内通信で低遅延。us-east-1 へ移行する場合は Cloud Batch からの RTT を SLO に合わせて評価し、必要なら GCP への近接性を重視したリージョンに再配置する。 | `scripts/table_schema/*`, 環境変数設定 |

---

## 6. 詳細実装計画

### 6.1 Python ランナー改修
1. `src/form_sender_runner.py`
   - `FORM_SENDER_ENV` に `gcp_batch` を追加し、ログサニタイズ・リトライ処理で Cloud Batch の Attempt 情報を活用する。
   - `src/utils/env.py` の `RuntimeEnv` / `get_runtime_environment()` / `should_sanitize_logs()` を `gcp_batch` を正しく判定できるよう拡張し、Cloud Run / GitHub Actions と同等にサニタイズが有効化されるようにする。併せて `src/form_sender/security/log_sanitizer.py` の初期化で `gcp_batch` 判定時は GitHub Actions 相当の強制マスク（企業名・URL 等）を適用し、CI/CD ログポリシーにズレが出ないことを確認する。
   - `_resolve_run_id()` を改修し、`JOB_EXECUTION_META.run_index_base` と `BATCH_TASK_INDEX` を優先採用して既存の `run_index = base + index + 1` を維持する。試行回数は Cloud Batch 標準の `BATCH_TASK_ATTEMPT` を Supabase metadata の retry 管理に取り込む。
   - Cloud Batch では `BATCH_TASK_INDEX` / `BATCH_TASK_ATTEMPT` が提供される一方、Cloud Run 互換ルート経由では既存の `CLOUD_RUN_TASK_INDEX` や `CLOUD_RUN_TASK_ATTEMPT` が残存するケースがあるため、`src/utils/gcp_batch.py` 側に許容するエイリアス設定を用意する。初期値は `task_index_aliases = ["BATCH_TASK_INDEX", "CLOUD_RUN_TASK_INDEX"]`, `attempt_aliases = ["BATCH_TASK_ATTEMPT", "CLOUD_RUN_TASK_ATTEMPT"]` とし、追加の名称が出た場合でも JSON 設定で拡張できる設計にして環境差分でランナーが起動不能になるリスクを避ける。
   - SIGTERM/SIGINT ハンドラを維持し、インスタンス内からメタデータサーバー (`http://metadata.google.internal/computeMetadata/v1/instance/preempted`) をポーリングしてプリエンプト通知を検知する。ポーリング時は `Metadata-Flavor: Google` ヘッダーを必須設定し、`429` / `5xx` 応答には指数バックオフ（初期 1 秒、最大 30 秒）で再試行する。プリエンプト検知時は `_update_job_execution_metadata()` を通じて `preempted` フラグと `last_preempted_at` を記録し、最終リトライまで到達した場合に限り `_mark_job_failed_once()` を呼ぶ構成へ改める。
   - Cloud Batch の各試行開始時に `_update_job_execution_metadata()` で `batch_attempt`, `last_attempt_started_at`, `current_task_index` を更新し、終了時には成功/失敗に応じた `last_attempt_finished_at` と `last_attempt_status` を書き戻す補助ヘルパー（例: `_record_batch_attempt()`）を追加する。これにより Supabase 側のダッシュボードから現在のリトライ状況をリアルタイムに把握できる。
   - 署名 URL 失効時の再取得は dispatcher の新設 API (`POST /v1/form-sender/signed-url/refresh`) を呼び出す。Cloud Batch モード時は `FORM_SENDER_DISPATCHER_BASE_URL` と `FORM_SENDER_DISPATCHER_AUDIENCE`（Cloud Run 認証向け）を必須化し、`google.oauth2.id_token.fetch_id_token` で ID トークンを取得して呼び出す実装とする。ローカル / GitHub Actions ではこれらの環境変数が未設定でも落ちないよう、設定が無い場合は再署名処理をスキップし 15h 以内に終わる前提の挙動を維持する。再取得した URL は `_update_job_execution_metadata()` で `metadata.batch.latest_signed_url` に保存し、Supabase にも反映する。

2. `bin/form_sender_job_entry.py`
   - 既存の `decode_job_meta()` / `delete_client_config_object()` を活かしつつ、`BATCH_TASK_INDEX` / `BATCH_TASK_ATTEMPT` を読み取って run_index・shard・attempt を再計算する分岐を追加する。計算後は後方互換のために従来どおり `FORM_SENDER_RUN_INDEX` / `FORM_SENDER_WORKERS_FROM_META` を再度 `env` に書き戻し、Runner 側の既存ロジックが崩れないようにする。
   - 署名 URL の失効検知と再取得フローを追加する。具体的には Runner 起動前に HEAD 要求で 403/署名期限切れを検知した場合に `POST /v1/form-sender/signed-url/refresh` を呼び出して最新 URL を取得し、Supabase メタデータへパッチを残す。Cloud Batch 再試行時でも同じロジックが動くように、Refresh API 呼び出しには `FORM_SENDER_DISPATCHER_BASE_URL` / `FORM_SENDER_DISPATCHER_AUDIENCE` と `google.oauth2.id_token.fetch_id_token` を利用する。リフレッシュ後の URL で改めて client_config をダウンロードすることを手順化し、失敗時は冪等なリトライログを出力する。
   - `FORM_SENDER_ENV` を `gcp_batch` にセットしつつ、Cloud Batch モードでも最終的に `FORM_SENDER_RUN_INDEX` が設定されることを確認する。GCS オブジェクト削除は既存実装で `google-cloud-storage` を利用しているため、追加実装は不要であることを確認ログに残す。

3. 新規ユーティリティ
   - `src/utils/gcp_batch.py` を追加し、`JOB_EXECUTION_META.run_index_base` と `BATCH_TASK_INDEX`, `BATCH_TASK_COUNT`, `BATCH_TASK_ATTEMPT` から `BatchMeta(run_index, shard_id, attempt, array_size)` を計算。
   - Cloud Batch 固有の環境変数名は IaC と合わせて管理できるよう、定数をモジュール化する。
   - タスクインデックス／リトライ番号の環境変数は `config/worker_config.json` 配下に `batch_env_aliases.task_index` / `batch_env_aliases.task_attempt` を定義し、`src/utils/gcp_batch.py` が読み込んだ上で dispatcher/runner 双方に共有する。`ConfigManager` に `get_batch_env_aliases()`（辞書を返却）を追加して呼び出し側が JSON 構造へ直接依存しないようにし、既存キャッシュロジックと整合するテスト（新規 `tests/test_config_manager.py` 等）を追加・更新する。Stage 環境で実際に渡される名称を記録し、必要なら JSON を更新する運用フローを記載する。

4. `config/worker_config.json`
   - `cpu_profiles` に `gcp_spot` を追加し、最大ワーカー数やプリエンプト待機時間を設定。
   - `runner` セクションに Cloud Batch 用の `metadata_polling_interval` を追加。Playwright のメモリ上限を 2 GiB/ワーカー (計 8 GiB) とするコメントを記載する。
   - `src/dispatcher/schemas.py` の `cpu_class` バリデータを更新し、`standard` / `low` に加えて `gcp_spot` を許容する。未知の値の場合は 422 を返して GAS 側にフィードバックできるよう既存の例外ハンドリングを維持する。

5. 環境変数バリデーション / テスト
   - `src/utils/env.py`, `config/validation.json`, `src/form_sender/utils/validation_config.py`, `tests/test_env_utils.py` を更新し、`FORM_SENDER_ENV=gcp_batch` を許容する。`pytest` で既存ケースが落ちないようテストを追加する。

### 6.2 GAS 改修
1. `gas/form-sender/Code.gs` / `triggerServerlessFormSenderWorkflow_()`
   - 現状の `shouldUseServerlessFormSender_()` は Script Property `USE_SERVERLESS_FORM_SENDER` のみを参照しているため、`resolveExecutionMode_()`（新規）として汎用化し、`USE_GCP_BATCH` を含む優先順位判定（`batch` → `serverless` → GitHub Actions）を行う。`processTargeting()` では同ヘルパーが返すモードに応じて Cloud Tasks 経路（Cloud Run / Cloud Batch）と GitHub Actions を切り替える。互換性維持のため `shouldUseServerlessFormSender_()` 自体は `resolveExecutionMode_() === 'serverless'` を返すラッパーとして残し、Cloud Batch 有効時は false を返すようにする。
   - 停止・監視系の `stopAllRunningFormSenderTasks()` / `stopSpecificFormSenderTask()` / `getRunningFormSenderTasks()` も `resolveExecutionMode_()` を利用し、`batch` モードを `serverless` 系と同列に扱って Cloud Run dispatcher API を呼び出すようにする。既存の `stopAllRunningFormSenderTasksServerless_()` などは名称を保ちつつ、Cloud Batch 実行でも `execution.metadata.batch` を解釈してレスポンスに含める。ブランチ検証・テスト用の `testFormSenderOnBranch()` / `testFormSenderWorkflowTrigger()` も同ヘルパーを参照し、Cloud Batch を選択した場合は GitHub Actions 小ルートをスキップする。
   - `isTargetingServerlessEnabled_()` は `useServerless` 系フラグしか見ていないため、`resolveTargetingExecutionMode_()` を追加し、targeting 行の `useGcpBatch` / `useServerless` / 既存互換表記（`use_gcp_batch` など）を正規化して Cloud Batch を有効化できるようにする。`useGcpBatch` が true の場合は Cloud Batch を選択し、従来の `useServerless` は Cloud Run Jobs を指すように構成を整理する。
   - Script Properties に `USE_GCP_BATCH` を追加し、グローバル優先順位を `USE_GCP_BATCH` → `USE_SERVERLESS_FORM_SENDER` → GitHub Actions として整理する。targeting 行へ `useGcpBatch` 列を追加し（既存列は削除しない）、列が未整備な期間は Script Property のみで切り替えられるようにする。
   - Cloud Batch 判定および CloudRunDispatcherClient に渡すペイロード組み立ては `triggerServerlessFormSenderWorkflow_()` 内で実施する。`processTargeting()` はモード決定のみとし、実際の `batch` サブオブジェクト（`enabled`, `max_parallelism`, `prefer_spot`, `allow_on_demand_fallback`, `machine_type`, `vcpu_per_worker`, `memory_per_worker_mb` など）の埋め込みは `triggerServerlessFormSenderWorkflow_()` に集約する。`machine_type` が指定されていない場合は GAS 側でワーカー数をもとに「vCPU = workers」「メモリ = workers × 3072 + 2048」を計算し、`n2d-custom-{vCPU}-{memory_mb}` 形式のカスタムマシンタイプ文字列を生成して dispatcher に送る。Script Property で上書き値が設定されている場合はそれを優先し、Spreadsheet 列では `batch_machine_type` を optional に保ちつつ、入力値が計算値より小さい場合は GAS ログで警告を出して dispatcher 側にもその旨を通知する。
   - targeting シートに `batch_max_parallelism`, `batch_prefer_spot`, `batch_allow_on_demand_fallback` 列（任意）を追加し、既存列はそのまま保つ。指定がない場合は Script Property `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT`, `FORM_SENDER_BATCH_PREFER_SPOT_DEFAULT`, `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT` を参照する。`triggerServerlessFormSenderWorkflow_()` 内で列→数値変換・真偽値正規化を行い、欠損時は既定値で補完する。

2. `gas/form-sender/CloudRunDispatcherClient.gs`
   - `/v1/form-sender/tasks` エンドポイントに渡す payload フォーマットへ Cloud Batch オプションを拡張し、`payload.mode = "batch"` を設定する。dispatcher は `mode` を基に Cloud Run / Cloud Batch を切り替える。
   - Cloud Tasks の taskId 生成・retryConfig は既存実装を流用し、Cloud Batch モード時にも重複検知を行う。
   - `listExecutions()` のレスポンスへ含まれる `execution.metadata.batch`（`job_name`, `task_group`, `task_count`, `attempts`）を解釈し、Batch 起動分についてはログ出力をマスクした上で `mode=batch` であることを利用者に明示する。
   - `stopSpecificFormSenderTask()` / `stopAllRunningFormSenderTasks()` は引き続き `/v1/form-sender/executions/{id}/cancel` を呼び出す。dispatcher 側で `metadata.batch_job_name` を検出して Cloud Batch API を実行するため、GAS からは追加 API を呼び分ける必要がないことを明文化する。

3. `gas/form-sender/StorageClient.gs`
   - client_config のアップロード／署名 URL 生成は現行通り GAS 内で実行するが、Cloud Batch 前提では Script Property `FORM_SENDER_SIGNED_URL_TTL_HOURS` を導入し既定値を 48 時間へ引き上げて長時間待機時の失効を防ぐ。`StorageClient.generateSignedUrl()` と `triggerServerlessFormSenderWorkflow_()` はこのプロパティ値を読み取って TTL を決定し、生成した署名 URL と TTL を dispatcher に引き渡す。`/v1/form-sender/signed-url/refresh` を通じて再署名できる運用を明文化し、署名失敗時のリトライログを追加して dispatcher／Runner の双方で再署名が実施されたことを Supabase メタデータへ反映する。新 API を利用する Runner には `FORM_SENDER_DISPATCHER_BASE_URL`（例: `https://asia-northeast1-form-sender-dispatcher.a.run.app`）と `FORM_SENDER_DISPATCHER_AUDIENCE` を環境変数で提供し、Script Properties → Cloud Tasks payload → Dispatcher → Cloud Batch 環境変数の流れが途切れないことを明示する。
   - Cloud Batch ルートでは `payload.batch.signed_url_ttl_hours` を追加し、dispatcher 側の `SignedUrlManager` がモードに応じて TTL を切り替えられるようにする。Script Property が未設定の場合は 48h を採用し、Cloud Run ジョブとの互換性のために 15h を維持する場合でも payload 側に明示的に値を渡す。

4. `gas/form-sender/SpreadsheetClient.gs`
   - `getTargetingConfig()` のヘッダーマッピングに `useGcpBatch`, `batch_max_parallelism`, `batch_prefer_spot`, `batch_allow_on_demand_fallback` を追加し、`clientConfig.targeting` 配下へ正規化して格納する。列名の表記揺れ（`use_gcp_batch`, `batchMaxParallelism` 等）に対しても既存 `use_extra_table` 同様のフォールバックを実装する。
   - 数値列は `parseInt` をそのまま使わず、空文字・null を許容してデフォルト値にフォールバックするヘルパーを導入する。これにより追加列が未入力でも GAS 側の既存フローが失敗しない。
   - targeting シート未改修の期間は `USE_GCP_BATCH` ScriptProperty のみで切替できるよう、列が欠落しているケースではログに warning を残しつつ旧挙動 (`useGcpBatch=false`) を維持する。

5. `gas/form-sender/GitHubClient.gs`
   - `testFormSenderOnBranch()` / `testFormSenderWorkflowTrigger()` は `resolveExecutionMode_()` を参照し、`batch` が選択された場合は Cloud Tasks → dispatcher → Cloud Batch ルートで payload を送信する。既存の GitHub Actions repository_dispatch フォールバックは `mode=gha` のみで使用し、ログにモードを出力して誤った経路を検知しやすくする。
   - GAS の停止系テスト (`testStopFormSenderTask` など) では `listRunningExecutions()` のレスポンスに含まれる `metadata.batch` を読み取って Slack 通知文言とデバッグログに反映させる。Cloud Batch 実行時は `job_name`・`task_group` を明示し、Cloud Run 実行と区別できるようにする。
   - Script Properties を参照して dispatcher audience/base URL を解決するロジックを共通化し、Cloud Batch モードでもブランチテストが 403 で失敗しないよう `FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT` を必須チェックする。

### 6.3 Dispatcher (Cloud Run)
1. `src/dispatcher/service.py`
   - `FormSenderTask` に Cloud Batch オプションを保持する `batch` サブモデル（`enabled`, `max_parallelism`, `prefer_spot`, `allow_on_demand_fallback`, `machine_type` など）と `mode` フィールドを追加し、Pydantic のバリデーションと JSON エンコードを更新する。
   - Cloud Run Job 起動ロジックと共通するバリデーションを維持したまま、`task.mode === "batch"` もしくは `task.batch.enabled` を判定して Cloud Batch Submit 用の `BatchJobRunner` を呼び出す分岐を追加する。非指定時は従来どおり Cloud Run Job を呼び出す。
   - `_build_env()` で生成した環境変数を Cloud Batch の `taskSpec.runnables[].environment` に引き継ぐヘルパーを実装し、`FORM_SENDER_CLIENT_CONFIG_URL` や `JOB_EXECUTION_META` などの必須パラメータが Cloud Run と同一の形で渡ることを保証する。Batch 経路ではここで `FORM_SENDER_ENV=gcp_batch`・`FORM_SENDER_DISPATCHER_BASE_URL`・`FORM_SENDER_DISPATCHER_AUDIENCE` を設定し、Runner 側が再署名 API を叩けるようにする。同時に `taskGroups[*].parallelism`（および必要に応じて `maxRunDuration`）へ `execution.parallelism` を確実に反映させ、従来の Cloud Run と同じスロットルポリシーで Supabase / RPC の同時実行数を制御する。Cloud Batch 側のジョブ起動レスポンスから `batch_job_name` 等を Supabase メタデータへ保存し、実行モードにかかわらず監視・停止系ロジックを共通化する。Dispatcher 設定クラスには `FORM_SENDER_DISPATCHER_BASE_URL` / `FORM_SENDER_DISPATCHER_AUDIENCE` を必須フィールドとして追加し、Cloud Run 環境変数・Terraform で設定する手順をここに追記する（Cloud Run Job 名と同様に `DispatcherSettings.from_env()` が検証する）。
   - Cloud Batch 用の `allocationPolicy.instances[0].instanceTemplate` 生成ロジックをモジュール化し、`task.execution.workers_per_workflow` を入力として `cpu = max(workers_per_workflow, 1)`、`memory_mb = ceil((workers_per_workflow × 2048 + 2048) / 256) × 256` をデフォルト算出する。Script Property `FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT`（例: `n2d-custom-4-10240`）や `payload.batch.machine_type` が指定された場合はそれを優先し、計算結果より小さいメモリ値が指定された場合は警告ログを出して 10 GiB をフォールバックにする。将来的な 6〜8 ワーカー対応を見据え、`cpu` 上限と `memory_mb` 上限を Dispatcher 設定で調整可能にしておく。
   - `handle_form_sender_task()` 完了後に Supabase `job_executions.metadata` へ `execution_mode`（`cloud_run` / `batch`）を含む `batch_job_name`, `batch_task_group`, `batch_array_size`, `batch_attempts` など監視・停止に必要なメタデータを保存し、`_public_execution()` が同値を返せるよう更新する。これにより GAS 側の停止系 API が Batch 実行を特定できる。
   - `FormSenderTask` モデルを `mode`（`cloud_run` / `batch`）と `batch` サブモデルを持つ構造に拡張し、`batch.max_parallelism`, `batch.prefer_spot`, `batch.allow_on_demand_fallback`, `batch.machine_type`, `batch.signed_url_ttl_hours` などを受け取れるようにする。既存クライアントからの payload では `mode` を省略した場合に `cloud_run` を既定値とし、`batch` サブモデルも Optional として後方互換を確保する。Pydantic のバリデーションで `signed_url_ttl_hours` を 1〜168 時間に制限し、`payload.batch` が設定された場合は `mode=batch` への正規化を行う。
   - `cancel_execution()` に Cloud Batch 経路を追加し、`metadata.batch_job_name` 等を基に `projects.locations.jobs.delete` を呼び出す。Cloud Run 用識別子が無い場合でも Batch 情報が存在すればキャンセル可能にし、成功時は Supabase ステータスを `cancelled` に更新する。`delete_job` が `404`/`403` を返した際は idempotent とみなし、Supabase の状態更新は継続しつつ dispatcher ログにワーニングを残す。移行期間中は既存レコードのトップレベル `cloud_run_operation` / `cloud_run_execution` を引き続き読めるようフェイルオーバーを残し、Batch 用サブフィールドへ移行済みのレコードでも例外を投げないようにする。既存実行履歴を移行する SQL は `scripts/migrations/202510_gcp_batch_execution_metadata.sql`（仮称）を新設し、`metadata.cloud_run.*` / `metadata.batch.*` の整備を段階的に実施する。
   - `DispatcherSettings` に Batch 関連フィールド（`batch_project_id`, `batch_location`, `batch_job_template`, `batch_task_group`, `batch_service_account_email`）を追加し、環境変数 `FORM_SENDER_BATCH_PROJECT_ID`, `FORM_SENDER_BATCH_LOCATION`, `FORM_SENDER_BATCH_JOB_TEMPLATE`, `FORM_SENDER_BATCH_TASK_GROUP`, `FORM_SENDER_BATCH_SERVICE_ACCOUNT` を Cloud Batch モード有効化時の必須項目として扱う。Cloud Run フォールバックを維持するため `FORM_SENDER_CLOUD_RUN_JOB` は引き続き必須とし、Dispatcher 起動時は Cloud Run / Batch いずれの識別子も設定されていないケースのみ異常終了させて GAS へ設定漏れを通知する。
   - Cloud Run 固有のメタデータ（`cloud_run_operation` / `cloud_run_execution`）は `execution_mode` が `cloud_run` の場合のみ `metadata.cloud_run` サブフィールドへ格納し、Batch 実行では空のままでも `cancel_execution()` が 422 を返さないようにする。既存の Supabase レコード更新ロジック（`update_metadata`）はモードごとに差分マージする形へ改修し、Cloud Batch で追加された `metadata.batch.*` キーを Cloud Run 経路が誤って削除しないようにする。

2. `src/dispatcher/gcp.py`
   - `CloudRunJobRunner` に加えて `CloudBatchJobRunner` を実装し、`projects.locations.jobs.submit` をラップする。Spot/Standard 混在ポリシーや `allocationPolicy` のテンプレート化をサポートする。
   - 署名 URL の再発行・Secret Manager アクセスは既存コードを再利用する。
   - `CloudBatchJobRunner` には `delete_job(job_name)` を実装し、プリエンプト回避や手動停止に対応する。`DispatcherSettings` から Batch 固有のテンプレート名・タスクグループ名・プロジェクト/リージョン情報を受け取り、Submit/Delete 双方で再利用できる形に整える。
   - `SignedUrlManager.ensure_fresh()` はリクエストされたモードを判断し、Cloud Batch では payload の `signed_url_ttl_hours`（もしくは `DispatcherSettings.signed_url_ttl_hours_batch`）を、Cloud Run では従来どおり既定 15h を使用する。モード判定に失敗した場合は Cloud Run と同じ挙動へフォールバックし、既存呼び出しが壊れないことを単体テストで担保する。閾値も `signed_url_refresh_threshold_seconds_batch` を追加で持たせ、Batch では失効 6 時間前に再署名をかけるよう既定値を 21600 秒に設定する。

3. `src/dispatcher/app.py`
   - 新しい API フラグ（例: `mode=batch`）を受け取り、レスポンスとして Cloud Batch の Job ID / Task Count を返却する。
   - `/v1/form-sender/executions` のレスポンスへ `batch` サブフィールド（`job_name`, `task_group`, `task_count`, `attempts` 等）を含め、GAS・モニタリングツールが Cloud Batch 実行を判別できるようにする。

4. Supabase クライアント
   - `src/dispatcher/supabase_client.py` を拡張し、Cloud Batch メタ情報（Spot 割り込み回数、最新 attempt 番号、`preempted` フラグ等）を `job_executions.metadata` へ格納する。Runner からの部分更新に対応する `patch_metadata()` ヘルパーを追加し、従来の `update_metadata()`（全置換）との差し替えもしくは内部委譲を行って冪等に増分反映できるようにする。あわせて、現在 Runner 側で個別実装されている `_update_job_execution_metadata()` をこのヘルパー経由に置き換えるため、リポジトリを `src/shared/supabase/`（仮）へ切り出し dispatcher / runner の双方から同一モジュールを import できる構成にする。これにより差分更新ロジックが単一箇所で管理され、相互にメタ情報を消し合わないよう運用を揃える。
   - Cloud Batch 側の `taskGroup.taskCount` や `maxRetryCount` を保存し、API から参照できるよう JSON 構造を定義する。Cloud Run / Batch で共通化できるキーと専用キーを整理し、移行期間中も後方互換性を維持する。
   - `SignedUrlManager` に Batch 用の TTL パラメータ（既定 48 時間）と再署名 API を実装し、Cloud Batch の長時間待機・プリエンプトでも client_config 取得が失効しないようにする。dispatcher 側へ `/v1/form-sender/signed-url/refresh` を追加し、Runner から `execution_id` / `client_config_object` を渡して再署名 URL を取得できるようにする。新しい署名 URL は Supabase `job_executions.metadata.batch.latest_signed_url` に保存し、次回 Submit 時に dispatcher が自動で最新 URL を利用する。


### 6.4 IaC (Terraform 想定)
> **補足:** 現在リポジトリには `infrastructure/` ディレクトリが存在しないため、本移行で `infrastructure/gcp/batch/` および `infrastructure/gcp/dispatcher/` を新設し、Terraform 管理対象をリポジトリ内に集約する。
1. `infrastructure/gcp/batch/variables.tf`
   - `project_id`, `region`, `supabase_secret_names`, `gcs_bucket`, `artifact_repo`, `prefer_spot_default`, `max_parallelism_default`, `allow_on_demand_default`, `machine_type` 等を定義。

2. `main.tf`
   - Cloud Storage バケット (client-config)、Lifecycle Policy (7 日後削除)。
   - Artifact Registry リポジトリ (`asia-northeast1-docker.pkg.dev/...`).
   - Cloud Batch Job Template, Compute Pool (Spot + Standard)、Cloud Batch 専用 Service Account。
   - Secret Manager (Supabase URL/Key)、Cloud Run dispatcher のサービスアカウント権限、Cloud Tasks キュー設定を更新。
   - Cloud Batch 用サービスアカウントには `roles/run.invoker`（dispatcher 再署名 API 用）と `roles/secretmanager.secretAccessor` を付与し、Cloud Tasks 側サービスアカウントにも `roles/run.invoker` / `roles/cloudtasks.enqueuer` を継続設定する。Terraform の `google_cloud_run_service_iam_member` / `google_secret_manager_secret_iam_member` を用いて明示的に管理し、手動設定を排除する。
   - Cloud Batch Job Template の `taskGroups[*].taskSpec.environment.variables` に `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_URL_TEST` / `SUPABASE_SERVICE_ROLE_KEY_TEST` を Secret Manager 参照として注入し、Cloud Run と同一の資格情報管理ポリシーを維持する。Secret を扱う値は `taskSpec.environment.secret_variables`（`projects/*/secrets/*` 形式）へ設定し、Spot フォールバック時も同じ環境変数が渡ることを確認するための Terraform モジュール出力を追加する。
   - `taskSpec.environment.variables` に `FORM_SENDER_ENV=gcp_batch` と `FORM_SENDER_LOG_SANITIZE=1`、および再署名 API 呼び出しに必要な `FORM_SENDER_DISPATCHER_BASE_URL` / `FORM_SENDER_DISPATCHER_AUDIENCE` を設定し、Dockerfile 既定値 (`cloud_run`) が上書きされることを保証する。`Runnable.Container` の `env` フィールドは利用せず、Batch が公式にサポートする `Environment` を用いる。

3. `outputs.tf`
   - Cloud Run dispatcher のエンドポイント、Cloud Batch Job Template 名称、GCS バケット名を出力し、GAS/CI へ渡す。

### 6.5 CI/CD
1. GitHub Actions ワークフロー (`.github/workflows/deploy-gcp-batch.yml`)
   - `on: workflow_dispatch` + `push` (main) で起動。
   - ステップ: `google-github-actions/auth` → `gcloud auth configure-docker` → `docker build/push` → `terraform init/plan/apply` → `gcloud run deploy`
   - 成功後に Slack 通知。

2. テスト自動化
   - `pytest -k gcp_batch` を CI に追加し、Cloud Batch メタ変換ロジックのテストを実施。GCS は `pytest-gcsfs` 等のモックを利用。

### 6.6 Supabase リージョン方針
- Supabase は本番・ステージングともに asia-northeast1 プロジェクトを利用し、Cloud Batch / Cloud Storage と同一リージョンに統一する。これにより RTT を < 50ms に抑え、フォーム送信成功率を最大化する。
- リージョン変更（例: `us-west1` など）の検討はコスト試算・SLO 影響を評価する将来課題として backlog に登録し、本計画の範囲では扱わない。

---

### 6.7 依存ライブラリ管理
1. `requirements.txt`
   - Cloud Batch API 呼び出しには `google-cloud-batch` を追加し、サポートする Python バージョンと互換な安定版へピン留めする。
   - Spot VM メタデータ取得が必要な場合は `google-cloud-compute` など追加ドライバーも検討し、ライセンス・セキュリティレビューを通す。
   - 追加した依存関係は Renovate / pip-tools の管理対象に含め、`pip install -r requirements.txt` で Cloud Run dispatcher イメージと GitHub Actions の両方が揃うよう検証する。特に `google-cloud-batch` は Dockerfile ビルドと GitHub Actions ランナー双方で import できることを確認し、移行チェックリストにインストールログを残す。

---

## 7. マイグレーション手順
1. **PoC**: Artifact Registry へイメージ登録 → Cloud Batch 手動 Submit → Supabase 書き込みを確認。`FORM_SENDER_ENV=gcp_batch` ブランチでローカルテスト。
2. **Staging**: Supabase ステージング (tokyo or us-east-1) で GAS ステージングを動かし、Cloud Batch の Spot プリエンプト (`gcloud batch jobs tasks terminate`) を検証。
3. **Supabase 本番移行 (必要時)**: 作業停止 → `pg_dump` → 目標リージョンの Supabase プロジェクトへ `pg_restore`。接続先を環境変数で切り替え、ダウンタイムを 3〜6 時間確保。
4. **GCP Batch 切替**: targeting ごとに `useGcpBatch` を true にし、GitHub Actions repository_dispatch をフェーズアウト。2 週間の並行運用期間を設ける。
5. **安定化**: Cloud Monitoring ダッシュボード公開、Spot 割り込みアラート調整、コストエクスプローラレポートを共有。

---

## 8. テスト計画
| フェーズ | テスト内容 | 成功基準 |
| --- | --- | --- |
| 単体 | `src/utils/gcp_batch.py` のユニットテスト | Task Index → Shard 計算が期待値通り |
| 単体 | GAS `triggerServerlessFormSenderWorkflow_()` の machine_type 自動算出 | 1〜4 ワーカー指定で `n2d-custom-{vCPU}-{memory}` が期待値になる（例: workers=2 → `n2d-custom-2-6144`、workers=4 → `n2d-custom-4-10240`） |
| 結合 | Cloud Run Dispatcher → Cloud Batch → Runner → Supabase | `job_executions.status` が `succeeded` になる |
| 運用 | Spot 中断シナリオ (`gcloud batch jobs tasks terminate`) | `attempt` がインクリメントされ、最終的に結果が確定 |
| 性能 | 100 targeting / 8h 連続実行 | Timeout 率 < 5%、Supabase 500 エラー率 < 1% |
| 構築 | `requirements.txt` インストール検証（Docker イメージ・GitHub Actions ランナー） | 両環境で `pip install -r requirements.txt` が成功し `google-cloud-batch` の import が確認できる |
| 運用(停止) | GAS から Batch 実行を停止 (`stopSpecificFormSenderTask`) | Supabase の該当 execution が `cancelled` となり、Cloud Batch 側ジョブが `DELETED` 状態になる |

---

## 9. リスクと対策
1. **Spot 枯渇**: オンデマンド Compute を同じジョブに設定し、Cloud Monitoring アラートで Spot 割り込み回数を監視。必要に応じて GAS 側の同時実行数を減らす。
2. **レイテンシ**: 東京リージョンで運用する際は国内サイトへの遅延が最小。us-east1 へ移した場合はフォーム送信先の応答時間を Synthetic Monitoring で監視し、閾値超過でリージョンを戻す判断材料とする。
3. **Secrets 漏えい**: Cloud Run dispatcher / Cloud Batch 用のサービスアカウントに最小権限を付与し、Secret Manager のアクセスログを定期監査。LogSanitizer に `gcp_batch` 用マスクを追加。
4. **GAS 呼び出し失敗**: dispatcher への呼び出しが連続失敗した場合は Spreadsheet ステータスを `FAILED_CLOUD_BATCH` に戻す。Apps Script で指数バックオフ 3 回を実施。
5. **料金高騰**: Spot 価格がオンデマンドの 80% を超えた場合に Cloud Monitoring → Slack 通知。自動で `taskCount` を 30% 減らす、もしくは Cloud Run 経路へ切り戻す判断フローを定義。
6. **メモリ過不足**: ワーカー数に対して 6 GiB 未満のマシンタイプが割り当てられた場合、Playwright 起動時に OOM となる恐れがある。dispatcher で算出したメモリが 8 GiB 未満かつ `workers_per_workflow >= 4` のケースでは警告ログを残し、Supabase メタデータに `memory_warning` を記録して GAS ダッシュボードで検出できるようにする。逆に過剰割り当て時は Cloud Monitoring のメモリ使用率を 30% 未満連続で検出したら `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` を見直す運用ガイドを整備する。

---

## 10. スケジュール（例）
| 週 | 作業 | 成果物 |
| --- | --- | --- |
| Week 1 | GCP プロジェクト整備、Terraform 雛形作成 | `infrastructure/gcp/batch/` 初期 commit |
| Week 2 | Cloud Run dispatcher 拡張 / GAS クライアント実装 | ステージング Cloud Batch Submit 成功 |
| Week 3 | Runner 改修、ユニットテスト追加 | `tests/test_gcp_batch_meta.py`、Artifact Registry イメージ v0.1 |
| Week 4 | 負荷・スポット中断試験 | 試験レポート、改善リスト |
| Week 5 | 本番デプロイ、72h モニタリング | Cloud Monitoring ダッシュボード、運用 Runbook |

---

## 11. 料金試算根拠 (2025-10-07 時点)
- GCP Spot VM `n2d-custom-4-10240` (asia-northeast1): vCPU $0.00916/時 ×4 + メモリ $0.001228/時 ×10 ≒ **$0.0489/時**。
- オンデマンド参考: 同構成で ≒ **$0.100/時**（vCPU $0.020/時 ×4 + メモリ $0.0020/時 ×10）。
- 同一ワークロード (17,600 インスタンス時/月) → Spot 約 **$991/月**、オンデマンド 約 **$1,718/月**。
- Cloud Run dispatcher / Cloud Batch / Cloud Storage の付随コストは月数十ドル規模。Supabase 通信はリージョン配置によるが、100GB/月 程度なら外向き課金 $12 前後。

---

本計画書をベースに詳細設計レビューを実施し、承認後に IaC・実装タスクを順次着手する。
