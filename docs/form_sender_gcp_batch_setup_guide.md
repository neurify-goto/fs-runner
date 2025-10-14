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

### 2.1 GCP API の有効化（コンソール推奨）

Cloud Batch へジョブを投入する前に、対象プロジェクトで必要な API を有効化します。Cloud Console で以下を順番に実施してください。

1. [Google Cloud Console](https://console.cloud.google.com/) に対象プロジェクトでログイン。
2. 左上のハンバーガーメニュー → **API とサービス** → **ライブラリ** を開く。
3. 次の API を検索し、「有効にする」をクリック。
   - Cloud Batch API (`batch.googleapis.com`)
   - Compute Engine API (`compute.googleapis.com`)
   - Cloud Run Admin API (`run.googleapis.com`)
   - Cloud Tasks API (`cloudtasks.googleapis.com`)
   - Artifact Registry API (`artifactregistry.googleapis.com`)
   - Secret Manager API (`secretmanager.googleapis.com`)
   - IAM Service Account Credentials API (`iamcredentials.googleapis.com`)

> 💡 まとめて有効化したい場合は `gcloud services enable ...` を利用できますが、本ガイドではコンソール操作のみで完結できます。

---

## 3. リポジトリ準備 & 依存ライブラリ

1. リポジトリをクローン／最新化します。
   ```bash
   git clone git@github.com:neurify-goto/fs-runner.git
   cd fs-runner
   git checkout main   # 運用環境に合わせて適切なブランチへ切り替え
   ```

> ℹ️ 本番用に別ブランチを運用している場合は、必要に応じて該当ブランチへ切り替えてから作業を進めてください。
> 💡 GUI ツール派の方は GitHub Desktop や SourceTree で同じブランチをチェックアウトして構いません。以降の手順ではローカル作業フォルダを `fs-runner` として説明します。

2. Python 依存をインストールします。
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt   # google-cloud-batch==0.17.37 を含むことを確認
   ```

3. VS Code / JetBrains などを利用する場合は `.venv` を解釈させ、`pytest` や `black` を実行できる状態にしておきます。

---

## 4. Supabase スキーマ & メタデータ更新

Cloud Batch では `job_executions.metadata.batch` を新しく利用するため、最新の SQL を適用してください。

### 4.1 マイグレーション実行

1. Supabase ダッシュボードにログインし、対象プロジェクトの **SQL Editor** を開きます。
2. `scripts/migrations/202510_gcp_batch_execution_metadata.sql` の内容を貼り付けて実行します。
3. 既存の Serverless 用テーブル定義を適用していない場合は、同じく SQL Editor から `scripts/table_schema/*.sql` を順番に実行します。
4. 実行後、**Table Editor** で `job_executions` テーブルを開き、`metadata` に `batch` サブフィールドが追加されていることを確認します。

> 💡 CLI (`psql`) で適用したい場合は従来のコマンドを利用できますが、本ガイドでは SQL Editor を前提にしています。

### 4.2 Service Role Key の整理

- Cloud Batch から Supabase へ接続するため、Secret Manager に Service Role Key を格納します (後述の Terraform で利用)。
- 本番／ステージングを分ける場合は `FORM_SENDER_BATCH_SUPABASE_URL_SECRET` / `..._SERVICE_ROLE_SECRET` / `..._TEST_SECRET` をそれぞれ設定します。

#### 4.2.0 Cloud Batch / Dispatcher / Cloud Build 用サービスアカウントの準備

1. Cloud Console → **IAM と管理** → **サービス アカウント** を開き、右上の **サービス アカウントを作成** をクリックします。
2. 画面上で次の 5 種類が揃っているか確認し、存在しないものだけ作成します。
   - **Cloud Batch Runner 用**（例: `form-sender-batch`）
   - **Cloud Run dispatcher 用**（例: `form-sender-dispatcher`）
   - **GAS オーケストレーター用**（例: `form-sender-gas`）※Apps Script から Cloud Tasks / GCS / Cloud Run API を呼び出すための専用アカウント
   - **Cloud Build 実行用**: 自動で用意される `PROJECT_NUMBER@cloudbuild.gserviceaccount.com` をそのまま使うのが最も簡単です。プロジェクト番号は Cloud Console 上部のプロジェクトセレクタに表示されています。独自に権限を分離したい場合は、追加でカスタム SA（例: `form-sender-cloudbuild`）を作成しても構いません。
   - **Terraform 実行（GitHub Actions）用**: `form-sender-terraform` など（GitHub Actions 連携が必要な場合のみ）。詳細は `docs/github_actions_batch_ci.md` を参照します。
3. 作成直後のロール割り当てウィザード、またはサービスアカウント詳細 → **権限** → **権限を追加** で最低限以下のロールを付与します。
   - Batch Runner 用: `roles/batch.admin`, `roles/secretmanager.secretAccessor`, `roles/storage.objectAdmin`, `roles/artifactregistry.reader`
   - Dispatcher 用: `roles/run.admin`, `roles/secretmanager.secretAccessor`, `roles/cloudtasks.enqueuer`, `roles/iam.serviceAccountTokenCreator`
   - GAS オーケストレーター用: `roles/cloudtasks.enqueuer`, `roles/storage.objectAdmin`, `roles/run.invoker`, `roles/iam.serviceAccountUser`
   - Cloud Build 実行用: `roles/artifactregistry.writer`, `roles/storage.admin`, `roles/logging.logWriter`, `roles/cloudtrace.agent`（既定の Cloud Build サービスアカウントには `roles/cloudbuild.builds.builder` が自動付与されています。新規プロジェクトではステージング用 Cloud Storage バケットを自動作成するため `roles/storage.admin` が必須です。カスタム SA を使う場合は同ロールも追加してください）
   - Terraform 実行用: `roles/run.admin`, `roles/iam.serviceAccountAdmin`, `roles/iam.serviceAccountUser`, `roles/batch.admin`, `roles/artifactregistry.admin`, `roles/secretmanager.admin`, `roles/storage.admin`, `roles/cloudtasks.admin`, `roles/logging.admin`（インフラを Terraform で一括管理できるよう、事前準備チェックリスト 4. の権限と同等に揃えます）

> 💡 Cloud Tasks のサービスエージェント（`service-<PROJECT_NUMBER>@gcp-sa-cloudtasks.iam.gserviceaccount.com`）にも `roles/iam.serviceAccountTokenCreator` と `roles/iam.serviceAccountUser` を付与しておく必要があります。例:
> ```bash
> gcloud iam service-accounts add-iam-policy-binding \
>   form-sender-dispatcher@formsalespaid.iam.gserviceaccount.com \
>   --member="serviceAccount:service-621668223275@gcp-sa-cloudtasks.iam.gserviceaccount.com" \
>   --role="roles/iam.serviceAccountTokenCreator"
> gcloud iam service-accounts add-iam-policy-binding \
>   form-sender-dispatcher@formsalespaid.iam.gserviceaccount.com \
>   --member="serviceAccount:service-621668223275@gcp-sa-cloudtasks.iam.gserviceaccount.com" \
>   --role="roles/iam.serviceAccountUser"
> ```
4. Cloud Build 実行用サービスアカウントは「ビルドを実行する主体」であり、Cloud Batch Runner / dispatcher 用とは別物です。5.2.3.2 節の Cloud Build トリガー画面では、このサービスアカウントを選択してください。
5. Terraform 実行用サービスアカウントは、GitHub Actions 連携ガイド（`docs/github_actions_batch_ci.md`）で説明する Workload Identity Federation で利用します。必要がなければ作成・設定しなくても構いません。
6. 後からロールを追加したい場合は、**IAM と管理** 画面で対象サービスアカウントの **アクセス権** を編集します。

> ℹ️ Terraform を利用する場合は、この段階で作成したサービスアカウントのメールアドレスを変数ファイルに記載しておくと、後続の設定がスムーズです。

#### 4.2.1 Secret Manager 登録手順（コンソール中心）

1. Cloud Console → **セキュリティ** → **Secret Manager** → **シークレットを作成** を選択。
2. 以下のシークレットを登録します。
   - **Service Role Key 用**: 名前を `form_sender_supabase_service_role` とし、Supabase ダッシュボード → **プロジェクト設定 → API** に表示される `service_role` キー（長い JWT 文字列）を貼り付けて保存。
   - **Supabase URL 用**: 名前を `form_sender_supabase_url` とし、同画面にある `https://<project>.supabase.co` を貼り付けて保存。
   - **テスト環境がある場合**: `form_sender_supabase_service_role_test` や `form_sender_supabase_url_test` などの名前で、テスト用プロジェクトの値を保存。
3. 各シークレットの詳細画面 → **権限** タブで、次のサービスアカウントに `Secret Manager Secret Accessor` を付与。
   - Cloud Batch Runner 用サービスアカウント（例: `form-sender-batch@<project>.iam.gserviceaccount.com`）
   - Cloud Run dispatcher 用サービスアカウント（例: `form-sender-dispatcher@<project>.iam.gserviceaccount.com`）

> ℹ️ Terraform の `supabase_secret_names` に `projects/<project>/secrets/<name>` を列挙すると、Cloud Batch テンプレートへ自動で環境変数が注入されます（コンソールで作成したシークレットもこの形式で指定できます）。

> 🔐 Supabase 別環境（ステージングなど）を利用する場合は `_TEST_SECRET` 用にも同様のシークレットを作成し、同じ権限付与を行ってください。



---

## 5. GCP リソース構築（Cloud Console 前提）

Cloud Batch を安定稼働させるために必要な周辺リソース（Cloud Storage、Artifact Registry、Cloud Batch、Cloud Run、Cloud Tasks など）は、すべて Cloud Console の操作だけで構築できます。ここではコンソール手順を中心に説明し、最後に Terraform で置き換える場合のメモを添えています。

### 5.1 作業のながれ

1. サービスアカウントを作成し、必要なロールが付与されているか確認（4.2.0 節参照）。
2. client_config を保存する Cloud Storage バケットを作成し、ライフサイクルを設定。
3. Artifact Registry に Runner／dispatcher 用のコンテナイメージを登録。
4. Cloud Batch ジョブテンプレートを作成し、Spot 優先・環境変数・リソースを構成。
5. Cloud Run に dispatcher をデプロイし、Supabase／Batch 関連の環境変数を投入。
6. Cloud Tasks キューを作成し、dispatcher への OIDC 認証付き呼び出しを設定。
7. Secret Manager と GAS Script Properties を最新の値に更新し、疎通テストを実施。

### 5.2 コンソール手順

1. **サービスアカウントの確認**（未作成の場合は 4.2.0 節で作成）\
   Cloud Console → **IAM と管理** → **サービス アカウント** に移動し、`form-sender-batch` と `form-sender-dispatcher` が存在し、以下のロールが付与されていることを確認します。\
   - Batch: `roles/batch.admin`, `roles/secretmanager.secretAccessor`, `roles/storage.objectAdmin`, `roles/artifactregistry.reader`\
   - Dispatcher: `roles/run.admin`, `roles/secretmanager.secretAccessor`, `roles/cloudtasks.enqueuer`, `roles/iam.serviceAccountTokenCreator`

2. **Cloud Storage バケット（client_config 保存先）**\
   Cloud Console → **Cloud Storage** → **バケットを作成**。名前は `formsalespaid-form-sender-client-config`（推奨）などプロジェクトを識別できるものにし、リージョンは `asia-northeast1`（Batch/dispatcher と同じ）を選択します。アクセス制御は「一様」を選択し、作成後にバケット詳細 → **ライフサイクル** → **ルールを追加** で「オブジェクトの年齢 7 日」を条件に削除ルールを作成します。**権限** タブでは以下のサービスアカウントに `Storage Object Admin` を付与してください。\
   - GAS (Apps Script) 実行用: `<project-number>@appspot.gserviceaccount.com`\
   - Cloud Batch Runner 用: `form-sender-batch@<project>.iam.gserviceaccount.com`\
   - Cloud Run dispatcher 用: `form-sender-dispatcher@<project>.iam.gserviceaccount.com`\
   このバケット名を後続の Script Properties `FORM_SENDER_GCS_BUCKET` および Terraform 設定（`terraform.tfvars` の `gcs_bucket`）に転記しておくと、GAS からの `client_config` アップロードや dispatcher からの署名付き URL 発行が正しく動作します。

3. **Artifact Registry（コンテナリポジトリ）**\
   1. Cloud Console → **Artifact Registry** → **リポジトリを作成** を開き、次の値で作成します。\
      - フォーマット: *Docker*\
      - ロケーションタイプ: *リージョン*\
      - リージョン: `asia-northeast1`（Cloud Batch／dispatcher と揃える）\
      - リポジトリ ID: `form-sender-runner` など識別しやすい名称\
      - 暗号化: 既定（Google 管理キー）を選択\
      作成後、リポジトリ詳細の **アクセス権** で不要な公開権限が付与されていないことを確認します。\
   2. イメージ登録は Cloud Build の **第 2 世代トリガー**で行います。\
      - Cloud Console → **Cloud Build** → **トリガー** で右上の **作成** を押し、「第 2 世代」を選択します（必要に応じて **リポジトリを接続** から GitHub App を連携）。\
      - 「ソース」を *リポジトリ* に設定し、「マネージド リポジトリ」一覧から対象リポジトリを選択してブランチ条件（例: `^main$`）を指定します。\
      - 「ビルド構成」は *Cloud Build ファイル* を選び、Runner 用トリガーでは `cloudbuild/form_sender_runner.yaml` を、dispatcher 用トリガーでは `cloudbuild/form_sender_dispatcher.yaml` を入力します。どちらも `_IMAGE_NAME` がファイル内に定義されているため追加設定は不要で、`:${SHORT_SHA}` と `:latest` の両方に push されます。\
      - 実行サービスアカウントは 4.2.0 節で準備した Cloud Build 用サービスアカウント（既定の `PROJECT_NUMBER@cloudbuild.gserviceaccount.com` か、用意した `form-sender-cloudbuild` など）を選択します。必須ロールは `Artifact Registry Writer`, `Storage Admin`, `Logging Log Writer`, `Cloud Trace Agent`、および既定で付与される `Cloud Build Service Account` 相当です。\
      - **詳細** セクションの「ビルドログ」カードでは GitHub へのログ送信を無効化（チェックを外す）すると、Cloud Logging のみに記録され機微情報が漏れにくくなります。その他の項目（承認フローなど）はチーム方針に合わせて設定してください。\
      - トリガー保存後、一覧のメニュー（︙）から **トリガーを実行** を選択すると指定ブランチの HEAD でビルドが走り、結果は Cloud Build の **ビルド** タブに記録されます。単発ビルドが必要な場合は右上の **ビルドを作成** → 「Cloud Build ファイルを使用」を選び、Runner 用には `cloudbuild/form_sender_runner.yaml`、dispatcher 用には `cloudbuild/form_sender_dispatcher.yaml` を指定してください。\
   3. ビルド完了後、Artifact Registry の **パッケージ** タブに `playwright`（Runner）と `dispatcher`（Cloud Run）が登録されていることを確認し、それぞれの `FULL IMAGE NAME` を控えます。`playwright` のフルパス（例: `asia-northeast1-docker.pkg.dev/<project>/form-sender-runner/playwright:<short_sha>`）は `terraform.tfvars` の `batch_container_image` に、`dispatcher` のフルパスは Cloud Run デプロイ時のコンテナイメージに利用します。また、`form-sender-batch` と `form-sender-dispatcher` のサービスアカウントに `Artifact Registry Reader` ロールが付与されているか再確認します（Cloud Console → **IAM と管理** → **IAM** で対象 SA を検索）。\
\
   > 参考: [Create and manage build triggers](https://cloud.google.com/build/docs/automating-builds/create-manage-triggers)（最終更新 2025-09-19 UTC）、[Create GitHub App triggers](https://cloud.google.com/build/docs/automating-builds/create-github-app-triggers)（最終更新 2024-09-10 UTC）

**Cloud Build 構成ファイルの活用（ユーザー管理サービスアカウント使用時）**\
   1. 本リポジトリには Runner 用の `cloudbuild/form_sender_runner.yaml` と dispatcher 用の `cloudbuild/form_sender_dispatcher.yaml` を既に含めています。どちらも `${PROJECT_ID}` を使い `asia-northeast1-docker.pkg.dev/<project>/form-sender-runner/<image>:{SHORT_SHA,latest}` へ push します。必要に応じて次の項目だけ書き換えてください。\
      - Artifact Registry のリポジトリ名を変更したい場合: `_IMAGE_NAME` の `form-sender-runner` 部分を自分のリポジトリ ID に置き換える。\
      - 追加のビルド手順が必要な場合のみ、Cloud Build ステップを追記する（必要なライブラリは既定の Dockerfile でインストール済み）。
   2. Cloud Build トリガーの編集画面に戻り、「ビルド構成」を *Cloud Build ファイル* に変更し、Runner 用トリガーでは `cloudbuild/form_sender_runner.yaml` を、dispatcher 用トリガーでは `cloudbuild/form_sender_dispatcher.yaml` を入力します。サブスティテューション `_IMAGE_NAME` はファイル内で既に定義済みのため、UI の「変数」セクションで追加設定を行う必要はありません（プロジェクト名を変更したい場合のみ上書きします）。
   3. どちらのファイルでも `options.logging: CLOUD_LOGGING_ONLY` が適用されるため、ユーザー管理サービスアカウントでもエラーなくビルドが実行されます。必要に応じて `logging: REGIONAL_USER_OWNED_BUCKET` や `defaultLogsBucketBehavior` へ変更してください。

4. **Cloud Batch ジョブ（テンプレート相当のベースジョブを作成）**\
   > ⚠️ 2025-10-11 時点では、公式 Terraform プロバイダ（`hashicorp/google` / `hashicorp/google-beta`）が Cloud Batch のジョブリソースをサポートしておらず、`terraform plan` で `Invalid resource type "google_batch_job"` が発生します。ジョブに相当する設定はコンソールまたは API から直接登録してください。\
   - Terraform を使わずに完結させたい場合は、以下のステップ 0 と 1 をスキップし、5.2.2 以降のコンソール手順でバケットや Artifact Registry などのリソースを作成してください。\
   0. （任意）Terraform で一括管理したい場合は、`infrastructure/gcp/batch/terraform.tfvars.example` を `terraform.tfvars` にコピーし、`project_id` / `region` / `gcs_bucket` / `artifact_repo` / `batch_container_image` / `dispatcher_base_url` / `dispatcher_audience` などを自分の環境向けに編集します。Supabase の Secret Manager を利用する場合は `supabase_secret_names` のコメントアウトを外してリソース名を列挙します。\
   1. （任意）Terraform で周辺リソース（サービスアカウント、GCS バケット、Artifact Registry 等）だけを適用する場合は、`infrastructure/gcp/batch/main.tf` 内の `resource "google_batch_job" ...` ブロックと、`infrastructure/gcp/batch/outputs.tf` にある `google_batch_job.form_sender_template` 参照行をコメントアウトした状態で `terraform init && terraform apply` を実行します。プロバイダがジョブリソースをサポートしたら差分を戻して再適用してください。\
   2. コンソールでベースとなるジョブを作成します（Cloud Console → **Batch** → **ジョブ一覧** → **作成**）。ジョブ作成ウィザードで定義した内容をそのままテンプレートとして残し、以後 dispatcher から参照します。フォームには次の値を入力します。citeturn0search0\
      - **ジョブ ID**: `form-sender-template` など固定値（再実行時の衝突を避けるためこのジョブは再利用せず保存専用とします）。\
      - **リージョン**: `asia-northeast1`。ゾーンは `any` のままで問題ありません。\
      - **タスク数 / 並列数**: それぞれ `1`。dispatcher が実際のジョブ投入時に上書きするため、テンプレートでは最小構成にします。\
      - **コンテナ イメージ**: `asia-northeast1-docker.pkg.dev/<project>/form-sender-runner/playwright:latest`。必要に応じて `Entry point` を `/bin/sh`、`CMD` を `-c "echo Template"` のような軽量コマンドにしておくと初回実行時に成功しやすくなります。\
      - **環境変数**: 6.3 節の一覧に従って `FORM_SENDER_BATCH_*` 系、`FORM_SENDER_DISPATCHER_BASE_URL` などを入力します。Secret Manager の値は「シークレット」タブから `projects/<project>/secrets/.../versions/latest` を選択します。\
      - **追加ストレージ（任意）**: 入出力データを共有する必要がある場合のみ「Additional configurations → Storage volumes → Add new volume」から Cloud Storage バケットをマウントします。バケット名とマウントパス（例: `/mnt/disks/client-config`）を指定するとタスク内でローカルフォルダとして利用できます。不要であれば設定しなくて構いません。citeturn0search0\
      - **リソース仕様**: Provisioning model は Spot（フォールバックあり）を推奨。コンソールではプリセットのマシンタイプのみ選択できるため、`n2d-standard-4` など最終的に必要となる vCPU/メモリを満たすタイプを選び、`Resources per task` の vCPU（`cpuMilli`）とメモリ（`memoryMiB`）がマシンタイプの上限を超えないようにしてください。`n2d-custom-4-10240` のようなカスタム形状を利用したい場合は、`gcloud batch jobs submit --config job.json` 等で `allocationPolicy.instances[].policy.machineType` にカスタム文字列を指定する必要があります。詳細はコンソールドキュメントの「Resource specifications」節を参照。citeturn0search0\
        ```json
        {
          "taskGroups": [{
            "taskSpec": {
              "runnables": [{"script": {"text": "#!/bin/bash\necho template"}}],
              "computeResource": {"cpuMilli": 4000, "memoryMib": 10240}
            },
            "taskCount": 1
          }],
          "allocationPolicy": {
            "instances": [{
              "policy": {"machineType": "n2d-custom-4-10240", "provisioningModel": "SPOT"}
            }]
          }
        }
        ```
        > *参考*: 上記 JSON を `job.json` として保存し、`gcloud batch jobs submit form-sender-template --location=asia-northeast1 --config=job.json --no-run` のように登録するとカスタム形状を含むジョブを作成できます（`--no-run` で即時実行を抑制）。
      - **コスト注意**: `Resources per task` に 10 GB など小さい値を入れても、課金は選択したマシンタイプ（例: `n2d-standard-4` は 16 GB メモリ搭載）に対して発生します。メモリを節約して課金を抑えたい場合は、CLI/API でカスタム形状を指定して実際の必要量（例: 4 vCPU / 10 GiB）へ合わせてください。カスタム N2D のオンデマンド料金は vCPU が $0.0288771/時間、メモリが $0.0038703/ギビバイト時間（Iowa リージョンの「Default」列）で、4 vCPU・10 GiB と 4 vCPU・16 GiB を 176 時間稼働させた場合の差額は約 $4.09 です。citeturn0search0
      - **ログ**: Cloud Logging 送信を有効にしたままで構いません。\
      作成ボタンを押すとジョブが即時実行されます。数十秒で `SUCCEEDED` になったことを確認し、このジョブは削除せず「テンプレート」として保持します（以後 dispatcher は `get_job` で定義をコピーします）。
   3. ジョブ作成後、Cloud Shell もしくはローカルで次のコマンドを実行し、テンプレート（例: `form-sender`）として利用するジョブ名とタスクグループ名を取得します。実行前に `gcloud config set project formsalespaid` などで対象プロジェクトを切り替えておいてください。citeturn1search1\
      > ⚠️ `WARNING: Your active project does not match the quota project ...` が表示された場合は、以下を順に実行して ADC の quota project を揃えます。サービスアカウントの ADC が設定されている状態だと `set-quota-project` が失敗するため、先にユーザーで再ログインするのが確実です。
      > ```bash
      > gcloud auth application-default login
      > gcloud auth application-default set-quota-project formsalespaid
      > ```
      ```bash
      gcloud config set project formsalespaid
      gcloud batch jobs describe form-sender \
        --location=asia-northeast1 \
        --format='value(name, taskGroups[0].name)'
      ```

      Cloud Run のログを確認したい場合は、`gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="form-sender"' --project=formsalespaid --limit=50` のように `gcloud logging read` コマンドを利用してください。citeturn0search0
      出力される `projects/<project>/locations/<region>/jobs/<job_id>` が `FORM_SENDER_BATCH_JOB_TEMPLATE`、`taskGroups[0].name` が `FORM_SENDER_BATCH_TASK_GROUP` に相当します。必要に応じて `--format=json` で完全なジョブ定義を保存し、将来の再作成に備えてください。citeturn1search1\
   4. Script Properties および Cloud Run dispatcher の環境変数を最新化します。\
      - GAS 側: スクリプトエディタ → **プロジェクトの設定** → **スクリプト プロパティ** を開き、以下を入力して保存します。\
        | キー | 値の例 | 説明 |
        | --- | --- | --- |
        | `FORM_SENDER_BATCH_JOB_TEMPLATE` | `projects/formsalespaid/locations/asia-northeast1/jobs/form-sender` | `gcloud batch jobs describe` で取得したジョブ名 |
        | `FORM_SENDER_BATCH_TASK_GROUP` | `form-sender-task-group` | 同コマンドで得た `taskGroups[0].name` |
        | `FORM_SENDER_BATCH_SERVICE_ACCOUNT` など既存エントリ | 必要に応じて更新 | 変更があればここで調整 |
      - Cloud Run 側: Cloud Console → **Cloud Run** → `form-sender-dispatcher` → **編集とデプロイ** → **コンテナ、変数、シークレット** を開き、以下を確認／更新します。\
        | 環境変数 | 値 | 備考 |
        | --- | --- | --- |
        | `FORM_SENDER_BATCH_JOB_TEMPLATE` | `projects/formsalespaid/locations/asia-northeast1/jobs/form-sender` | Script Properties と同じ値 |
        | `FORM_SENDER_BATCH_TASK_GROUP` | `form-sender-task-group` | 同上 |
        | `FORM_SENDER_BATCH_SERVICE_ACCOUNT` など関連項目 | 既存値を再確認 | 変更が必要な場合のみ更新 |
      - これらを更新したら Cloud Run サービスを再デプロイし、GAS の Script Properties も保存したことを確認してから次の手順に進みます。\
   5. 後日 Terraform のプロバイダがジョブリソースをサポートした場合は、上記ジョブ構成を YAML/JSON に書き出し、`google_batch_job` ブロックへ移植すると IaC 管理へ戻せます。プロバイダの更新状況は [terraform-provider-google-beta のリリースノート](https://github.com/hashicorp/terraform-provider-google-beta/releases) を随時確認してください。\
   6. dispatcher が実ジョブを投入する際は `src/dispatcher/gcp.py` の `_calculate_resources()` でワーカー数や targeting オプションに基づきリソースを動的に再計算します。テンプレートで指定した値は初期値として扱われる点に注意してください。

5. **Cloud Run dispatcher のデプロイ**\
   Cloud Run は Cloud Tasks からの HTTP リクエストを受け取り Cloud Batch ジョブを起動するエントリポイントです。次の手順で `form-sender-dispatcher` サービスを作成します。\
   1. Cloud Console → **Cloud Run** → **サービスを作成** を開き、ソースは *既存のコンテナ イメージをデプロイ*（デフォルト）を選択します。\
      - **コンテナ イメージの URL**: `asia-northeast1-docker.pkg.dev/<project>/form-sender-runner/dispatcher:latest` を入力し **選択**。Cloud Build の dispatcher トリガーが `:latest` を常に更新するため、このタグを指定します。\
   2. **構成** セクションで以下を設定します。\
      - **サービス名**: `form-sender-dispatcher`（任意ですが GAS の Script Properties と一致させることを推奨）\
      - **リージョン**: `asia-northeast1 (東京)`\
      - **認証**: *認証が必要* を選択し、IAM のみ有効にします（Cloud Tasks からのリクエストが OIDC で認証されるため）。\
      - **課金**: *リクエスト ベース* のままで問題ありません。\
      - **サービスのスケーリング**: *自動スケーリング* を選択し、`インスタンスの最小数` = 0、`インスタンスの最大数` = 10（要件に応じて調整）。\
      - **Ingress**: *すべて* を選択し、インターネット経由のリクエストを許可したうえで IAM で制限します。\
   3. **コンテナ、ボリューム、ネットワーキング、セキュリティ** → **コンテナ** を開き、`編集` から次の値を設定します。\
      - **コンテナポート**: `8080`（既定）\
      - **メモリ**: `512 MiB` 以上（Playwright dispatcher は軽量なので既定値で可）\
      - **CPU**: `1`\
      - **環境変数**: **変数とシークレット** をクリックし、以下を追加します。Secret Manager から値を参照するものは「シークレット」タブで指定してください。\
        - Supabase 関連: `DISPATCHER_SUPABASE_URL`, `DISPATCHER_SUPABASE_SERVICE_ROLE_KEY`\
        - Dispatcher 基本設定: `DISPATCHER_PROJECT_ID`, `DISPATCHER_LOCATION`, `FORM_SENDER_CLOUD_RUN_JOB`, `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_DISPATCHER_AUDIENCE`\
        - Batch 連携: `FORM_SENDER_BATCH_PROJECT_ID`, `FORM_SENDER_BATCH_LOCATION`, `FORM_SENDER_BATCH_JOB_TEMPLATE`, `FORM_SENDER_BATCH_TASK_GROUP`, `FORM_SENDER_BATCH_SERVICE_ACCOUNT`, `FORM_SENDER_BATCH_CONTAINER_IMAGE`, `FORM_SENDER_BATCH_SUPABASE_URL_SECRET`, `FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_SECRET`\
      - **サービスアカウント**: 画面下部の **セキュリティ** セクションで `form-sender-dispatcher@<project>.iam.gserviceaccount.com` を選択します。存在しない場合は 4.2.0 節の手順で作成してください。\
      - その他の項目（ヘルスチェック、同時実行数、起動時 CPU ブーストなど）は既定値のままで構いません。要件に応じて調整する場合のみ変更してください。\
   4. **作成** を押してデプロイし、完了後に表示される **エンドポイント URL**（例: `https://form-sender-dispatcher-xxxx.a.run.app`）を控えます。リビジョンが起動エラーになっても URL 自体は変わらないため、この時点で `terraform.tfvars` の `dispatcher_base_url` / `dispatcher_audience` や Script Properties `FORM_SENDER_DISPATCHER_BASE_URL` に反映して構いません。

6. **Cloud Tasks キュー**\
Cloud Console → **Cloud Tasks** → **キューを作成**。名前は `form-sender-dispatcher`、リージョンは `asia-northeast1` を指定。ターゲットに HTTP を選択し、URL に `https://<cloud-run-url>/v1/form-sender/tasks` を入力します。**認証方式は「認証なし」で問題ありません**（GAS 側で署名付き ID トークンを `Authorization` ヘッダーとして付与します）。リトライは **キュー設定** で管理します（タスク作成時に `retryConfig` を渡すことはできません）。推奨設定例: 
   - **Maximum attempts**: 3（再送回数の上限）
   - **Minimum backoff**: 60 秒 / **Maximum backoff**: 600 秒（指数バックオフ）
   - **Maximum retry duration**: 当日 19:00 JST（=10:00 UTC）までを許容する場合は 32,400 秒程度を目安に調整
   - Spot 枯渇時にフォールバックさせない案件では、最大試行回数を 1 にするのも選択肢です。
   これらの値は運用ポリシーに応じて調整し、必要に応じて Monitoring と連携してアラートを設定してください。
   > Cloud Tasks のサービスエージェント（`service-<PROJECT_NUMBER>@gcp-sa-cloudtasks.iam.gserviceaccount.com`）と GAS オーケストレーター用サービスアカウント（`form-sender-gas@<project>.iam.gserviceaccount.com`）の両方に、`form-sender-dispatcher@<project>.iam.gserviceaccount.com` へ `roles/iam.serviceAccountTokenCreator` と `roles/iam.serviceAccountUser` を付与しておくと、タスク作成時に OIDC トークンが確実に生成されます。
   > Cloud Tasks サービスエージェント（`service-<PROJECT_NUMBER>@gcp-sa-cloudtasks.iam.gserviceaccount.com`）にも `roles/iam.serviceAccountTokenCreator` を、GAS オーケストレーター用サービスアカウントには `roles/iam.serviceAccountUser` を付与しておくと、タスク作成時に `form-sender-dispatcher@...` の OIDC トークンが確実に生成されます。

7. **最終確認**\
   Secret Manager のアクセス権、Cloud Storage バケットの権限、Cloud Batch テンプレートと Cloud Run dispatcher の環境変数が意図どおりかを確認し、GAS Script Properties（`FORM_SENDER_TASKS_QUEUE`, `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_BATCH_*` など）を最新値に更新します。その後、テスト用 targeting で dispatcher が正しく呼び出せるかを確認してください。

### 補足: Terraform を利用する場合

Infrastructure as Code で管理したい場合は、`infrastructure/gcp/batch` と `infrastructure/gcp/dispatcher` にある Terraform 定義を活用できます。`terraform.tfvars` にプロジェクト ID・リージョン・バケット名・サービスアカウント・シークレット名などを記入し、`terraform init && terraform apply` を実行してください（5.2.4 の手順に従い `google_batch_job` ブロックはプロバイダ対応まで除外する必要があります）。Terraform を利用するケースでも、事前に Secret Manager への登録と Artifact Registry へのイメージ配置を済ませておく必要があります。

> 📝 手動構築では設定漏れが起こりやすいため、設定内容を記録しチーム内でレビューする運用を推奨します。再現性や差分管理が必要になったら Terraform への移行を検討してください。

## 6. GAS (Apps Script) 設定

### 6.1 Script Properties の初期設定
1. GAS エディタ → **プロジェクトの設定** → **スクリプト プロパティ** を開き、次のキーが正しく設定されているか確認・更新します。

   | キー | 設定例 | 説明 |
   | --- | --- | --- |
| `FORM_SENDER_TASKS_QUEUE` | `projects/formsalespaid/locations/asia-northeast1/queues/form-sender-dispatcher` | Cloud Tasks で作成したキューのフルリソース名。`projects/<project>/locations/<region>/queues/<queue>` 形式で入力します。 |
| `FORM_SENDER_DISPATCHER_BASE_URL` | `https://form-sender-dispatcher-621668223275.asia-northeast1.run.app` | Cloud Run dispatcher のベース URL。旧プロパティ `FORM_SENDER_DISPATCHER_URL` を利用している場合も同じ値を設定します。末尾にエンドポイントパスは付与しません。 |
| `FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT` | `form-sender-dispatcher@formsalespaid.iam.gserviceaccount.com` | Cloud Tasks → Cloud Run dispatcher 呼び出し時の OIDC トークン用サービスアカウント。未設定だとタスクに認証情報が付与されず 403 になります。 |
| `FORM_SENDER_GCS_BUCKET` | `formsalespaid-form-sender-client-config` | 5.2.2 で作成した Cloud Storage バケット名。Apps Script から client_config JSON を格納し、dispatcher が署名付き URL を生成する際に利用します。 |
| `SERVICE_ACCOUNT_JSON` | `{"type":"service_account","project_id":"formsalespaid","private_key_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\\n...","client_email":"form-sender-gas@formsalespaid.iam.gserviceaccount.com",...}` | GAS から Cloud Tasks / Cloud Run / Cloud Storage API を呼び出すためのサービスアカウント鍵。4.2.0 節で作成した **GAS オーケストレーター用** サービスアカウントの JSON を貼り付けます。改行は `\n` のまま保存して問題ありません。 |

   > これらが未設定の場合、GAS 側は自動的に GitHub Actions 経路へフォールバックし Cloud Batch を利用しません。移行前後で値を控えておくと復旧が容易です。
   > `FORM_SENDER_GCS_BUCKET` は Cloud Storage で作成したバケット名と完全一致させてください。間違えていると GAS が client_config をアップロードできず dispatcher 側で `FORM_SENDER_GCS_BUCKET が設定されていません` というエラーになります。
   > `SERVICE_ACCOUNT_JSON` を取得する手順: Cloud Console → **IAM** → GAS オーケストレーター用サービスアカウント（例: `form-sender-gas@<project>.iam.gserviceaccount.com`）→ **鍵** → **鍵を追加** → **新しい鍵を作成** → JSON を選択 → ダウンロードしたファイルの内容をそのまま Script Property に貼り付けます。鍵は機密情報のため、ダウンロード後は安全な場所（社内パスワードマネージャなど）に保管し、不要なローカルファイルは削除してください。既存キーを使い回す場合でも、Cloud Tasks / Storage / Cloud Run へのアクセス権が最新かを必ず再確認してください。

2. 同じ画面で次のキーを追加または更新します。
   - `USE_GCP_BATCH = true`
   - `FORM_SENDER_BATCH_PREFER_SPOT_DEFAULT = true`
   - `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT = false`
   - `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT = 100`
  - `FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT = n2d-standard-2`
   - `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE =` （必要に応じて。設定方法は下記参照）
   - `FORM_SENDER_BATCH_INSTANCE_COUNT_DEFAULT = 2`
   - `FORM_SENDER_BATCH_INSTANCE_COUNT_OVERRIDE =` （必要に応じて）
   - `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_DEFAULT = 2`
   - `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_OVERRIDE =` （必要に応じて）
   - `FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT = 1`
   - `FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT = 2048`
   - `FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT = 2048`
   - `FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH = 48`
   - `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH = 21600`
   - `FORM_SENDER_BATCH_MAX_ATTEMPTS_DEFAULT = 1`

   > ✅ デフォルトでは **n2d-standard-2 を 2 台同時起動** する構成です。Spot 在庫の確保率とコストのバランスが良く、ほとんどの targeting で追加設定なしに動作します。
   > ⚠️ `FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT` / `batch_machine_type` を `n2d-standard-8` など他の標準形状に設定した場合、GAS はその形状の vCPU / メモリ上限を基準に判定し、リソース要求が上限を超えたときのみ `n2d-custom-*` に自動フォールバックします。標準形状で必要リソースを満たせる間はそのまま維持されるため、意図したメモリ確保が崩れません。
   > ⚠️ メモリ不足が発生した案件では `batch_machine_type` / Script Properties をカスタム形状（`n2d-custom-*` など）へ切り替えるか、`batch_workers_per_workflow` / `batch_instance_count` を調整してください。フォールバックにより課金が増える点にも留意が必要です。
   > 💡 `FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT` はコスト最適化のため既定値を `false` としています。オンデマンドへの自動切り替えが必要な案件のみ targeting シートの `batch_allow_on_demand_fallback` を `true` に設定してください。
   > `SERVICE_ACCOUNT_JSON` を取得する手順: Cloud Console → **IAM** → GAS オーケストレーター用サービスアカウント（例: `form-sender-gas@<project>.iam.gserviceaccount.com`）→ **鍵** → **鍵を追加** → **新しい鍵を作成** → JSON を選択 → ダウンロードしたファイルの内容をそのまま Script Property に貼り付けます。鍵は機密情報のため、ダウンロード後は安全な場所（社内パスワードマネージャなど）に保管し、不要なローカルファイルは削除してください。

3. `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` の設定目安:
   - デフォルトの `n2d-standard-2`（2 vCPU / 8 GiB）で問題が無い場合は **空文字のまま** にしてください。
   - targeting 側で `batch_machine_type` を指定する予定が無く、全案件でカスタム形状を固定利用したい場合だけ設定します。
   - 形式は `n2d-custom-<vCPU>-<memoryMB>`（例: `n2d-custom-16-65536`）。`memoryMB` は MiB 単位です。
   - 特定案件のみメモリや vCPU を増強したい場合は、targeting シートの `batch_machine_type` 列を優先的に利用してください。


### 6.2 Targeting シートの更新
1. targeting シートに以下の列が存在するか確認し、なければ追加します。
   - `useGcpBatch`
   - `batch_max_parallelism`
   - `batch_prefer_spot`
   - `batch_allow_on_demand_fallback`
   - `batch_machine_type`
   - `batch_instance_count`
   - `batch_workers_per_workflow`
   - `batch_signed_url_ttl_hours`
   - `batch_signed_url_refresh_threshold_seconds`
   - `batch_vcpu_per_worker`
   - `batch_memory_per_worker_mb`
   - `batch_max_attempts`

| 項目 | 参照優先度 | 備考 |
| --- | --- | --- |
| 実行モード (`useGcpBatch` / `useServerless`) | 1. targeting列 → 2. Script Properties (`USE_GCP_BATCH`, `USE_SERVERLESS_FORM_SENDER`) → 3. GitHub Actions | `true` / `false` だけでなく `1` / `0` / `yes` も受け付けます。|
| Cloud Batch タスク並列数 (`batch_max_parallelism`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT` → 3. GAS 推奨値 | 1 つのジョブで同時に実行する Cloud Batch タスク数の上限。未入力時は Script Property の既定 (デフォルト 100)。 |
| Spot 設定 (`batch_prefer_spot`, `batch_allow_on_demand_fallback`) | 1. targeting列 → 2. Script Properties | `prefer_spot=true` で Spot 優先、fallback を false にするとスポット枯渇時に失敗します。 |
| マシンタイプ (`batch_machine_type`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` → 3. `FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT` | 既定値は `n2d-standard-2`。標準形状を指定した場合はその形状の vCPU / メモリ上限まで利用し、超過時のみ `n2d-custom-<workers>-<memory_mb>` に自動フォールバックします。 |
| Batch インスタンス数 (`batch_instance_count`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_INSTANCE_COUNT_OVERRIDE` → 3. `FORM_SENDER_BATCH_INSTANCE_COUNT_DEFAULT` (既定 2) | 起動する Spot VM 数の下限。`concurrent_workflow` が小さくてもここで指定した台数を確保します。|
| Batch ワーカー数 (`batch_workers_per_workflow`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_OVERRIDE` → 3. `FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_DEFAULT` (既定 2) | 1 インスタンスあたりの Python ワーカー数。1〜16 の範囲で調整してください。 |
| 署名付き URL TTL (`batch_signed_url_ttl_hours`) | 1. targeting列 → 2. `FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH` (既定 48h) | 1〜168 の整数を指定。 |
| 署名付き URL リフレッシュ閾値 (`batch_signed_url_refresh_threshold_seconds`) | 1. targeting列 → 2. `FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH` (既定 21600 秒) | 60〜604800 の範囲で指定。 |
| リソース単位 (`batch_vcpu_per_worker`, `batch_memory_per_worker_mb`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT` / `FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT` | 未指定時は vCPU=1, メモリ=2048MB（共有バッファとして 2048MB を別途確保）。 |
| リトライ回数 (`batch_max_attempts`) | 1. targeting列 → 2. `FORM_SENDER_BATCH_MAX_ATTEMPTS_DEFAULT` | Cloud Batch タスクの最大試行回数（1 以上）。設定すると dispatcher が `maxRetryCount` を上書きします。 |

   > targeting 列を空にすると Script Properties の値がそのまま使われます。移行初期は Script Properties だけで小さく始め、必要になった Targeting だけ列で上書きする運用が推奨です。

   > ⚠️ TTL と閾値の整合性に注意: `signed_url_refresh_threshold_seconds` を `signed_url_ttl_hours × 3600` 以上に設定すると dispatcher 側で自動的に閾値が TTL 未満へ補正されます。閾値は TTL より十分短い値（例: TTL=48hなら閾値=21600秒 ≒ 6h）に保ってください。

   > ℹ️ `FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT` で設定したバッファ値は、GAS から dispatcher へ送信される `memory_buffer_mb` フィールドにも埋め込まれます。Script Properties を更新すれば Cloud Batch 実行へ自動反映されます。

### 6.3 Cloud Run dispatcher 用環境変数の確認

Terraform を使わずに手動で Cloud Run サービスを更新する場合や、ローカルで `DispatcherSettings.from_env()` を利用する場合は下記の環境変数を忘れずに設定します（`src/dispatcher/config.py` 参照）。不足すると起動時にエラーになります。

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

これらに加えて、Cloud Run dispatcher では従来通り `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_DISPATCHER_AUDIENCE`, `FORM_SENDER_CLOUD_RUN_JOB` などの既存環境変数も必須です。手動デプロイやローカル検証では Cloud Console → **Cloud Run** → **環境変数とシークレット** から設定するか、必要に応じて `gcloud run deploy --set-env-vars` を利用してください。

### 6.4 その他の補足

- `gas/form-sender/Code.gs` の `triggerServerlessFormSenderWorkflow_` は Cloud Batch モードを自動判定します。特定 targeting だけ先行移行したい場合は `resolveExecutionMode_()` を併用してください。
- targeting や Script Properties を変更した後は、テスト用 targeting で Dry Run を実施してから本番に適用します。


---

## 7. 補足: GitHub Actions 連携（任意）

本ドキュメントの手順で GAS → Cloud Tasks → Cloud Run dispatcher → Cloud Batch のルートは完結します。CI/CD や Terraform の自動実行を GitHub Actions で行いたい場合のみ、別ドキュメント「[GitHub Actions 連携ガイド](github_actions_batch_ci.md)」を参照してください。

---


## 8. 動作確認フロー

> 運用時の定期チェックやトラブルシューティング手順は `docs/form_sender_batch_operations.md` を参照してください。


1. **（任意）GitHub Actions でユニットテストを実行**
   - GitHub Actions を利用する場合は、リポジトリ → **Actions** → `Deploy Cloud Batch Runner` ワークフローを実行し、`pytest -k gcp_batch` の結果を確認します。CI/CD を使用しない場合はローカルで `pytest -k gcp_batch` を実行してください。

2. **Dry Run (GAS)**
   - GAS エディタから `triggerFormSenderWorkflow(targetingId, { testMode: true })` を実行。  
   - Supabase の `job_executions` に `execution_mode=batch` が登録され、Cloud Batch のジョブ名が保存されることを確認。

3. **Cloud Batch コンソール確認**
   - Cloud Console → **Batch** → **ジョブ** で対象ジョブが `RUNNING` → `SUCCEEDED` になるか確認します。ジョブ詳細画面ではタスク単位のログと再試行回数が確認できます。
   - （任意）Spot プリエンプトの挙動を再現したい場合は、GUI からは直接操作できないため `gcloud batch jobs tasks terminate` コマンドが必要です。実施する際はテスト用ジョブでのみ行い、実行後に Supabase 側で `job_executions.metadata.batch.preempted` が `true` になることを確認してください。

4. **GAS 停止 API**
   - `stopSpecificFormSenderTask(targetingId)` を実行し、Cloud Batch ジョブが `DELETED` になるか／Supabase ステータスが `cancelled` になるかを確認。

---

## 9. よくある質問 (FAQ)

**Q1. Terraform で `dispatcher_base_url` が分かりません。**  
A. 初回はプレースホルダでも plan は可能です。Cloud Console → **Cloud Run** → 対象サービスを開き、右上に表示される URL（例: `https://form-sender-dispatcher-xxxx.a.run.app`）をコピーして `terraform.tfvars` の `dispatcher_base_url` に貼り付けます。その後、GitHub Actions から再度 `Deploy Cloud Batch Runner` ワークフローを実行し、plan の差分が解消されることを確認してください。

**Q2. Batch マシンタイプが足りずにフォールバックされました。どうすれば良いですか？**  
A. ログに `Requested Batch machine_type ... Falling back to n2d-custom-...` のようなメッセージが出た場合、`job_executions.metadata.batch.memory_warning` が `true` になります。対象案件の `batch_machine_type` をより大きい形状へ変更するか、Script Property `FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE` を調整してください。ワーカー数やバッファを減らすことで回避できるケースもあります。

**Q3. Supabase Service Role Key をローカルに置きたくありません。**  
A. Terraform の `supabase_secret_names` を利用して Secret Manager に格納し、Cloud Run/Batch からのみ参照する運用にしてください。ローカル検証時は `.env` に一時的に書くか、GitHub Actions のシークレットを使ってください。

**Q4. GitHub Actions 経由のデプロイで Batch だけ更新したい。**  
A. `workflow_dispatch` で `apply=true` を指定し、Terraform の plan/apply をバッチ側だけに限定したい場合は `terraform apply -target=module.batch` などを参考にジョブを編集してください。設定手順の詳細は `docs/github_actions_batch_ci.md` を参照してください。

---

## 10. 次のステップ

- Cloud Monitoring アラートを追加し、Spot 割り込み回数や失敗率を監視する。  
- targeting ごとに `batch_max_parallelism` や `batch_memory_per_worker_mb` を調整し、コストと安定性のバランスを最適化する。  
- 並行期間中は `USE_SERVERLESS_FORM_SENDER` を `true` に保ち、問題が起きた際にすぐ Cloud Run Jobs へ切り戻せる体制を維持する。

セットアップが完了したら、運用手順やブラウザテストの Runbook も更新し、チーム全体で共有してください。分からない点があればこのガイドにメモを残して改善していきましょう。
