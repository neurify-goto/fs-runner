# Form Sender サーバーレス移行セットアップ & 運用ガイド

最終更新: 2025-10-03 (JST)
対象範囲: GAS `form-sender` / Cloud Tasks / Cloud Run Job (dispatcher + runner) / Supabase / GitHub Actions フォールバック

---

## 1. 背景とゴール
- GitHub Actions 依存のフォーム送信ワークフローを段階的に Cloud Tasks → Cloud Run Jobs → Supabase のサーバーレス基盤へ移行する。
- 既存の GAS スケジューラと Supabase データ構造を維持したまま、マルチワーカー／シャーディング挙動を再現する。
- テストモード・ブランチ検証 (form_sender_test / manual) を本番テーブルから分離し、`send_queue_test` / `submissions_test` を利用する。
- 移行期間中は feature flag (`USE_SERVERLESS_FORM_SENDER`) で GitHub Actions とサーバーレス経路を切り替え可能にする。

---

## 2. システム構成概要
1. **GAS (Apps Script)**
   - 時間トリガー `startFormSenderFromTrigger` が targeting 行を取得。
   - client_config を GCS にアップロードし、Cloud Tasks に dispatcher 呼び出しタスクを enqueue。
   - Script Properties で並列数・シャード数等を制御。
2. **Cloud Tasks**
   - Queue: `FORM_SENDER_TASKS_QUEUE` (`projects/<project>/locations/<region>/queues/<queue>`)
   - OIDC トークン付き HTTP 呼び出しで dispatcher Service を起動。
3. **Cloud Run Service (dispatcher)**
   - FastAPI ベース。
   - payload 検証 → 署名 URL 更新 → Cloud Run Job `RunJobRequest` 発行。
   - Supabase `job_executions` テーブルへ実行メタを INSERT。
4. **Cloud Run Job (form-sender-runner)**
   - Dockerfile に Playwright / 依存ライブラリを同梱。
   - エントリポイント `bin/form_sender_job_entry.py` が client_config を取得し、`form_sender_runner.py` を起動。
   - 環境変数経由で shard / table mode / run_id を渡す。
5. **Supabase**
   - RPC: `create_queue_for_targeting[_extra/_test]`, `claim_next_batch[_extra/_test]`, `mark_done[_extra/_test]`, `reset_send_queue_all[_extra/_test]`。
   - 新規テーブル: `job_executions`, `send_queue_test`, `submissions_test`。
6. **GitHub Actions (フォールバック)**
   - `form-sender.yml` は `FORM_SENDER_ENV=github_actions` 設定で既存挙動維持。

---

## 3. Supabase 事前準備
1. **DDL 適用** (`scripts/table_schema/`)
   - `job_executions.sql`
   - `send_queue_test.sql`
   - `submissions_test.sql`
2. **RPC / Function**
   - `create_queue_for_targeting_test`
   - `create_queue_for_targeting_step_test`
   - `claim_next_batch_test`
   - `mark_done_test`
   - `reset_send_queue_all_test`
   - `requeue_stale_assigned_test`
3. **ロール権限**
   - Cloud Run Job/dispatcher に使用する Service Role Key が上記テーブル・関数へアクセス可能であること。

---

## 4. Cloud Run Job (Runner) セットアップ
1. **Docker イメージ**
   ```bash
   docker build -t asia-northeast1-docker.pkg.dev/<project>/form-sender/form-sender-runner:latest .
   docker push asia-northeast1-docker.pkg.dev/<project>/form-sender/form-sender-runner:latest
   ```
2. **Cloud Run Job 作成/更新**
   - `gcloud run jobs update form-sender-runner \
       --image=asia-northeast1-docker.pkg.dev/<project>/form-sender/form-sender-runner:latest \
       --region=asia-northeast1 \
       --no-cpu-throttling`
3. **既定環境変数**
   | 変数 | 推奨値 | 説明 |
   |------|--------|------|
   | `FORM_SENDER_ENV` | `cloud_run` | ランタイム識別 |
   | `FORM_SENDER_LOG_SANITIZE` | `1` | ログマスク有効 |
   | `FORM_SENDER_MAX_WORKERS` | `4` | 1タスク上限 |
   | `FORM_SENDER_TOTAL_SHARDS` | `8` | fallback 値（GAS/dispatcher から上書き） |
   | `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Secret Manager | RPC 用 |
   | `SUPABASE_URL_TEST` / `SUPABASE_SERVICE_ROLE_KEY_TEST` | Secret Manager | `FORM_SENDER_TEST_MODE=true` 時に使用するテスト環境 |
4. **実行時に dispatcher から渡される env**
   - `FORM_SENDER_CLIENT_CONFIG_URL`
   - `FORM_SENDER_CLIENT_CONFIG_PATH`
   - `FORM_SENDER_TOTAL_SHARDS`
   - `FORM_SENDER_WORKFLOW_TRIGGER`
   - `FORM_SENDER_TARGETING_ID`
   - `FORM_SENDER_TEST_MODE`
   - `JOB_EXECUTION_ID`
   - `JOB_EXECUTION_META`（Base64 JSON: `run_index_base`, `shards`, `workers_per_workflow`, `test_mode`）
   - `FORM_SENDER_GIT_REF` / `FORM_SENDER_GIT_TOKEN`（ブランチテスト）
   - `COMPANY_TABLE` / `SEND_QUEUE_TABLE` / `SUBMISSIONS_TABLE` / `FORM_SENDER_TABLE_MODE`

---

## 5. Cloud Run Service (dispatcher) デプロイ
1. **依存ライブラリ**
   - `google-cloud-tasks`
   - `google-cloud-run`
   - `google-cloud-storage`
   - `google-cloud-secret-manager`
   - `fastapi`, `uvicorn`
2. **環境変数 (`DispatcherSettings.from_env`)**
   | 変数 | 説明 |
   |------|------|
   | `DISPATCHER_PROJECT_ID` | Cloud Run Job 配置先 GCP プロジェクト |
   | `DISPATCHER_LOCATION` | Job/Queue のリージョン（例: `asia-northeast1`） |
   | `FORM_SENDER_CLOUD_RUN_JOB` | `form-sender-runner` |
   | `DISPATCHER_SUPABASE_URL` / `DISPATCHER_SUPABASE_SERVICE_ROLE_KEY` | job_executions への接続 |
   | `FORM_SENDER_CLIENT_CONFIG_BUCKET` | client_config 保存用バケット（任意） |
   | `FORM_SENDER_SIGNED_URL_TTL_HOURS` | 署名URL TTL (既定 15h) |
   | `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD` | 残り秒数閾値 (既定 1800s) |
   | `FORM_SENDER_GIT_TOKEN_SECRET` | Secret Manager リソース名（ブランチテスト用 PAT） |
3. **Cloud Tasks Queue**
   - `gcloud tasks queues create <queue> --max-attempts=3 --min-backoff=60 --max-backoff=600`
   - dispatch URL を OIDC 署名付きで受ける設定。
4. **Secret Manager 権限**
   - dispatcher サービスアカウントに `roles/secretmanager.secretAccessor`
   - Cloud Run Job サービスアカウントに `roles/storage.objectViewer`, `roles/storage.objectAdmin` (client_config 再署名用) 等。

---

## 6. GAS 側設定
### 6.1 ファイル配置
- `gas/form-sender/Code.gs`
- `CloudRunDispatcherClient.gs`
- `StorageClient.gs`
- `ServiceAccountClient.gs`

### 6.2 Script Properties
| キー | 用途 |
|------|------|
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | 従来通り |
| `USE_SERVERLESS_FORM_SENDER` | `true` で Cloud Tasks 経路有効 |
| `FORM_SENDER_GCS_BUCKET` | client_config アップロード先 |
| `FORM_SENDER_TASKS_QUEUE` | Cloud Tasks Queue パス |
| `FORM_SENDER_DISPATCHER_URL` | dispatcher endpoint |
| `FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT` | Cloud Tasks OIDC 用 SA |
| `SERVICE_ACCOUNT_JSON` | GCS アップロード用サービスアカウントキー（`private_key` を `\n` 変換済） |
| `FORM_SENDER_SHARD_COUNT` | 既定シャード数（例: `8`） |
| `FORM_SENDER_PARALLELISM_OVERRIDE` | 同時タスク数オーバーライド（任意） |
| `FORM_SENDER_WORKERS_OVERRIDE` | 1タスクあたりワーカー数オーバーライド（任意） |

### 6.3 テストモードの指針
- `options.testMode === true` の場合、GAS は `send_queue_test` を生成し、dispatcher へ `submissions_test` を通知します。
- ブランチテスト (`testFormSenderOnBranch`)・手動テスト (`testFormSenderWorkflowTrigger`) も自動的に test テーブルへルーティング。
- クリーンアップ用に `reset_send_queue_all_test` RPC を適宜呼び出す（`resetSendQueueAllTest()` 実装済みか要確認）。

---

## 7. GitHub Actions フォールバック
- `form-sender.yml` は `FORM_SENDER_ENV=github_actions` を設定済み。
- serverless フラグ OFF の場合は従来通り Repository Dispatch → `/tmp/client_config_*.json` → Python Runner を実行。
- ロールバック手順: Script Properties `USE_SERVERLESS_FORM_SENDER=false` に戻す。

---

## 8. テスト・検証フロー
1. **単体テスト**
   ```bash
   PYTHONPATH=src pytest tests/test_env_utils.py tests/test_client_config_validator.py tests/test_dispatcher_internals.py
   ```
2. **ステージング検証**
   - Script Properties で `USE_SERVERLESS_FORM_SENDER=true`、対象 targeting のみ ON。
   - Supabase `job_executions` で実行ログを確認。
3. **手動テスト**
   - GAS `testFormSenderOnBranch('feature/xxx', <id>)`
   - dispatcher は test テーブルへ送信。送信結果は `submissions_test` を確認。

---

## 9. 運用チェックリスト
- [ ] Supabase DDL・RPC を適用済みか
- [ ] Cloud Run Job イメージの最新タグを反映したか
- [ ] dispatcher サービスアカウントに Secret / Tasks / Run Jobs 権限があるか
- [ ] GAS Script Properties を設定し `USE_SERVERLESS_FORM_SENDER=true` で起動確認したか
- [ ] Supabase `job_executions` に実行記録が作成されるか
- [ ] Cloud Tasks の失敗リトライが 3 回で止まるか
- [ ] Playwright の依存キャッシュ (`/ms-playwright`) が適切か

---

## 10. 監視・アラート
- **Supabase**: `job_executions.status` を監視（`failed`/`cancelled` を通知）。
- **Cloud Tasks**: 隊列長・失敗率。
- **Cloud Run Job**: Execution 成功率、再試行回数、ログ（`form_sender.lifecycle`）。
- **GAS**: Stackdriver ログで enqueue 成否と署名 URL エラーを追跡。

---

## 11. トラブルシューティング
| 症状 | 確認ポイント |
|------|--------------|
| Cloud Tasks が `PERMISSION_DENIED` | `FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT` に `roles/run.invoker` と `roles/cloudtasks.enqueuer` が付与されているか |
| dispatcher が 422 | client_config の署名 URL 失効 → GCS オブジェクト権限・時計ずれ確認 |
| runner が shard を 8 固定で扱う | `FORM_SENDER_TOTAL_SHARDS` と `JOB_EXECUTION_META` の `shards` が設定されているか |
| test 実行で本番テーブル更新 | GAS `buildSendQueueForTargeting` の `testMode` ルートが有効化されているか、Supabase の test RPC 有無をチェック |
| client_config 保存失敗 | Service Account JSON の `private_key` フォーマット (`\n`) を確認 |

---

## 12. フィーチャーフラグ運用
1. GAS Script Properties: `USE_SERVERLESS_FORM_SENDER`
2. 切替手順
   - `false` → GitHub Actions 経路
   - `true` → Cloud Tasks 経路
3. 部分適用したい場合は targeting 行に `useServerless` 列を追加し、スクリプト内の条件分岐で制御（既存コードの `cfg.useServerless` 参照）。

---

## 13. 参考情報
- 設計計画書: `docs/form_sender_serverless_migration_plan.md`
- Cloud Run / Tasks / Supabase の CLI コマンド例は `scripts/` ディレクトリ参照
- Playwright 導入手順: `requirements.txt` + Dockerfile 内コメント

---

以上。
