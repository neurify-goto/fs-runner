# Form Sender サーバーレス移行セットアップ & 運用ガイド

最終更新: 2025-10-03 (JST)
対象範囲: GAS `form-sender` / Cloud Tasks / Cloud Run Job (dispatcher + runner) / Supabase / GitHub Actions フォールバック

---

## 1. 背景とゴール
- GitHub Actions 依存のフォーム送信ワークフローを段階的に Cloud Tasks → Cloud Run Jobs → Supabase のサーバーレス基盤へ移行する。
- 既存の GAS スケジューラと Supabase データ構造を維持したまま、マルチワーカー／シャーディング挙動を再現する。
- テストモード・ブランチ検証 (form_sender_test / manual) を本番テーブルから分離し、`send_queue_test` / `submissions_test` を利用する。
- 移行期間中は feature flag (`USE_SERVERLESS_FORM_SENDER`) で GitHub Actions とサーバーレス経路を切り替え可能にする。

### 1.1 セットアップ開始前チェックリスト
初心者の方でも迷わず準備できるよう、以下の前提をすべて満たしてから次章へ進んでください。

1. **GCP プロジェクト**
   - 課金が有効であることを [Google Cloud Console](https://console.cloud.google.com/billing) で確認。
   - 利用するプロジェクト ID をメモ（例: `fs-prod-001`）。
2. **Supabase プロジェクト**
   - Supabase ダッシュボードにログインし、対象プロジェクトの URL (`https://<project>.supabase.co`) と Service Role Key を控える。
   - 本番・テストを分離する場合はプロジェクトを分けるか、Service Role Key を環境変数で分離する運用にする。
3. **ローカル開発環境**
   - `gcloud` CLI（Google Cloud SDK）をインストールし `gcloud version` で確認。未インストールなら [Google Cloud SDK インストール手順](https://cloud.google.com/sdk/docs/install) を参照。
   - Docker が利用可能であること (`docker --version`)。Cloud Build を使う場合でもローカルでの動作確認に役立ちます。
   - Supabase CLI または `psql` を利用できると SQL 適用が容易になります（`brew install supabase/tap/supabase` など）。
4. **アクセス権限**
   - GCP 側で Owner または以下のロールを付与済み: `roles/run.admin`, `roles/cloudtasks.admin`, `roles/secretmanager.admin`, `roles/iam.serviceAccountAdmin`, `roles/storage.admin`。
   - Supabase 側で SQL Editor を使用できるロールを所持していること。
5. **環境変数メモ**
   - 以下の値をまとめておくと後続のコマンドで迷いません: `PROJECT_ID`, `REGION` (推奨: `asia-northeast1`), `ARTIFACT_REGISTRY_REPO`, `DISPATCHER_SERVICE_ACCOUNT`, `JOB_SERVICE_ACCOUNT`。

> 💡 **TIP**: 作業中に混乱しないよう、これらの値を `.env.serverless` などのファイルに控えておくと便利です。

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

### 3.1 Supabase ダッシュボードでの SQL 適用手順
1. Supabase ダッシュボードにアクセスし、対象プロジェクトを選択。
2. 左メニューの **SQL Editor** を開き、「New query」をクリック。
3. `scripts/table_schema/job_executions.sql` の内容をコピーして貼り付け、「Run」を実行。
4. 同様に `send_queue_test.sql`、`submissions_test.sql` を順に実行。
5. 画面上部の `Saved queries` に保存しておくと、再実行時に便利です。
6. 次に `scripts/functions/` 以下の各 SQL を同じ手順で実行し、成功メッセージ（`SUCCESS`）が表示されることを確認します。

### 3.2 CLI での一括適用例
CLI を使用する場合は、以下のように `psql` または Supabase CLI で一括適用できます。`<SUPABASE_DB_URL>` には Supabase プロジェクトの `postgresql://` 接続文字列を指定してください。

```bash
# 例: psql で DDL を一括適用
export SUPABASE_DB_URL="postgresql://postgres:<password>@db.<project>.supabase.co:5432/postgres"
psql "$SUPABASE_DB_URL" -f scripts/table_schema/job_executions.sql
psql "$SUPABASE_DB_URL" -f scripts/table_schema/send_queue_test.sql
psql "$SUPABASE_DB_URL" -f scripts/table_schema/submissions_test.sql

# RPC 群を適用
for file in scripts/functions/create_queue_for_targeting_step_test.sql \
            scripts/functions/create_queue_for_targeting_test.sql \
            scripts/functions/claim_next_batch_test.sql \
            scripts/functions/mark_done_test.sql \
            scripts/functions/reset_send_queue_all_test.sql \
            scripts/functions/requeue_stale_assigned_test.sql; do
  psql "$SUPABASE_DB_URL" -f "$file"
done
```

> ⚠️ **注意**: Supabase の Service Role Key は強力な権限を持つため、ローカル環境で環境変数に設定した後は必ず `unset` してください。

### 3.3 Supabase ロール権限の設定例
1. ダッシュボード左メニューの **Authentication → Policies** から、`job_executions` テーブルにアクセス。
2. `Enable RLS` が有効になっている場合、Cloud Run から参照できるように Service Role を利用するか、ポリシーを追加してください。
3. Cloud Run / dispatcher で使用する Service Role Key は **Project Settings → API** の `Service Role` からコピーします。テスト環境用のキーが必要な場合は `Service Role (anon, service_role)` を使い分けるか、別プロジェクトを用意します。

---

## 4. Cloud Run Job (Runner) セットアップ

### 4.1 有効化しておくべき GCP API
```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudtasks.googleapis.com
```

### 4.2 コンテナイメージのビルドと登録
`REGION` は Cloud Run を稼働させたいリージョン（推奨: `asia-northeast1`）。リポジトリは Artifact Registry のリポジトリ名です。

```bash
export PROJECT_ID="fs-prod-001"
export REGION="asia-northeast1"
export REPO="form-sender"

# Artifact Registry リポジトリ作成（初回のみ）
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Form Sender runner images" || true

# Docker ビルド＆プッシュ
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/form-sender-runner:latest"
docker build -t "$IMAGE" .
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
docker push "$IMAGE"
```

> ✅ Cloud Build を使いたい場合は `gcloud builds submit --tag "$IMAGE" .` でも同等です。

### 4.3 Cloud Run Job の作成/更新
Cloud Run Job 用のサービスアカウント (`form-sender-runner@<project>.iam.gserviceaccount.com` など) を用意し、`roles/run.invoker`, `roles/storage.objectViewer`, `roles/storage.objectAdmin` を付与しておきます。

```bash
export JOB_SERVICE_ACCOUNT="form-sender-runner@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud run jobs deploy form-sender-runner \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$JOB_SERVICE_ACCOUNT" \
  --set-env-vars=FORM_SENDER_ENV=cloud_run,FORM_SENDER_LOG_SANITIZE=1 \
  --set-secrets=SUPABASE_URL=projects/${PROJECT_ID}/secrets/SUPABASE_URL:latest,\
SUPABASE_SERVICE_ROLE_KEY=projects/${PROJECT_ID}/secrets/SUPABASE_SERVICE_ROLE_KEY:latest \
  --task-timeout=3600s \
  --max-retries=3 \
  --cpu=4 \
  --memory=14Gi
```

> `--execute-now` でデプロイ直後に試験実行できます。安定稼働前にジョブのログを確認してください。

### 4.4 既定環境変数の推奨値
| 変数 | 推奨値 | 説明 |
|------|--------|------|
| `FORM_SENDER_ENV` | `cloud_run` | ランタイム識別 |
| `FORM_SENDER_LOG_SANITIZE` | `1` | ログマスク有効 |
| `FORM_SENDER_MAX_WORKERS` | `4` | 1タスク上限（dispatcherからの上書きを許可） |
| `FORM_SENDER_TOTAL_SHARDS` | `8` | fallback 値（dispatcher/GAS で上書き） |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Secret Manager | RPC 用。`--set-secrets` で注入 |
| `SUPABASE_URL_TEST` / `SUPABASE_SERVICE_ROLE_KEY_TEST` | Secret Manager | `FORM_SENDER_TEST_MODE=true` 時に使用 |

### 4.5 dispatcher から渡される環境変数
Cloud Run Job 実行時には、dispatcher から以下が注入されます。手動実行時は `gcloud run jobs execute --set-env-vars` で明示してください。

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

### 5.1 依存ライブラリ
`requirements.txt` に追加済みですが、ローカルで FastAPI を起動して確認する場合は仮想環境を作成して以下をインストールします。

- `google-cloud-tasks`
- `google-cloud-run`
- `google-cloud-storage`
- `google-cloud-secret-manager`
- `fastapi`, `uvicorn`

### 5.2 Secret Manager に Supabase キーを登録

```bash
gcloud secrets create SUPABASE_URL --replication-policy=automatic || true
gcloud secrets create SUPABASE_SERVICE_ROLE_KEY --replication-policy=automatic || true

echo -n "https://<project>.supabase.co" | gcloud secrets versions add SUPABASE_URL --data-file=-
echo -n "<service-role-key>" | gcloud secrets versions add SUPABASE_SERVICE_ROLE_KEY --data-file=-

# テスト環境を分ける場合（任意）
gcloud secrets create SUPABASE_URL_TEST --replication-policy=automatic || true
echo -n "https://<project-test>.supabase.co" | gcloud secrets versions add SUPABASE_URL_TEST --data-file=-

gcloud secrets create SUPABASE_SERVICE_ROLE_KEY_TEST --replication-policy=automatic || true
echo -n "<service-role-key-test>" | gcloud secrets versions add SUPABASE_SERVICE_ROLE_KEY_TEST --data-file=-
```

> PAT を使ってブランチ検証を行う場合は `FORM_SENDER_GIT_TOKEN_SECRET` というシークレットを同様に登録してください。

### 5.3 dispatcher 用サービスアカウントの作成

```bash
export DISPATCHER_SERVICE_ACCOUNT="form-sender-dispatcher@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create form-sender-dispatcher \
  --project="$PROJECT_ID" \
  --description="Form Sender dispatcher service"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DISPATCHER_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DISPATCHER_SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DISPATCHER_SERVICE_ACCOUNT}" \
  --role="roles/storage.objectAdmin"
```

Cloud Tasks から dispatcher を呼び出す OIDC 用サービスアカウントを分けたい場合は、同様に `form-sender-tasks@` などを作成し `roles/iam.serviceAccountTokenCreator` を付与します。

### 5.4 Cloud Tasks Queue の作成

```bash
export TASKS_QUEUE="projects/${PROJECT_ID}/locations/${REGION}/queues/form-sender-tasks"

gcloud tasks queues create form-sender-tasks \
  --location="$REGION" \
  --max-attempts=3 \
  --min-backoff=60s \
  --max-backoff=600s \
  --max-dispatches-per-minute=30 || true
```

### 5.5 Cloud Run Service のデプロイ

まずは dispatcher 用コンテナの Dockerfile を用意します（例）。

```Dockerfile
# ファイル名例: Dockerfile.dispatcher
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Tokyo

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn[standard] google-cloud-tasks google-cloud-run google-cloud-storage google-cloud-secret-manager \
    && pip install --no-cache-dir -r requirements.txt

COPY src/ src/

CMD ["uvicorn", "dispatcher.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

ビルドからデプロイまでは以下のコマンドで実施します。

```bash
IMAGE_DISPATCHER="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/form-sender-dispatcher:latest"

docker build -t "$IMAGE_DISPATCHER" -f Dockerfile.dispatcher .
docker push "$IMAGE_DISPATCHER"

gcloud run deploy form-sender-dispatcher \
  --image="$IMAGE_DISPATCHER" \
  --region="$REGION" \
  --service-account="$DISPATCHER_SERVICE_ACCOUNT" \
  --allow-unauthenticated=false \
  --set-env-vars=DISPATCHER_PROJECT_ID=${PROJECT_ID},DISPATCHER_LOCATION=${REGION},FORM_SENDER_CLOUD_RUN_JOB=form-sender-runner \
  --set-secrets=DISPATCHER_SUPABASE_URL=projects/${PROJECT_ID}/secrets/SUPABASE_URL:latest,\
DISPATCHER_SUPABASE_SERVICE_ROLE_KEY=projects/${PROJECT_ID}/secrets/SUPABASE_SERVICE_ROLE_KEY:latest \
  --max-instances=3 \
  --cpu=1 \
  --memory=1Gi
```

> 既存の CI/CD でビルドする場合は `gcloud builds submit --tag "$IMAGE_DISPATCHER" -f Dockerfile.dispatcher .` を用いても構いません。

### 5.6 dispatcher の環境変数 (`DispatcherSettings.from_env`)
| 変数 | 説明 |
|------|------|
| `DISPATCHER_PROJECT_ID` | Cloud Run Job 配置先 GCP プロジェクト |
| `DISPATCHER_LOCATION` | Job/Queue のリージョン（例: `asia-northeast1`） |
| `FORM_SENDER_CLOUD_RUN_JOB` | `form-sender-runner` |
| `DISPATCHER_SUPABASE_URL` / `DISPATCHER_SUPABASE_SERVICE_ROLE_KEY` | `job_executions` テーブルへの接続 |
| `FORM_SENDER_CLIENT_CONFIG_BUCKET` | client_config 保存用バケット（任意、設定時は StorageClient で検証） |
| `FORM_SENDER_SIGNED_URL_TTL_HOURS` | 署名URL TTL (既定 15h) |
| `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD` | 残り秒数閾値 (既定 1800s) |
| `FORM_SENDER_GIT_TOKEN_SECRET` | ブランチテスト用 PAT を Secret Manager から取得する際のリソース名 |

### 5.7 Cloud Tasks から dispatcher を呼び出す設定
- Cloud Run コンソールで `form-sender-dispatcher` の URL をコピー。
- Cloud Tasks からの HTTP タスクで OIDC トークンを付与するため、`FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT` に `roles/iam.serviceAccountTokenCreator` を付与。
- GAS 側で Script Properties に `FORM_SENDER_DISPATCHER_URL` と `FORM_SENDER_TASKS_QUEUE` を設定します（詳しくは §6 を参照）。

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

### 6.4 Script Properties の設定手順（GAS UI）
1. Google Apps Script エディタで `form-sender` プロジェクトを開く。
2. 右上の **歯車アイコン → プロジェクトの設定 → スクリプト プロパティ** を開く。
3. `追加` ボタンから上記 `Script Properties` の値を入力。複数行にわたる JSON やサービスアカウントキーは貼り付け前に整形しておく。
4. `SERVICE_ACCOUNT_JSON` を設定する際は、GCP で生成した JSON キーファイルを開き `replace(/\n/g, "\\n")` を実行してから貼り付ける。
5. `USE_SERVERLESS_FORM_SENDER` を `true` にして保存すると、次回トリガーから Cloud Tasks 経由になります。

### 6.5 Cloud Tasks 連携の動作確認
1. GAS エディタから `testFormSenderWorkflowTrigger()` を実行。
2. 実行ログに Cloud Tasks のレスポンス（`taskId` や duplicate 判定）が出力されることを確認。
3. GCP コンソールの **Cloud Tasks → form-sender-tasks** でタスクが `dispatching` → `completed` になる流れをチェック。
4. 問題があれば `FORM_SENDER_DISPATCHER_URL` やサービスアカウントの権限を再確認します。

### 6.6 GAS 用サービスアカウントの権限付与例
`SERVICE_ACCOUNT_JSON` に設定するサービスアカウント（例: `form-sender-gas@${PROJECT_ID}.iam.gserviceaccount.com`）には、以下のロールを付与しておきます。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:form-sender-gas@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/cloudtasks.enqueuer"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:form-sender-gas@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:form-sender-gas@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

> GAS から署名付き URL を発行する場合は対象バケットに対して `roles/storage.objectCreator` も必要です。

---

## 7. GitHub Actions フォールバック
- `form-sender.yml` は `FORM_SENDER_ENV=github_actions` を設定済み。
- serverless フラグ OFF の場合は従来通り Repository Dispatch → `/tmp/client_config_*.json` → Python Runner を実行。
- ロールバック手順: Script Properties `USE_SERVERLESS_FORM_SENDER=false` に戻す。

---

## 8. テスト・検証フロー
### 8.1 単体テスト
ローカルで Python テストを実行し、主要コンポーネントの動作を確認します。

```bash
PYTHONPATH=src pytest \
  tests/test_env_utils.py \
  tests/test_client_config_validator.py \
  tests/test_dispatcher_internals.py \
  tests/test_form_sender_job_entry.py \
  tests/test_form_sender_runner.py
```

失敗したテストがある場合は該当モジュールの環境変数や依存ライブラリを確認してください。

### 8.2 ステージング検証
1. GAS Script Properties で `USE_SERVERLESS_FORM_SENDER=true` を設定し、検証したい targeting のみに `useServerless=true` を付与。
2. Supabase `job_executions` テーブルでステータスが `running` → `succeeded` になることを確認。
3. Cloud Run Job のログ (`gcloud run jobs executions logs read form-sender-runner --region=${REGION}`) をチェックし、Playwright のエラーや Supabase 連携エラーが出ていないか確認。
4. 問題があれば `reset_send_queue_all_test` でテストテーブルを初期化し、再実行します。

### 8.3 手動テスト
1. GAS エディタから `testFormSenderOnBranch('feature/xxx', <targetingId>)` を実行し、ブランチ単位の検証を行う。
2. dispatcher 経由で test テーブルへ送信されるため、Supabase の `submissions_test` を確認し、期待どおり登録されているかチェック。
3. 必要に応じて Cloud Run Job を `gcloud run jobs execute form-sender-runner --args=...` で手動実行し、環境変数の差異を確認します。

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

### 11.1 ログ確認コマンド早見表
- **Cloud Run Job**: `gcloud run jobs executions logs read form-sender-runner --region=${REGION} --limit=50`
- **Cloud Run Service (dispatcher)**: `gcloud run services logs read form-sender-dispatcher --region=${REGION}`
- **Cloud Tasks**: `gcloud tasks tasks list --queue=form-sender-tasks --location=${REGION}` で未処理タスクを確認。
- **Supabase**: `job_executions` テーブルを `select * from job_executions order by started_at desc limit 20;` で参照し、`status` と `metadata` を確認。
- **GAS**: Apps Script ダッシュボードの実行ログ、または Stackdriver ログ（`resource.type="app_script_function"`）で `startFormSenderFromTrigger` の出力を見る。

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
