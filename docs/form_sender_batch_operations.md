# Form Sender Batch 運用ガイド

GAS → Cloud Tasks → Cloud Run dispatcher → Cloud Batch（Spot VM 優先）の運用に必要なフロー概要、Script Properties の確認項目、日常オペレーションをまとめたドキュメントです。セットアップ手順は `docs/form_sender_gcp_batch_setup_guide.md` を参照してください。

## 1. アーキテクチャ概要

主要コンポーネントと役割:

- **GAS (form-sender)**: targeting / client 設定を読み込み、実行モードと Script Properties を解決する入口。
- **Cloud Tasks**: `form-sender-dispatcher` キューにタスクを積み、リトライとスケジュールを担保。
- **Cloud Run dispatcher**: タスクを受け取り、client_config を GCS に保存して署名付き URL を生成。Spot 優先で Cloud Batch ジョブを起動。
- **Cloud Batch (Playwright Runner)**: Supabase から設定を取得し、Spot VM 上でワーカーを並列実行。必要に応じてオンデマンドへフォールバック。
- **専用 VPC + Cloud NAT**: Batch ワーカーは `form-sender-batch-vpc` 内のプライベートサブネットで起動し、外部 IP を持たず Cloud NAT 経由でインターネットへ出る。`In-use regional external IPv4 addresses` のクォータ消費を抑えつつ、Outbound 通信要件を満たす。
- **Supabase**: targeting / client マスタと、`job_executions` / `job_execution_attempts` による実行ログを保持。

データフロー（Batch モード）:

```
GAS trigger (time-based / manual)
  │ 1. targeting 読み込み + Script Properties 解決
  ▼
Cloud Tasks queue (form-sender-dispatcher)
  │ 2. dispatcher タスクを enqueue
  ▼
Cloud Run dispatcher
  │ 3. client_config を GCS:FORM_SENDER_GCS_BUCKET へ保存
  │ 4. Supabase 用署名付き URL を生成
  │ 5. Cloud Batch job submit (provisioningModel=SPOT)
  ▼
Cloud Batch workers
  │ 6. signed URL で設定取得 → Playwright 実行
  ▼
Supabase / Cloud Logging / Monitoring
```

> Spot VM が確保できない場合、`batch_allow_on_demand_fallback` が `TRUE` のターゲティングではオンデマンドへ自動切り替えされます。フォールバック結果は `job_executions.metadata.batch` に記録されます。

## 2. Script Properties チェックリスト

GAS が Batch 経路へフォールバックする条件を満たすには、以下の Script Properties が正しく設定されている必要があります（詳細はセットアップガイド 6.1 節）。

| キー | 用途 | 設定例 / 取得先 | 備考 |
| --- | --- | --- | --- |
| `USE_GCP_BATCH` | Batch モード既定値 | `true` | targeting 列が空欄の案件で Batch を強制 |
| `FORM_SENDER_TASKS_QUEUE` | Cloud Tasks キュー名 | `projects/formsalespaid/locations/asia-northeast1/queues/form-sender-dispatcher` | `projects/<id>/locations/<region>/queues/<queue>` 形式 |
| `FORM_SENDER_DISPATCHER_BASE_URL` | Cloud Run dispatcher のベース URL | `https://form-sender-dispatcher-xxxx.a.run.app` | 末尾 `/v1/...` は付けない |
| `FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT` | Cloud Tasks → dispatcher OIDC 用 SA | `form-sender-dispatcher@formsalespaid.iam.gserviceaccount.com` | 未設定だとタスクに認証ヘッダーが付与されず 403 になります |
| `FORM_SENDER_GCS_BUCKET` | client_config 保存用バケット | `formsalespaid-form-sender-client-config` | GAS と dispatcher が共用。未設定だとアップロードに失敗 |
| `SERVICE_ACCOUNT_JSON` | Cloud Tasks / Storage / Batch 用 SA キー | Cloud Console → サービスアカウント → 鍵を追加（JSON） | 4.2.0 節で作成した `form-sender-gas@<project>.iam.gserviceaccount.com`（GAS オーケストレーター用）の JSON 全文を貼り付け |
| `FORM_SENDER_BATCH_JOB_TEMPLATE` | Cloud Batch テンプレート | `projects/formsalespaid/locations/asia-northeast1/jobs/form-sender` | `gcloud batch jobs describe` の出力を転記 |
| `FORM_SENDER_BATCH_TASK_GROUP` | タスクグループ名 | `form-sender-task-group` | 同上 |
| `FORM_SENDER_BATCH_SERVICE_ACCOUNT` | Batch 実行用 SA | `form-sender-batch@formsalespaid.iam.gserviceaccount.com` | テンプレートの `serviceAccountEmail` と一致させる |
| `FORM_SENDER_BATCH_NETWORK` | Batch 用 VPC | `projects/formsalespaid/global/networks/form-sender-batch-vpc` | Terraform の `batch_network_name` 出力を設定 |
| `FORM_SENDER_BATCH_SUBNETWORK` | Batch 用サブネット | `projects/formsalespaid/regions/asia-northeast1/subnetworks/form-sender-batch-subnet` | `privateIpGoogleAccess=true` のサブネットを指定 |
| `FORM_SENDER_BATCH_NO_EXTERNAL_IP` | 外部 IP 割り当て抑止 | `true` | Cloud NAT 越しの通信を強制するフラグ |
| `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT` | targeting 未指定時の並列上限 | `100` | `batch_max_parallelism` 列のデフォルト |
| `FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT` / `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` | Batch マシンタイプ既定 / 強制上書き | `e2-standard-2` / 任意 | デフォルトはコスト効率が高い `e2-standard-2`。標準形状を指定した場合はその vCPU / メモリ枠を上限として利用し、超過したときのみ GAS / dispatcher が `n2d-custom-*`（または `n2-custom-*`）へフォールバックします。案件ごとに変えたい場合は targeting 列を推奨。 |
| `FORM_SENDER_BATCH_INSTANCE_COUNT_DEFAULT` / `FORM_SENDER_BATCH_INSTANCE_COUNT_OVERRIDE` | 起動インスタンス数の既定 / 上書き | `2` / 任意 | Spot VM を複数確保したい場合に利用。`concurrent_workflow` が少なくてもこの台数を最低限確保します。 |
| `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_DEFAULT` / `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_OVERRIDE` | 1 インスタンスあたりのワーカー数 | `2` / 任意 | Python ワーカー上限は 16。負荷やメモリに応じて調整。 |
| `FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT` / `FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT` | ワーカーリソース既定 | `1` / `2048` | dispatcher がマシンタイプを自動算出する際に使用 |
| `FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT` | 共有メモリバッファ | `2048` | 追加メモリを確保したい場合に調整 |
| `FORM_SENDER_BATCH_PREFER_SPOT_DEFAULT` / `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT` | Spot 優先 / フォールバック既定 | `true` / `false` | targeting 側が空欄の場合の挙動（フォールバックは明示的に有効化が必要） |
| `FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH` / `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH` | 署名付き URL の TTL と更新閾値 | `48` / `21600` | 長時間ジョブの URL 期限切れを防ぐ |
| `FORM_SENDER_MAX_SESSION_HOURS_DEFAULT` | ランナー全体の最大稼働時間（時間） | `8` | targeting の `session_max_hours` が空欄のときに利用。未設定時は GAS ハードコード 8 時間。 |
| `FORM_SENDER_DEFAULT_SEND_END_TIME` | 営業終了時刻の既定値 (JST `HH:MM`) | `18:00` | targeting の `send_end_time` が空欄のときに利用。未設定時は GAS ハードコード 18:00。 |

> Supabase 関連 (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`) や GitHub Actions フォールバック用のプロパティも環境ごとに整合性を保ってください。`.env` と Script Properties の差分がないか定期的に確認することを推奨します。
> オンデマンドへの自動切り替えは既定で無効 (`FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT = false`) です。Spot VM でのみ実行したい案件は何も設定せず、フォールバックが必要な案件のみ targeting シートで `batch_allow_on_demand_fallback = TRUE` をオンにしてください（チェックボックスの場合は `TRUE`、手入力の場合は大文字小文字不問）。
> `SERVICE_ACCOUNT_JSON` に使用する `form-sender-gas` サービスアカウントには `roles/cloudtasks.enqueuer`, `roles/storage.objectAdmin`, `roles/run.invoker`, `roles/iam.serviceAccountUser` を付与し、Cloud Tasks・GCS・Cloud Run dispatcher へのアクセスに利用します。また、`form-sender-dispatcher@...` 側には `roles/iam.serviceAccountTokenCreator` を付けたうえで、Cloud Tasks サービスエージェント（`service-<PROJECT_NUMBER>@gcp-sa-cloudtasks.iam.gserviceaccount.com`）にも同ロールを与えておくと OIDC トークン生成が安定します。

## 3. 日次・定期チェック

### 3.1 時間トリガーの基本運用手順

1. 初回デプロイまたはプロパティ更新後に GAS エディタで `deleteFormSenderTriggers()` を実行し、既存の時間トリガーをクリアします（`gas/form-sender/TriggerManagement.gs`）。
2. 続けて `createNextDayTrigger()` を実行すると、翌営業日の `CONFIG.DAILY_TRIGGER_HOUR`（既定 09:00 JST）に `startFormSenderFromTrigger()` が発火するトリガーが自動生成されます。祝日・週末は自動でスキップされ、営業日に再設定されます。
3. `listFormSenderTriggers()` で作成されたトリガーを確認し、`startFormSenderFromTrigger` ハンドラのみが残っていることをチェックします。

### 3.2 追加トリガー（GitHub Actions 時代のレガシー）

- `startFormSenderFromTriggerAt7()` および `startFormSenderFromTriggerAt13()` は、GitHub Actions（Workflow Dispatch）ベースだった頃の再実行用ハンドラです。Cloud Batch（GCP）運用では利用しません。
- GAS プロジェクトにこれらの時間トリガーが残っている場合は、以下の手順で削除してください。
  1. `deleteTriggersByHandler('startFormSenderFromTriggerAt7')`
  2. `deleteTriggersByHandler('startFormSenderFromTriggerAt13')`
- 以後は `createSpecificTimeTriggerFor` で同名ハンドラを登録しないよう注意し、`listFormSenderTriggers()` を用いて `startFormSenderFromTrigger` 以外のトリガーが存在しないことを定期的に確認します。

### 3.3 トリガー監視ユーティリティ

- `listFormSenderTriggers()` : 現在登録されている `startFormSender...` 系トリガーを一覧表示。
- `deleteCurrentFormSenderTrigger()` : 直前に動いた `startFormSenderFromTrigger` トリガーを削除し、再設定漏れを防止。
- `deleteFormSenderTriggers()` : `startFormSenderFromTrigger` を対象に一括削除。
- `deleteTriggersByHandler(handlerName)` : 任意のハンドラ（例: `'startFormSenderFromTriggerAt7'`）のみを削除。

### 3.4 日次チェックリスト

1. **Cloud Tasks キューの遅延確認**
   - Cloud Console → **Cloud Tasks** → `form-sender-dispatcher`。
   - `In flight` と `Queue latency` が増加していないか確認。閾値: 遅延が 5 分を超える場合はアラート。
2. **Cloud Batch ジョブの状態確認**
   - Cloud Console → **Batch** → **ジョブ**。
   - 失敗 (`FAILED`) や停止 (`DELETED`) が連続していないか確認。
   - 代表的なメトリクス: `Succeeded/Failed task count`、Spot プリエンプト率。
3. **Supabase job_executions の整合性**
   - Supabase SQL エディタで以下を実行し、異常な滞留がないかチェック。
     ```sql
     select status, count(*)
       from job_executions
      where created_at > now() - interval '24 hours'
      group by 1;
     ```
4. **Cloud Run dispatcher のヘルスチェック**
   - `https://<dispatcher-url>/healthz` を叩いて `{"status":"ok"}` が返るか確認。
   - 異常時は Cloud Logging で `service_name="form-sender-dispatcher"` を検索。

## 4. 手動オペレーション

### 4.1 操作できるエントリーポイント

| 用途 | GAS 関数 | 補足 |
| --- | --- | --- |
| 営業日朝の定期起動 | `startFormSenderFromTrigger()` | 時間トリガーから呼ばれるメインエントリ。`createNextDayTrigger()` でスケジュール。`gas/form-sender/Code.gs` |
| 07:00 / 13:00 JST 再起動 | `startFormSenderFromTriggerAt7()` / `startFormSenderFromTriggerAt13()` | GitHub Actions 時代の名残。Batch 運用ではトリガーを登録せず、存在する場合は削除。 |
| 単一 targeting を手動実行 | `startFormSender(targetingId)` | targeting シート設定を読み込み dispatcher を起動。必要に応じて `options` 引数で `testMode` などを指定。 |
| 全 targeting を手動実行 | `startFormSenderAll()` | 対象を指定せず全アクティブ targeting を処理。 |
| dispatcher へ直接 enqueue | `triggerFormSenderWorkflow(targetingId, options)` | Cloud Tasks を経由して dispatcher に直接投入。テストや特殊オプション指定時に利用。 |
| 実行中ジョブ一覧 | `getRunningFormSenderTasks()` | Supabase / dispatcher から実行中リストを取得し、`batch_job_name` などを確認。 |
| 全ジョブ停止 | `stopAllRunningFormSenderTasks()` | dispatcher / GitHub Actions を横断して一括停止。Batch 実行への移行後は dispatcher ルートが利用される。 |
| targeting 単位停止 | `stopSpecificFormSenderTask(targetingId)` | Section 10 を参照。Batch / Cloud Run いずれも `execution_id` 単位でキャンセル。 |

### 4.2 テスト実行（Dry Run）
- GAS エディタから `triggerFormSenderWorkflow(targetingId, { testMode: true })` を実行。
- Supabase `job_executions` に `execution_mode=batch` でレコードが追加され、Cloud Batch で `RUNNING` → `SUCCEEDED` となることを確認。

### 4.3 ジョブの停止
- GAS エディタ：`stopSpecificFormSenderTask(targetingId)`。
- もしくは CLI：
  ```bash
  gcloud batch jobs delete <job-name> --location=asia-northeast1 --project=formsalespaid
  ```
- Supabase 側で `status=cancelled` に更新されているか確認。

### 4.4 Cloud Tasks の手動投入
- Cloud Console → **Cloud Tasks** → `form-sender-dispatcher` → **タスクを作成**。
- 送信先 URL: `https://<dispatcher-url>/v1/form-sender/tasks`
- OIDC サービスアカウント: `form-sender-dispatcher@formsalespaid.iam.gserviceaccount.com`
- リクエスト本文に GAS から送られる Payload を貼り付けてテスト可能。

## 5. 監視とアラート推奨事項

| 項目 | 推奨設定 |
| --- | --- |
| Cloud Batch 失敗ジョブ | `job.status=FAILED` が連続した場合にメール通知 |
| Cloud Run dispatcher エラー | `severity>=ERROR` を条件に Log-based Alert |
| Cloud Tasks 遅延 | `Queue latency > 300 秒` でアラート |
| Supabase 異常滞留 | `job_executions` の `pending` が一定件数を超えたら通知 |

## 6. トラブルシューティング例

| 症状 | 確認ポイント | 対応 |
| --- | --- | --- |
| Cloud Run dispatcher が起動しない | Cloud Logging で `RuntimeError: 環境変数 ...` を検索 | `docs/form_sender_gcp_batch_setup_guide.md` の 6.3 節を参照し、環境変数・シークレットを再設定 |
| Batch ジョブが `FAILED` を繰り返す | `job_executions.metadata.batch` の `memory_warning`、`machine_type` を確認 | targeting の `batch_memory_per_worker_mb`/`batch_machine_type` を増やす |
| Supabase に `execution_mode=cloud_run` が残る | GAS Script Properties `USE_GCP_BATCH` などを確認 | targeting 側フラグが `FALSE` の場合は切り替え漏れ。GAS Property と targeting を更新 |
| Cloud Tasks で 401/403 | ターゲット URL または OIDC サービスアカウントの権限不足 | Cloud Run dispatcher の URL と SA (`form-sender-dispatcher@...`) に `roles/run.invoker` が設定されているか確認 |

## 7. targeting シート設定リファレンス

> **真偽値の記法について**: チェックボックス列（`TRUE`/`FALSE`）や手入力の真偽値は、大文字小文字を区別せずに処理されます。`TRUE`、`true`、`True` のいずれも同じ意味として扱われます。

| 列名 | 役割 | 値の優先順位 | 備考 |
| --- | --- | --- | --- |
| `useGcpBatch` / `useServerless` | Batch／Serverless の切り替え | targeting列 → Script Properties (`USE_GCP_BATCH`, `USE_SERVERLESS_FORM_SENDER`) → デフォルト | `TRUE`/`FALSE` のほか `1`/`0` や `yes`/`no` も可（大文字小文字不問）。Batch を使わない案件は `FALSE`。 |
| `concurrent_workflow` | Cloud Batch タスク総数 | targeting列のみ | GAS 側で `task_count` として使用。未指定なら 1。 |
| `batch_max_parallelism` | 同時実行タスクの上限 | targeting列 → Script Property `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT`（既定 100） | `concurrent_workflow` と合わせて並列度を調整。 |
| `batch_prefer_spot` | Spot VM を優先するか | targeting列 → Script Property `FORM_SENDER_BATCH_PREFER_SPOT_DEFAULT` | `TRUE` で Spot 優先、`FALSE` でオンデマンドのみ。 |
| `batch_allow_on_demand_fallback` | Spot 枯渇時にオンデマンドへ切り替えるか | targeting列 → Script Property `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT` | 省略時は `FALSE`。必要な案件のみ `TRUE` にしてフォールバックを許可。 |
| `batch_machine_type` | 利用したいマシンタイプ | targeting列 → Script Property `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` → `FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT` | 既定値は `e2-standard-2`。標準形状を指定した場合はその形状の上限を使い切るまでは維持され、超過時のみ自動で `n2d-custom-*`（または入力が `n2-*` 系なら `n2-custom-*`）にフォールバックします。必要な案件のみ上書き。 |
| `batch_instance_count` | 起動する Spot インスタンス数 | targeting列 → Script Property `FORM_SENDER_BATCH_INSTANCE_COUNT_OVERRIDE` → `FORM_SENDER_BATCH_INSTANCE_COUNT_DEFAULT` (既定 2) | `concurrent_workflow` より小さくてもこの台数は確保。1 台だけにしたい場合は `1` を指定。 |
| `batch_workers_per_workflow` | 1 インスタンスあたりの Python ワーカー数 | targeting列 → Script Property `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_OVERRIDE` → `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_DEFAULT` (既定 2) | 1〜16 の範囲で設定。ワーカーを増やす場合はメモリも確認。 |
| `batch_vcpu_per_worker` | 1 ワーカーあたりの vCPU | targeting列 → Script Property `FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT` | 1 以上の整数。 |
| `batch_memory_per_worker_mb` | 1 ワーカーあたりのメモリ (MiB) | targeting列 → Script Property `FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT` | 2048 以上推奨。 |
| `batch_memory_buffer_mb` | 追加メモリバッファ | targeting列 → Script Property `FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT` | 全体バッファ（共有）。 |
| `batch_signed_url_ttl_hours` | 署名付き URL の有効期限 (時間) | targeting列 → Script Property `FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH` | 1〜168 の整数。 |
| `batch_signed_url_refresh_threshold_seconds` | 署名付き URL を更新する閾値 (秒) | targeting列 → Script Property `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH` | TTL より短い値に設定。 |
| `batch_max_attempts` | Cloud Batch のリトライ上限 | targeting列 → Script Property `FORM_SENDER_BATCH_MAX_ATTEMPTS_DEFAULT` | 1 以上の整数。 |
| `session_max_hours` | ランナー全体の最大稼働時間 (時間) | targeting列 → Script Property `FORM_SENDER_MAX_SESSION_HOURS_DEFAULT` → GAS 既定 8h | 長時間実行が必要な案件だけ数値を指定。未設定の場合は Script Property / ハードコードの順に適用。 |
| `send_end_time` | 営業終了時刻 (JST `HH:MM`) | targeting列 → Script Property `FORM_SENDER_DEFAULT_SEND_END_TIME` → GAS 既定 18:00 | GAS の営業時間判定と自動停止に使用。営業終了を前倒ししたい案件で調整。 |
| `branch` | Git リファレンス | targeting列 → Script Properties（`FORM_SENDER_GIT_REF_DEFAULT` などがある場合） | GitHub Actions ルート併用時のみ利用。 |
| `use_extra_table` などクライアント固有列 | GAS `client_config` の挙動制御 | targeting列のみ | Extra テーブル使用有無などを制御。 |

### 優先順位ルールのまとめ
- **targeting シート** > **Script Properties** > **アプリ既定値** の順で評価されます。
- targeting に空欄がある場合のみ Script Property が利用され、両方が未設定ならコード側のハードコード既定値（例: 並列数 1）が使われます。
- `session_max_hours` / `send_end_time` も同じ優先順位で評価され、最終的には GAS ハードコード値（8 時間 / 18:00 JST）がフォールバックとして機能します。
- 案件ごとの個別調整は targeting シートで行うのが基本。全案件共通の既定値を変えたい場合のみ Script Property を更新します。

---

## 8. targeting シートの並列設定ガイド

- `concurrent_workflow` は「ターゲティングで要求するタスク数」です。GAS は `batch_instance_count` と比較し、大きい方を Cloud Batch の `task_count` として送信します。
- `batch_instance_count` は「確保したい Spot VM 台数」の下限です。Script Properties 既定は 2。1 台運用にしたい案件は targeting シートで `1` を指定してください。
- `batch_max_parallelism` は「同時実行するタスクの上限」を決める値です。指定がある場合はその値、空欄の場合は Script Property `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT`（既定 100）が上限として適用されます。実際には `max_parallelism` も `batch_instance_count` 以上になるよう GAS が自動調整します。

挙動のまとめ:

```
実行されるタスクの総数 = max(concurrent_workflow, batch_instance_count)
同時実行の最大本数   = min(task_count, batch_max_parallelism または デフォルト)
```

運用の目安:
- 総投入数を増やしたい → `concurrent_workflow` を増やす。
- ピーク負荷やリソースを抑えたい → `batch_max_parallelism` で上限を小さく設定する。
- 案件ごとに細かく調整したい場合は targeting シートで制御し、全案件で一律上限を変えたい場合は Script Property `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT` を調整してください。

---

## 9. 変更管理の流れ（例）

1. 変更内容をローカルで検証 (`.env` を使って Playwright 実行やユニットテストを実施)。
2. Cloud Build トリガーで `dispatcher` / `playwright` イメージを更新。
3. Cloud Run dispatcher を再デプロイし、`/healthz` で動作確認。
4. テスト対象 targeting で Dry Run。
5. 問題なければ GAS targeting シートや Script Properties を更新して本番反映。

---

## 10. targeting 単位でのキャンセル運用

### 10.1 GAS から dispatcher 経由で停止する手順

1. GAS の `stopSpecificFormSenderTask(targetingId)` を呼び出すと、`CloudRunDispatcherClient.listRunningExecutions(targetingId)` が `targeting_id` でフィルタした実行一覧を取得します。
2. 返却された `execution_id` ごとに dispatcher の `/v1/form-sender/executions/{id}/cancel` API を呼び出します。GAS 側では `CloudRunDispatcherClient.cancelExecution()` で処理されています。
3. dispatcher は Supabase `job_executions` から該当レコードを読み込み、`metadata.execution_mode` に応じて以下を実行します。
   - **Batch 実行**: `metadata.batch.job_name` を使って Cloud Batch `projects.locations.jobs.delete` を呼び出し、ジョブを停止。
   - **Cloud Run 実行**: `metadata.cloud_run.execution` / `cloud_run_operation` を使って Cloud Run の Execution をキャンセル。
4. 停止に成功すると Supabase のステータスが `cancelled` に更新され、GAS 側のレスポンスにもキャンセル結果が含まれます。

> 実装詳細: `gas/form-sender/TaskControl.gs`（`stopSpecificFormSenderTaskServerless_`）、`gas/form-sender/CloudRunDispatcherClient.gs`、`src/dispatcher/service.py`（`cancel_execution`）。

### 10.2 Cloud Batch ジョブの識別

- dispatcher が Batch ジョブを生成する際、`labels` に `workload=form_sender` と `targeting_id=<数値>` を付与しています。GCP コンソールや `gcloud batch jobs list --filter="labels.targeting_id=<ID>"` で該当ジョブを手動確認できます。
- Supabase `job_executions.metadata.batch.job_name` には Cloud Batch ジョブ名が保存されており、監視ダッシュボードや GAS ログからジョブを突き止めたい場合に利用できます。

### 10.3 Cloud Tasks キュー上のタスクを取り消す場合（オプション）

- dispatcher 呼び出し前（Cloud Tasks に積まれただけ）のジョブを取り消したい場合は、`CloudRunDispatcherClient.enqueue()` が返す `task.name` をどこかに保持しておき、キャンセル時に `cloudtasks.tasks.delete` を呼び出す拡張を追加します。
- enqueue 時点で生成されるタスク ID は `fs-YYYYMMDD-<targetingId>-<runIndex>` 形式で `targeting_id` が含まれるため、ターゲティング単位での突合が容易です。現在の実装では保持していないため、必要に応じて Supabase などに記録する想定で検討してください。

---

運用中に不明点があれば、セットアップガイドおよび `src/dispatcher` 配下のコードに付属する docstring を参照してください。
