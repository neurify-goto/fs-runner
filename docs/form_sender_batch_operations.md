# Form Sender Batch 運用ガイド

GAS → Cloud Tasks → Cloud Run dispatcher → Cloud Batch の本番ルートを安定運用するための手順とチェックポイントをまとめたドキュメントです。セットアップ手順は `docs/form_sender_gcp_batch_setup_guide.md` を参照してください。

## 1. 日次・定期チェック

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

## 2. 手動オペレーション

### 2.1 テスト実行（Dry Run）
- GAS エディタから `triggerFormSenderWorkflow(targetingId, { testMode: true })` を実行。
- Supabase `job_executions` に `execution_mode=batch` でレコードが追加され、Cloud Batch で `RUNNING` → `SUCCEEDED` となることを確認。

### 2.2 ジョブの停止
- GAS エディタ：`stopSpecificFormSenderTask(targetingId)`。
- もしくは CLI：
  ```bash
  gcloud batch jobs delete <job-name> --location=asia-northeast1 --project=formsalespaid
  ```
- Supabase 側で `status=cancelled` に更新されているか確認。

### 2.3 Cloud Tasks の手動投入
- Cloud Console → **Cloud Tasks** → `form-sender-dispatcher` → **タスクを作成**。
- 送信先 URL: `https://<dispatcher-url>/v1/form-sender/tasks`
- OIDC サービスアカウント: `form-sender-dispatcher@formsalespaid.iam.gserviceaccount.com`
- リクエスト本文に GAS から送られる Payload を貼り付けてテスト可能。

## 3. 監視とアラート推奨事項

| 項目 | 推奨設定 |
| --- | --- |
| Cloud Batch 失敗ジョブ | `job.status=FAILED` が連続した場合にメール通知 |
| Cloud Run dispatcher エラー | `severity>=ERROR` を条件に Log-based Alert |
| Cloud Tasks 遅延 | `Queue latency > 300 秒` でアラート |
| Supabase 異常滞留 | `job_executions` の `pending` が一定件数を超えたら通知 |

## 4. トラブルシューティング例

| 症状 | 確認ポイント | 対応 |
| --- | --- | --- |
| Cloud Run dispatcher が起動しない | Cloud Logging で `RuntimeError: 環境変数 ...` を検索 | `docs/form_sender_gcp_batch_setup_guide.md` の 6.3 節を参照し、環境変数・シークレットを再設定 |
| Batch ジョブが `FAILED` を繰り返す | `job_executions.metadata.batch` の `memory_warning`、`machine_type` を確認 | targeting の `batch_memory_per_worker_mb`/`batch_machine_type` を増やす |
| Supabase に `execution_mode=cloud_run` が残る | GAS Script Properties `USE_GCP_BATCH` などを確認 | targeting 側フラグが `false` の場合は切り替え漏れ。GAS Property と targeting を更新 |
| Cloud Tasks で 401/403 | ターゲット URL または OIDC サービスアカウントの権限不足 | Cloud Run dispatcher の URL と SA (`form-sender-dispatcher@...`) に `roles/run.invoker` が設定されているか確認 |

## 5. 変更管理の流れ（例）

1. 変更内容をローカルで検証 (`.env` を使って Playwright 実行やユニットテストを実施)。
2. Cloud Build トリガーで `dispatcher` / `playwright` イメージを更新。
3. Cloud Run dispatcher を再デプロイし、`/healthz` で動作確認。
4. テスト対象 targeting で Dry Run。
5. 問題なければ GAS targeting シートや Script Properties を更新して本番反映。

---

運用中に不明点があれば、セットアップガイドおよび `src/dispatcher` 配下のコードに付属する docstring を参照してください。
