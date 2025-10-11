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
2. 画面上で次の 4 種類が揃っているか確認し、存在しないものだけ作成します。
   - **Cloud Batch Runner 用**（例: `form-sender-batch`）
   - **Cloud Run dispatcher 用**（例: `form-sender-dispatcher`）
   - **Cloud Build 実行用**: 自動で用意される `PROJECT_NUMBER@cloudbuild.gserviceaccount.com` をそのまま使うのが最も簡単です。プロジェクト番号は Cloud Console 上部のプロジェクトセレクタに表示されています。独自に権限を分離したい場合は、追加でカスタム SA（例: `form-sender-cloudbuild`）を作成しても構いません。
   - **Terraform 実行（GitHub Actions）用**: `form-sender-terraform` など、後述の Workload Identity Federation で GitHub Actions と紐付けるサービスアカウントを 1 つ準備します。
3. 作成直後のロール割り当てウィザード、またはサービスアカウント詳細 → **権限** → **権限を追加** で最低限以下のロールを付与します。
   - Batch Runner 用: `roles/batch.admin`, `roles/secretmanager.secretAccessor`, `roles/storage.objectAdmin`, `roles/artifactregistry.reader`
   - Dispatcher 用: `roles/run.admin`, `roles/secretmanager.secretAccessor`, `roles/cloudtasks.enqueuer`, `roles/iam.serviceAccountTokenCreator`
   - Cloud Build 実行用: `roles/artifactregistry.writer`, `roles/storage.admin`, `roles/logging.logWriter`, `roles/cloudtrace.agent`（既定の Cloud Build サービスアカウントには `roles/cloudbuild.builds.builder` が自動付与されています。新規プロジェクトではステージング用 Cloud Storage バケットを自動作成するため `roles/storage.admin` が必須です。カスタム SA を使う場合は同ロールも追加してください）
   - Terraform 実行用: `roles/run.admin`, `roles/iam.serviceAccountAdmin`, `roles/iam.serviceAccountUser`, `roles/batch.admin`, `roles/artifactregistry.admin`, `roles/secretmanager.admin`, `roles/storage.admin`, `roles/cloudtasks.admin`, `roles/logging.admin`（インフラを Terraform で一括管理できるよう、事前準備チェックリスト 4. の権限と同等に揃えます）
4. Cloud Build 実行用サービスアカウントは「ビルドを実行する主体」であり、Cloud Batch Runner / dispatcher 用とは別物です。5.2.3.2 節の Cloud Build トリガー画面では、このサービスアカウントを選択してください。
5. Terraform 実行用サービスアカウントは GitHub Actions の Workload Identity Federation で利用します（6.0 節参照）。Cloud Build のサービスアカウントとは役割が異なるため混同しないよう注意してください。
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
   Cloud Console → **Cloud Storage** → **バケットを作成**。名前は `fs-prod-001-form-sender-client-config` のようにプロジェクトを識別できるものにし、リージョンは `asia-northeast1`（Batch/dispatcher と同じ）を選択します。アクセス制御は「一様」を選択し、作成後にバケット詳細 → **ライフサイクル** → **ルールを追加** で「オブジェクトの年齢 7 日」を条件に削除ルールを作成します。**権限** タブでは `form-sender-batch` と `form-sender-dispatcher` に `Storage Object Admin` を付与してください。

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
      - 「ビルド構成」は *Dockerfile をビルド* を選び、Dockerfile のパスにはリポジトリ直下の `Dockerfile` を指定します（GitHub Actions `deploy-gcp-batch.yml` でも同じパスを利用）。UI 上で「Dockerfile のディレクトリ」が `/`（リポジトリルート）、「Dockerfile のファイル名」が `Dockerfile` になるように入力してください。トリガー対象ブランチは実際に検証したいブランチ（例: `^main$` や `^feature/.*$`）を指定します。実行サービスアカウントは 4.2.0 節で確認した Cloud Build 用サービスアカウント（既定の `PROJECT_NUMBER@cloudbuild.gserviceaccount.com` か、用意した `form-sender-cloudbuild` など）を選択し、付与済みの `Artifact Registry Writer` 権限で Artifact Registry へプッシュできるようにします。\
      - 同じカード内の下部にある **結果イメージ** 入力欄に、Artifact Registry へプッシュしたいタグ（例: `asia-northeast1-docker.pkg.dev/<project>/<repo>/playwright:latest`）を 1 行ずつ入力します。ここへ値を入れないと、自動で付与されるタグがなくビルドが失敗します。`<project>` と `<repo>` は実際のプロジェクト ID／リポジトリ ID に置き換えてください。dispatcher 用 Dockerfile が別途必要な場合は専用トリガーを作成するか、cloudbuild.yaml にまとめて扱います。\
      - 画面最下部の **詳細** セクションには「承認」「ビルドログ」「サービス アカウント」の 3 カードが並びます。2025-10 時点の Cloud Console では、Dockerfile モードを選択した場合にログの送信先（`Cloud Logging のみ` やカスタムバケット）を GUI から変更できません。ユーザー管理サービスアカウントを指定した状態でこのまま実行すると、次のエラーでビルドが失敗します。\
        \
        `build must either specify build.logs_bucket, use REGIONAL_USER_OWNED_BUCKET build.options.default_logs_bucket_behavior, or select CLOUD_LOGGING_ONLY/NONE logging options`\
        \
        回避策として、いずれかを選択してください。\
        1. Cloud Storage にログ専用バケット（例: `gs://<project>-cloud-build-logs`）を作成し、トリガー作成後に `gcloud builds triggers patch` で `--service-account` と併せて `--build-log-bucket=<bucket>` も指定する。\
        2. もしくは（推奨）リポジトリに Cloud Build 構成ファイルを追加し、`options.logging: CLOUD_LOGGING_ONLY` を記述したうえでトリガーのビルド構成を *Cloud Build ファイル* に切り替える。手順は後述の「Cloud Build 構成ファイルの例」を参照してください。\
      - トリガー保存後、一覧のメニュー（︙）から **トリガーを実行** を選択すると指定ブランチの HEAD でビルドが走り、結果は Cloud Build の **ビルド** タブに記録されます。単発ビルドが必要な場合は右上の **ビルドを作成** → 「Dockerfile をビルド」を利用します。\
   3. ビルド完了後、Artifact Registry の **パッケージ** タブに `playwright:latest` や `dispatcher:latest` が登録されていることを確認し、`form-sender-batch` と `form-sender-dispatcher` のサービスアカウントに `Artifact Registry Reader` ロールが付与されているか再確認します（Cloud Console → **IAM と管理** → **IAM** で対象 SA を検索）。\
\
   > 参考: [Create and manage build triggers](https://cloud.google.com/build/docs/automating-builds/create-manage-triggers)（最終更新 2025-09-19 UTC）、[Create GitHub App triggers](https://cloud.google.com/build/docs/automating-builds/create-github-app-triggers)（最終更新 2024-09-10 UTC）

   **Cloud Build 構成ファイルの活用（ユーザー管理サービスアカウント使用時）**\
   1. 本リポジトリには `cloudbuild/form_sender_runner.yaml` を既に含めています。内容は以下のとおりで、必要に応じて `_IMAGE_NAME` のレジストリ名などを自分のプロジェクト向けに調整してください（`${PROJECT_ID}` は Cloud Build が自動的に置き換えます）。\
      ```yaml
      # cloudbuild/form_sender_runner.yaml
      substitutions:
        _IMAGE_NAME: "asia-northeast1-docker.pkg.dev/$PROJECT_ID/form-sender-runner/playwright"

      steps:
        - name: gcr.io/cloud-builders/docker
          args: ["build", "-t", "${_IMAGE_NAME}:${SHORT_SHA}", "."]
        - name: gcr.io/cloud-builders/docker
          args: ["push", "${_IMAGE_NAME}:${SHORT_SHA}"]

      images:
        - "${_IMAGE_NAME}:${SHORT_SHA}"

      options:
        logging: CLOUD_LOGGING_ONLY
      ```
      - `SHORT_SHA` は Cloud Build が自動展開するコミットハッシュ 7 桁です。特定タグ（例: `latest`）を維持したい場合は `images` セクションに `"${_IMAGE_NAME}:latest"` を追記し、追加ステップで `docker tag` → `docker push` を行ってください。
   2. Cloud Build トリガーの編集画面に戻り、「ビルド構成」を *Cloud Build ファイル* に変更し、ファイルパスに `cloudbuild/form_sender_runner.yaml` を入力します。サブスティテューション `_IMAGE_NAME` はファイル内で既に定義済みのため、UI の「変数」セクションで追加設定を行う必要はありません（プロジェクト名を変更したい場合のみ上書きします）。
   3. このファイルを利用すると `options.logging: CLOUD_LOGGING_ONLY` が常に適用されるため、ユーザー管理サービスアカウントでもエラー無くビルドが実行されます。必要に応じて `logging: REGIONAL_USER_OWNED_BUCKET` や `defaultLogsBucketBehavior` へ変更しても構いません。

4. **Cloud Batch ジョブテンプレート**\
   Cloud Console → **Batch** → **ジョブ テンプレート** → **テンプレートを作成**。リージョンは `asia-northeast1`、テンプレート名は `form-sender-batch-template` など任意で構いません。タスク グループでは次の項目を設定します。\
   - コンテナイメージ: 手順 3 で登録した Runner イメージ\
   - サービスアカウント: `form-sender-batch@<project>.iam.gserviceaccount.com`\
   - 環境変数: `FORM_SENDER_ENV=gcp_batch`, `FORM_SENDER_LOG_SANITIZE=1`, `FORM_SENDER_DISPATCHER_BASE_URL=<Cloud Run URL>`, `FORM_SENDER_DISPATCHER_AUDIENCE=<Cloud Run URL>`, `FORM_SENDER_CLIENT_CONFIG_BUCKET=<バケット名>`, `FORM_SENDER_BATCH_PROJECT_ID=<project>`, `FORM_SENDER_BATCH_LOCATION=asia-northeast1`, `FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT=100` など Runner・dispatcher が参照する値をすべて入力\
   - コンピュート リソース: `CPU (milli)` を 4000、`メモリ (MiB)` を 10240 に設定し `n2d-custom-4-10240` 相当とする\
   - リトライ: `maxRetryCount` を 0（再実行なし）または運用ポリシーに従って設定\
   割り当てポリシーでは `Spot` を最優先にし、必要に応じて Standard（オンデマンド）を追加します。

5. **Cloud Run dispatcher のデプロイ**\
   Cloud Console → **Cloud Run** → **サービスを作成**。コンテナイメージに dispatcher 用イメージを指定し、リージョンは `asia-northeast1`、サービスアカウントは `form-sender-dispatcher@<project>.iam.gserviceaccount.com` を選択します。**環境変数** タブで以下を登録し、Secret Manager の値は「秘密」から参照させてください。\
   - Supabase 関連: `DISPATCHER_SUPABASE_URL`, `DISPATCHER_SUPABASE_SERVICE_ROLE_KEY`\
   - Dispatcher 基本設定: `DISPATCHER_PROJECT_ID`, `DISPATCHER_LOCATION`, `FORM_SENDER_CLOUD_RUN_JOB`, `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_DISPATCHER_AUDIENCE`\
   - Batch 連携: `FORM_SENDER_BATCH_PROJECT_ID`, `FORM_SENDER_BATCH_LOCATION`, `FORM_SENDER_BATCH_JOB_TEMPLATE`, `FORM_SENDER_BATCH_TASK_GROUP`, `FORM_SENDER_BATCH_SERVICE_ACCOUNT`, `FORM_SENDER_BATCH_CONTAINER_IMAGE`, `FORM_SENDER_BATCH_SUPABASE_URL_SECRET`, `FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_SECRET`\
   デプロイ後に表示されるサービス URL を控え、後続の Cloud Batch テンプレートや GAS Script Properties に利用します。

6. **Cloud Tasks キュー**\
   Cloud Console → **Cloud Tasks** → **キューを作成**。名前は `form-sender-dispatcher`、リージョンは `asia-northeast1` を指定。ターゲットに HTTP を選択し、URL に `https://<cloud-run-url>/v1/form-sender/tasks` を入力します。認証方式は OIDC を選択し、サービスアカウントに `form-sender-dispatcher@<project>.iam.gserviceaccount.com` を設定。リトライ設定は既定値をベースに運用ポリシーへ合わせて調整します。

7. **最終確認**\
   Secret Manager のアクセス権、Cloud Storage バケットの権限、Cloud Batch テンプレートと Cloud Run dispatcher の環境変数が意図どおりかを確認し、GAS Script Properties（`FORM_SENDER_TASKS_QUEUE`, `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_BATCH_*` など）を最新値に更新します。その後、テスト用 targeting で dispatcher が正しく呼び出せるかを確認してください。

### 補足: Terraform を利用する場合

Infrastructure as Code で管理したい場合は、`infrastructure/gcp/batch` と `infrastructure/gcp/dispatcher` にある Terraform 定義を使うと同じリソースを再現できます。`terraform.tfvars` にプロジェクト ID・リージョン・バケット名・サービスアカウント・シークレット名などを記入し、`terraform init && terraform apply` を実行してください。Terraform を利用するケースでも、事前に Secret Manager への登録と Artifact Registry へのイメージ配置を済ませておく必要があります。

> 📝 手動構築では設定漏れが起こりやすいため、設定内容を記録しチーム内でレビューする運用を推奨します。再現性や差分管理が必要になったら Terraform への移行を検討してください。

---

## 6. コンテナイメージのビルド & GitHub Actions 設定

### 6.0 GitHub Actions Workload Identity Federation の準備（Cloud Console）

GitHub Actions から GCP へ安全に接続するため、Terraform 実行用サービスアカウント（4.2.0 節）と GitHub リポジトリを Workload Identity Federation で紐付けます。すべて Cloud Console の GUI で完了できます。

1. Cloud Console → **IAM と管理** → **Workload Identity Federation** を開き、上部の **プールを作成** をクリックします。
   - **名前**: `fs-runner-gha` など判別しやすい ID。
   - **表示名**: `GitHub Actions Pool` など任意。
   - **場所**: 既定の `global` のままにします。
   - 作成後、一覧にプールが追加されるのでクリックして詳細へ移動します。
2. プール詳細画面の **プロバイダ** タブで **プロバイダを追加** → **OIDC** を選択し、以下の値を入力します。
   - **プロバイダ ID**: `github`
   - **発行元 URL**: `https://token.actions.githubusercontent.com`
   - **表示名**: 任意（例: `GitHub`）
   - **属性マッピング**: 
     - `google.subject=assertion.sub`
     - `attribute.repository=assertion.repository`
   - **属性条件**: `attribute.repository == "neurify-goto/fs-runner"`
   - **対象サービスアカウント** は後段で付与するため、ここでは設定不要です。
   - プレビューで問題が無いことを確認し、**作成** をクリックします。
3. プール詳細画面に戻り、右側に表示される **リソース名**（`projects/.../locations/global/workloadIdentityPools/.../providers/...` の形式）を控えます。これは後で GitHub Secrets `GCP_WORKLOAD_IDENTITY_PROVIDER` に登録します。
4. 同じ画面の **サービス アカウントへのアクセス権** セクションで **アクセス権を付与** をクリックし、以下の設定で Terraform 実行用サービスアカウントを紐付けます。
   - **対象サービスアカウント**: 4.2.0 節で作成した `form-sender-terraform@<project>.iam.gserviceaccount.com`。
   - **ロール**: `Workload Identity User`
   - **条件**: `attribute.repository == "neurify-goto/fs-runner"`
   - 保存すると一覧に対象サービスアカウントが表示され、GitHub リポジトリからの呼び出しが許可されます。
5. GitHub リポジトリ → **Settings** → **Secrets and variables** → **Actions** に移動し、以下のシークレットを登録します。
   - `GCP_WORKLOAD_IDENTITY_PROVIDER`: 手順 3 で控えたリソース名
   - `GCP_TERRAFORM_SERVICE_ACCOUNT`: `form-sender-terraform@<project>.iam.gserviceaccount.com`
6. 既に GitHub Workflow 側（`.github/workflows/deploy-gcp-batch.yml`）では `permissions: id-token: write` が設定済みです。上記シークレットを登録すれば、`google-github-actions/auth@v2` ステップで自動的に連携されます。複数ブランチを許可する場合は、属性条件の `attribute.repository` を対象リポジトリに合わせて調整してください。

### 6.1 Cloud Build トリガーで Runner イメージを更新する

1. Cloud Console → **Cloud Build** → **トリガー** を開き、5.2.3.2 節で作成した Playwright Runner 用トリガーを選択します。
2. 右側の **実行** ボタンをクリックし、ダイアログで対象ブランチ（例: `main`）とサブスティテューションが正しいかを確認してから **実行** を押します。
3. **ビルド** タブで進行状況を確認します。完了すると緑色のチェックが表示され、ログ上部にビルド ID が記録されます。エラーが出た場合はログを開き、`docker build` や `artifactregistry` の行で失敗していないかを確認してください。
4. 成功後、Artifact Registry → リポジトリ → 対象パッケージ (`playwright`) を開き、最新タグ（例: `:latest` または コミットハッシュ）が追加されていることを確認します。GitHub Actions や Terraform で参照するタグはここに表示されたものを利用してください。

> 💡 手元での検証用にローカルビルドが必要な場合は、`gcloud auth configure-docker` → `docker build` → `docker push` を行いますが、本番運用では Cloud Build トリガー経由の更新だけで十分です。

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

これらに加えて、Cloud Run dispatcher では従来通り `FORM_SENDER_DISPATCHER_BASE_URL`, `FORM_SENDER_DISPATCHER_AUDIENCE`, `FORM_SENDER_CLOUD_RUN_JOB` などの既存環境変数も必須です。Terraform を利用する場合はモジュールで自動付与されますが、手動デプロイやローカル検証では Cloud Console → **Cloud Run** → **環境変数とシークレット** から設定するか、必要に応じて `gcloud run deploy --set-env-vars` などの CLI を利用してください。

4. `gas/form-sender/Code.gs` の `triggerServerlessFormSenderWorkflow_` は Cloud Batch モードを自動判定します。必要に応じて `resolveExecutionMode_()` を利用し、特定 targeting だけ先行移行する運用が可能です。

---

## 8. 動作確認フロー

1. **GitHub Actions でユニットテストを実行**
   - GitHub リポジトリ → **Actions** → `Deploy Cloud Batch Runner` を開き、右側の **Run workflow** ボタンを押します。
   - `apply` 入力は既定の `false` のままにすると、テストと plan のみが実行されます。ワークフロー内の「Run Cloud Batch unit tests」ステップで `pytest -k gcp_batch` が自動実行されるため、コンソールで結果を確認してください。
   - 成功すると緑のチェックが表示されます。失敗した場合はログ内の `tests/` 配下のエラー行を確認し、該当箇所を修正します。

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
