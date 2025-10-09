# Form Sender GCP Batch セットアップガイド（初心者向け）

最終更新: 2025-10-09 (JST)  
対象範囲: GAS `form-sender` / Cloud Tasks / Cloud Run Dispatcher / Cloud Batch Runner / Supabase / GitHub Actions

---

## 1. 目的とゴール

- Playwright ランナーを **Cloud Batch (Spot VM 優先)** で動かし、GitHub Actions 依存を解消する。  
- 既存の GAS → Cloud Tasks → Cloud Run dispatcher の呼び出し経路を維持しつつ、Cloud Batch へのジョブ投入を追加。  
- Supabase `job_executions` メタデータを Cloud Batch 対応 (attempt / preempted / machine_type など) に拡張する。  
- 既存の Cloud Run Jobs (サーバーレス) 経路はフォールバックとして温存し、Script Property `USE_GCP_BATCH` で段階的に切り替えられるようにする。

はじめてセットアップする方でも迷わないよう、以下の各章を順番に完了してください。

---

## 2. 事前準備チェックリスト

| No | 項目 | 確認方法 |
| --- | --- | --- |
| 1 | **Google Cloud プロジェクト** | Billing が有効化済みかを Cloud Console → Billing で確認。Project ID を控える (例 `fs-prod-001`) |
| 2 | **Supabase プロジェクト** | ダッシュボードにログインし、URL (`https://<project>.supabase.co`) と Service Role Key をメモ。テスト用プロジェクトを切り分ける場合は両方控える |
| 3 | **CLI/ツール** | `gcloud version`, `docker --version`, `terraform -version`, `python --version` が全て実行できる。必要に応じて `brew install terraform` などで導入 |
| 4 | **GCP 権限** | 対象プロジェクトで以下ロールを保持: `roles/run.admin`, `roles/iam.serviceAccountAdmin`, `roles/batch.admin`, `roles/artifactregistry.admin`, `roles/secretmanager.admin`, `roles/storage.admin`, `roles/cloudtasks.admin`, `roles/logging.admin` |
| 5 | **Supabase 権限** | SQL Editor でスキーマ更新可能なロールを保持 (`Owner` もしくは Service Role Key) |
| 6 | **ローカル環境変数メモ** | `PROJECT_ID`, `REGION` (推奨 `asia-northeast1`), `ARTIFACT_REPO`, `DISPATCHER_BASE_URL`, `DISPATCHER_AUDIENCE` などを `.env.gcp_batch` として控える |

> 💡 **TIP**: 4〜6 の値は Terraform と GitHub Actions でも利用するので、`.env` や `terraform.tfvars` にまとめておくと後続作業がスムーズです。

---

## 3. リポジトリ準備 & 依存ライブラリ

1. リポジトリをクローン／最新化します。
   ```bash
   git clone git@github.com:neurify-goto/fs-runner.git
   cd fs-runner
   git checkout feature/gcp-batch-implementation   # 運用ブランチへ切り替え
   ```

2. Python 依存をインストールします。
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt   # google-cloud-batch>=1.11.0 を含むことを確認
   ```

3. VS Code / JetBrains などを利用する場合は `.venv` を解釈させ、`pytest` や `black` を実行できる状態にしておきます。

---

## 4. Supabase スキーマ & メタデータ更新

Cloud Batch では `job_executions.metadata.batch` を新しく利用するため、最新の SQL を適用してください。

### 4.1 マイグレーション実行

1. Supabase SQL Editor で以下を順番に実行:
   - `scripts/migrations/202510_gcp_batch_execution_metadata.sql`
   - 未適用の場合は、Serverless 移行時のテーブル (`scripts/table_schema/*.sql`) も合わせて再実行

2. CLI で実行する例:
   ```bash
   export SUPABASE_DB_URL="postgresql://postgres:<password>@db.<project>.supabase.co:5432/postgres"
   psql "$SUPABASE_DB_URL" -f scripts/migrations/202510_gcp_batch_execution_metadata.sql
   ```

3. 実行後、`job_executions` の `metadata` に `batch` サブフィールドが存在するかを確認します。

### 4.2 Service Role Key の整理

- Cloud Batch から Supabase へ接続するため、Secret Manager に Service Role Key を格納します (後述の Terraform で利用)。
- 本番／ステージングを分ける場合は `FORM_SENDER_BATCH_SUPABASE_URL_SECRET` / `..._SERVICE_ROLE_SECRET` / `..._TEST_SECRET` をそれぞれ設定します。

---

## 5. GCP リソース (Terraform) の整備

本リポジトリには Terraform モジュールが用意されています。基本的には **Batch → Dispatcher** の順に plan/apply を行います。

### 5.1 Terraform 変数ファイルの準備

`infrastructure/gcp/batch/terraform.tfvars` (例):

```hcl
project_id                = "fs-prod-001"
region                    = "asia-northeast1"
gcs_bucket                = "fs-prod-001-form-sender-client-config"
artifact_repo             = "form-sender-runner"
supabase_secret_names     = [
  "projects/fs-prod-001/secrets/form_sender_supabase_url",
  "projects/fs-prod-001/secrets/form_sender_supabase_service_role",
]
batch_container_image     = "asia-northeast1-docker.pkg.dev/fs-prod-001/form-sender-runner/playwright:latest"
dispatcher_base_url       = "https://form-sender-dispatcher-<hash>-uc.a.run.app"
dispatcher_audience       = "https://form-sender-dispatcher-<hash>-uc.a.run.app"

# 必要に応じて上書き
prefer_spot_default       = true
allow_on_demand_default   = true
max_parallelism_default   = 100
machine_type              = "n2d-custom-4-10240"
batch_service_account_id  = "form-sender-batch"
batch_job_template_id     = "form-sender-batch-template"
batch_task_group_name     = "form-sender-workers"
```

`infrastructure/gcp/dispatcher/terraform.tfvars` (例):

```hcl
project_id                     = "fs-prod-001"
region                         = "asia-northeast1"
service_name                   = "form-sender-dispatcher"
container_image                = "asia-northeast1-docker.pkg.dev/fs-prod-001/form-sender-runner/playwright:latest"
client_config_bucket           = "fs-prod-001-form-sender-client-config"
cloud_run_job_name             = "form-sender-runner"
dispatcher_base_url            = "https://form-sender-dispatcher-<hash>-uc.a.run.app"
dispatcher_audience            = "https://form-sender-dispatcher-<hash>-uc.a.run.app"
batch_job_template_name        = "projects/fs-prod-001/locations/asia-northeast1/jobs/form-sender-batch-template"
batch_task_group_name          = "form-sender-workers"
batch_service_account_email    = "form-sender-batch@fs-prod-001.iam.gserviceaccount.com"
batch_container_image          = "asia-northeast1-docker.pkg.dev/fs-prod-001/form-sender-runner/playwright:latest"
supabase_url_secret            = "projects/fs-prod-001/secrets/form_sender_supabase_url"
supabase_service_role_secret   = "projects/fs-prod-001/secrets/form_sender_supabase_service_role"
supabase_url_test_secret       = "projects/fs-prod-001/secrets/form_sender_supabase_url_test"
supabase_service_role_test_secret = "projects/fs-prod-001/secrets/form_sender_supabase_service_role_test"
```

> 🔐 **ポイント**: `dispatcher_base_url` / `dispatcher_audience` は Cloud Run デプロイ後に取得できます。初回はプレースホルダで plan し、`gcloud run services describe` で実 URL を入れてから apply すると安全です。

### 5.2 Terraform 実行手順

```bash
# Batch モジュール
cd infrastructure/gcp/batch
terraform init
terraform plan
terraform apply    # 変更内容を確認した上で実行

# Dispatcher モジュール
cd ../dispatcher
terraform init
terraform plan
terraform apply
```

実行後、以下が自動的に構成されます。

- Cloud Batch Job Template + Spot / Standard 混在ポリシー  
- Cloud Storage バケット (client_config 保管) ライフサイクル 7 日  
- Artifact Registry リポジトリ  
- Cloud Run dispatcher サービス + Cloud Tasks キュー  
- 各種 Service Account と IAM 付与 (`roles/run.invoker`, `roles/secretmanager.secretAccessor` など)  
- Batch テンプレート環境変数: `FORM_SENDER_ENV=gcp_batch`, `FORM_SENDER_LOG_SANITIZE=1`, `FORM_SENDER_DISPATCHER_*`

---

## 6. コンテナイメージのビルド & GitHub Actions 設定

### 6.1 ローカルでのビルドとプッシュ

```bash
export PROJECT_ID="fs-prod-001"
export REGION="asia-northeast1"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/form-sender-runner/playwright"

gcloud auth configure-docker ${REGION}-docker.pkg.dev
docker build -t ${IMAGE}:$(git rev-parse --short HEAD) .
docker push ${IMAGE}:$(git rev-parse --short HEAD)
```

> ✅ Terraform の `batch_container_image` / `container_image` に同じタグを渡してください。

### 6.2 GitHub Actions シークレット

`.github/workflows/deploy-gcp-batch.yml` では以下のシークレットを利用します。リポジトリの Settings → Secrets から登録してください。

| 名前 | 用途 |
| --- | --- |
| `GCP_PROJECT_ID` | Terraform / gcloud 用 Project ID |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Workload Identity Federation 設定 |
| `GCP_TERRAFORM_SERVICE_ACCOUNT` | Terraform 実行 Service Account メール |
| `DISPATCHER_BASE_URL` | Cloud Run dispatcher の本番 URL |
| `DISPATCHER_AUDIENCE` | ID トークン Audience (通常は Base URL と同一) |
| `SUPABASE_URL_SECRET_ID` | Secret Manager のリソースパス |
| `SUPABASE_SERVICE_ROLE_SECRET_ID` | 同上 (Service Role Key) |
| `SUPABASE_URL_TEST_SECRET_ID` | テスト用 (任意) |
| `SUPABASE_SERVICE_ROLE_TEST_SECRET_ID` | テスト用 (任意) |

GitHub Actions を手動実行すると `terraform plan` が走り、`workflow_dispatch` で `apply=true` にすると本番反映されます。

---

## 7. GAS (Apps Script) 設定

1. GAS エディタ → プロジェクトの Script Properties に以下を追加/更新:
   - `USE_GCP_BATCH = true`
   - `FORM_SENDER_BATCH_PREFER_SPOT_DEFAULT = true`
   - `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT = true`
   - `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT = 100`
   - `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE =` (必要な場合のみ)
   - `FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH = 48`
   - `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH = 21600`

2. targeting シートに以下の列が存在するか確認し、なければ追加:
   - `useGcpBatch`
   - `batch_max_parallelism`
   - `batch_prefer_spot`
  - `batch_allow_on_demand_fallback`
  - `batch_machine_type`
  - `batch_signed_url_ttl_hours`
  - `batch_signed_url_refresh_threshold_seconds`
  - `batch_vcpu_per_worker`
  - `batch_memory_per_worker_mb`

3. `gas/form-sender/Code.gs` の `triggerServerlessFormSenderWorkflow_` は Cloud Batch モードを自動判定します。必要に応じて `resolveExecutionMode_()` を利用し、特定 targeting だけ先行移行する運用が可能です。

---

## 8. 動作確認フロー

1. **ユニットテスト**
   ```bash
   pytest -k gcp_batch --maxfail=1 --disable-warnings
   ```

2. **Dry Run (GAS)**
   - GAS エディタから `triggerFormSenderWorkflow(targetingId, { testMode: true })` を実行。  
   - Supabase の `job_executions` に `execution_mode=batch` が登録され、Cloud Batch のジョブ名が保存されることを確認。

3. **Cloud Batch コンソール確認**
   - Cloud Console → Batch → Jobs でジョブが `RUNNING` → `SUCCEEDED` になるか確認。  
   - Spot プリエンプトを模擬する場合は `gcloud batch jobs tasks terminate <job> --task-group=<group> --task-id=<id>` を実行し、`job_executions.metadata.batch.preempted` が `true` になるかを確認。

4. **GAS 停止 API**
   - `stopSpecificFormSenderTask(targetingId)` を実行し、Cloud Batch ジョブが `DELETED` になるか／Supabase ステータスが `cancelled` になるかを確認。

---

## 9. よくある質問 (FAQ)

**Q1. Terraform で `dispatcher_base_url` が分かりません。**  
A. 初回はプレースホルダでも plan は可能です。Cloud Run を手動デプロイ (`gcloud run deploy`) → `gcloud run services describe` で URL を取得し、`terraform.tfvars` を更新して再度 plan/apply してください。

**Q2. Batch マシンタイプが足りずにフォールバックされました。どうすれば良いですか？**  
A. ログに `Requested Batch machine_type ... Falling back to n2d-custom-4-10240` と表示された場合、`job_executions.metadata.batch.memory_warning` が `true` になります。GAS 側の `batch_machine_type` か Script Property `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` を増やして再実行してください。

**Q3. Supabase Service Role Key をローカルに置きたくありません。**  
A. Terraform の `supabase_secret_names` を利用して Secret Manager に格納し、Cloud Run/Batch からのみ参照する運用にしてください。ローカル検証時は `.env` に一時的に書くか、GitHub Actions のシークレットを使ってください。

**Q4. GitHub Actions 経由のデプロイで Batch だけ更新したい。**  
A. `workflow_dispatch` で `apply=true` を指定し、Terraform の plan/apply をバッチ側だけに限定したい場合は `terraform apply -target=module.batch` などを参考にジョブを編集してください。

---

## 10. 次のステップ

- Cloud Monitoring アラートを追加し、Spot 割り込み回数や失敗率を監視する。  
- targeting ごとに `batch_max_parallelism` や `batch_memory_per_worker_mb` を調整し、コストと安定性のバランスを最適化する。  
- 並行期間中は `USE_SERVERLESS_FORM_SENDER` を `true` に保ち、問題が起きた際にすぐ Cloud Run Jobs へ切り戻せる体制を維持する。

セットアップが完了したら、運用手順やブラウザテストの Runbook も更新し、チーム全体で共有してください。分からない点があればこのガイドにメモを残して改善していきましょう。

