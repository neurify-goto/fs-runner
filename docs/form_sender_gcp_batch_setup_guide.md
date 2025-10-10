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

### 2.1 GCP API の有効化

Cloud Batch へジョブを投入する前に、対象プロジェクトで必要な API を必ず有効化してください。特に `batch.googleapis.com` が無効だと Terraform や `gcloud batch jobs submit` が失敗します。

```bash
gcloud services enable \
  batch.googleapis.com \
  compute.googleapis.com \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com
```

---

## 3. リポジトリ準備 & 依存ライブラリ

1. リポジトリをクローン／最新化します。
   ```bash
   git clone git@github.com:neurify-goto/fs-runner.git
   cd fs-runner
   git checkout main   # 運用環境に合わせて適切なブランチへ切り替え
   ```

> ℹ️ 本番用に別ブランチを運用している場合は、必要に応じて該当ブランチへ切り替えてから作業を進めてください。

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

#### 4.2.1 Secret Manager 登録手順 (例)

```bash
# プロジェクトとサービスアカウントは環境に合わせて読み替えてください
export PROJECT_ID="fs-prod-001"
export SECRET_ID="form_sender_supabase_service_role"

# シークレット本体を作成（初回のみ）
gcloud secrets create ${SECRET_ID} \
  --project=${PROJECT_ID} \
  --replication-policy="automatic"

# 値を登録（JSON や文字列をファイル経由でアップロード）
echo "<SUPABASE_SERVICE_ROLE_KEY>" > /tmp/service-role.key
gcloud secrets versions add ${SECRET_ID} \
  --project=${PROJECT_ID} \
  --data-file=/tmp/service-role.key

# Cloud Batch / Dispatcher から参照するサービスアカウントにアクセス権を付与
export BATCH_SA="form-sender-batch@${PROJECT_ID}.iam.gserviceaccount.com"
export DISPATCHER_SA="form-sender-dispatcher@${PROJECT_ID}.iam.gserviceaccount.com"
for SA in ${BATCH_SA} ${DISPATCHER_SA}; do
  gcloud secrets add-iam-policy-binding ${SECRET_ID} \
    --project=${PROJECT_ID} \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

> ℹ️ Terraform の `supabase_secret_names` に `projects/<project>/secrets/<name>` を列挙すると、Cloud Batch テンプレートへ自動で環境変数が注入されます。

Supabase URL も同様にシークレットへ格納しておきます（Dispatcher 側の必須値）。

```bash
export URL_SECRET_ID="form_sender_supabase_url"
echo "https://<YOUR_PROJECT>.supabase.co" > /tmp/supabase-url.txt

gcloud secrets create ${URL_SECRET_ID} \
  --project=${PROJECT_ID} \
  --replication-policy="automatic"

gcloud secrets versions add ${URL_SECRET_ID} \
  --project=${PROJECT_ID} \
  --data-file=/tmp/supabase-url.txt

for SA in ${BATCH_SA} ${DISPATCHER_SA}; do
  gcloud secrets add-iam-policy-binding ${URL_SECRET_ID} \
    --project=${PROJECT_ID} \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

> 🔐 Supabase 別環境（ステージング等）を利用する場合は `_TEST_SECRET` 用にも同様の手順を実施してください。

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

> ℹ️ **補足**: `terraform apply` により Cloud Batch ジョブテンプレートが作成される際、内部で 1 回だけ軽量な検証スクリプト (`echo "Form Sender Batch template validated"`) が実行されます。ランナー本番用コンテナは起動せず、ジョブは数秒で完了するため失敗ログは残りません。

実行後、以下が自動的に構成されます。

- Cloud Batch Job Template（初期実行は検証スクリプトのみ） + Spot / Standard 混在ポリシー  
- Cloud Storage バケット (client_config 保管) ライフサイクル 7 日  
- Artifact Registry リポジトリ  
- Cloud Run dispatcher サービス + Cloud Tasks キュー  
- 各種 Service Account と IAM 付与 (`roles/run.invoker`, `roles/secretmanager.secretAccessor` など)  
- Batch テンプレート環境変数: `FORM_SENDER_ENV=gcp_batch`, `FORM_SENDER_LOG_SANITIZE=1`, `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_DISPATCHER_AUDIENCE`

---

## 6. コンテナイメージのビルド & GitHub Actions 設定

### 6.0 GitHub Actions Workload Identity Federation の準備

GitHub Actions から GCP へ接続する際は、Workload Identity Federation (WIF) を利用します。以下は最小構成の例です。

```bash
export PROJECT_ID="fs-prod-001"
export PROJECT_NUMBER=$(gcloud projects describe ${PROJECT_ID} --format="value(projectNumber)")
export POOL_ID="fs-runner-gha"
export PROVIDER_ID="github"
export SERVICE_ACCOUNT_EMAIL="form-sender-terraform@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. Workload Identity Pool と Provider を作成
gcloud iam workload-identity-pools create ${POOL_ID} \
  --project=${PROJECT_ID} \
  --location="global" \
  --display-name="GitHub Actions Pool"

gcloud iam workload-identity-pools providers create-oidc ${PROVIDER_ID} \
  --project=${PROJECT_ID} \
  --location="global" \
  --workload-identity-pool=${POOL_ID} \
  --display-name="GitHub" \
  --attribute-mapping="google.subject=assertion.sub" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# 2. Terraform 実行用のサービスアカウントに WIF バインドを付与
gcloud iam service-accounts add-iam-policy-binding ${SERVICE_ACCOUNT_EMAIL} \
  --project=${PROJECT_ID} \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/neurify-goto/fs-runner" \
  --role="roles/iam.workloadIdentityUser"

# 3. GitHub Secrets に登録する値を控える
WORKLOAD_IDENTITY_PROVIDER=$(gcloud iam workload-identity-pools providers describe ${PROVIDER_ID} \
  --project=${PROJECT_ID} \
  --location="global" \
  --workload-identity-pool=${POOL_ID} \
  --format="value(name)")
echo "Set GCP_WORKLOAD_IDENTITY_PROVIDER=${WORKLOAD_IDENTITY_PROVIDER}"
echo "Set GCP_TERRAFORM_SERVICE_ACCOUNT=${SERVICE_ACCOUNT_EMAIL}"
```

> GitHub 側では `permissions: id-token: write` を有効化済みのため、上記で取得した値を Secrets に設定すれば `google-github-actions/auth@v2` から自動的に利用されます。`attribute.repository` の指定は対象リポジトリ（`owner/name`）に合わせて変更してください。

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

1. GAS エディタ → プロジェクトの Script Properties を開き、既存の dispatcher 関連設定が空になっていないか必ず確認します。
   - `FORM_SENDER_TASKS_QUEUE`
   - `FORM_SENDER_DISPATCHER_URL` または `FORM_SENDER_DISPATCHER_BASE_URL`
   > これらが未設定の場合、GAS 側は自動的に GitHub Actions 経路へフォールバックし Cloud Batch を利用しません。スポット移行後も Batch 実行が意図せず停止しないよう、移行前後で値を控えておくことを推奨します。

2. GAS エディタで Script Properties に以下を追加/更新:
   - `USE_GCP_BATCH = true`
   - `FORM_SENDER_BATCH_PREFER_SPOT_DEFAULT = true`
   - `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT = true`
   - `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT = 100`
   - `FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT = n2d-custom-4-10240`
   - `FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT = 1`
   - `FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT = 2048`
   - `FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT = 2048`
   - `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE =` (必要な場合のみ)
   - `FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH = 48`
   - `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH = 21600`
   - `FORM_SENDER_BATCH_MAX_ATTEMPTS_DEFAULT = 1`

   > ⚠️ `batch_machine_type` 系の値は **カスタムマシンタイプ（例: `n2d-custom-*`）を推奨** します。`e2-standard-2` など標準プリセットを指定すると、dispatcher 側でメモリ不足を事前検知できず Cloud Batch 提出時に失敗する恐れがあります。targeting シートからマシンタイプを上書きする場合も同じ制約が適用されます。

3. targeting シートに以下の列が存在するか確認し、なければ追加:
   - `useGcpBatch`
   - `batch_max_parallelism`
   - `batch_prefer_spot`
   - `batch_allow_on_demand_fallback`
   - `batch_machine_type`
   - `batch_signed_url_ttl_hours`
   - `batch_signed_url_refresh_threshold_seconds`
   - `batch_vcpu_per_worker`
   - `batch_memory_per_worker_mb`
   - `batch_max_attempts`

| 項目 | 参照優先度 | 備考 |
| --- | --- | --- |
| 実行モード (`useGcpBatch` / `useServerless`) | 1. targeting列 → 2. Script Properties (`USE_GCP_BATCH`, `USE_SERVERLESS_FORM_SENDER`) → 3. GitHub Actions | `true` / `false` だけでなく `1` / `0` / `yes` も受け付けます。|
| 並列数 (`batch_max_parallelism`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT` → 3. GAS推奨値 | 未入力時は Script Property の既定 (デフォルト 100)。 |
| Spot 設定 (`batch_prefer_spot`, `batch_allow_on_demand_fallback`) | 1. targeting列 → 2. Script Properties | `prefer_spot=true` で Spot 優先、fallback を false にするとスポット枯渇時に失敗します。 |
| マシンタイプ (`batch_machine_type`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` → 3. GAS がワーカー数から自動計算 | 自動計算は `n2d-custom-<workers>-<memory_mb>` 形式 (2GB/worker + 2GB バッファ)。|
| 署名付き URL TTL (`batch_signed_url_ttl_hours`) | 1. targeting列 → 2. `FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH` (既定 48h) | 1〜168 の整数を指定。 |
| 署名付き URL リフレッシュ閾値 (`batch_signed_url_refresh_threshold_seconds`) | 1. targeting列 → 2. `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH` (既定 21600 秒) | 60〜604800 の範囲で指定。 |
| リソース単位 (`batch_vcpu_per_worker`, `batch_memory_per_worker_mb`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT` / `FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT` | 未指定時は vCPU=1, メモリ=2048MB（共有バッファとして 2048MB を別途確保）。 |
| リトライ回数 (`batch_max_attempts`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_MAX_ATTEMPTS_DEFAULT` | Cloud Batch タスクの最大試行回数（1 以上）。設定すると dispatcher が `maxRetryCount` を上書きします。 |

   > targeting 列を空にすると Script Properties の値がそのまま使われます。移行初期は Script Properties だけで小さく始め、必要になった Targeting だけ列で上書きする運用が推奨です。

   > ⚠️ TTL と閾値の整合性に注意: `signed_url_refresh_threshold_seconds` を `signed_url_ttl_hours × 3600` 以上に設定すると dispatcher 側で自動的に閾値が TTL 未満へ補正されます。想定外の再署名を避けるため、閾値は TTL より十分短い値（例: TTL=48hなら閾値=21600秒 ≒ 6h）に保ってください。

   > ℹ️ `FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT` で設定したバッファ値は、GAS から dispatcher へ送信される `memory_buffer_mb` フィールドにも埋め込まれます。Cloud Run 側の `FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT` はフォールバック値として残りますが、Script Properties を更新すれば自動的に Batch 実行へ反映されます。

### 7.4 Cloud Run dispatcher 用環境変数の確認

Terraform を使わずに手動で Cloud Run サービスを更新する場合や、ローカルで `DispatcherSettings.from_env()` を利用する場合は下記の環境変数を忘れずに設定します（`src/dispatcher/config.py` 参照）。`require_batch_configuration()` で不足すると起動時にエラーになります。

| 環境変数 | 用途 |
| --- | --- |
| `FORM_SENDER_BATCH_PROJECT_ID` | Batch リソースを作成する GCP プロジェクト ID（省略時は `DISPATCHER_PROJECT_ID` を使用） |
| `FORM_SENDER_BATCH_LOCATION` | Batch ジョブのリージョン（例: `asia-northeast1`） |
| `FORM_SENDER_BATCH_JOB_TEMPLATE` | `projects/<proj>/locations/<region>/jobs/<template>` 形式のジョブテンプレート名 |
| `FORM_SENDER_BATCH_TASK_GROUP` | テンプレートで利用するタスクグループ名（`taskGroups[0].name`） |
| `FORM_SENDER_BATCH_SERVICE_ACCOUNT` | Batch ジョブが実行するサービスアカウント（メールアドレス形式） |
| `FORM_SENDER_BATCH_CONTAINER_IMAGE` | Runner イメージの Artifact Registry パス |
| `FORM_SENDER_BATCH_ENTRYPOINT` | 任意。コンテナのエントリポイントを上書きする場合に指定 |
| `FORM_SENDER_BATCH_SUPABASE_URL_SECRET` | Supabase URL を格納した Secret Manager リソースパス |
| `FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_SECRET` | Supabase Service Role Key の Secret Manager リソースパス |
| `FORM_SENDER_BATCH_SUPABASE_URL_TEST_SECRET` | テスト環境向け Supabase URL シークレット（必要な場合のみ） |
| `FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_TEST_SECRET` | テスト環境向け Service Role Key シークレット（必要な場合のみ） |

これらに加えて、Cloud Run dispatcher では従来通り `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_DISPATCHER_AUDIENCE`, `FORM_SENDER_CLOUD_RUN_JOB` などの既存環境変数も必須です。Terraform を利用する場合はモジュールで自動付与されますが、手動デプロイやローカル検証では `.env`・`gcloud run deploy --set-env-vars` などで反映させてください。

4. `gas/form-sender/Code.gs` の `triggerServerlessFormSenderWorkflow_` は Cloud Batch モードを自動判定します。必要に応じて `resolveExecutionMode_()` を利用し、特定 targeting だけ先行移行する運用が可能です。

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
