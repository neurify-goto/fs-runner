# GitHub Actions 連携ガイド（任意）

このドキュメントは、Cloud Batch / Cloud Run dispatcher の CI/CD を GitHub Actions で自動化する場合の補助資料です。GAS → Cloud Tasks → Cloud Run dispatcher → Cloud Batch の本番ルート自体は GitHub Actions を必要としません。必要になったタイミングで参照してください。

## 1. Workload Identity Federation の設定

1. Google Cloud Console → **IAM と管理** → **Workload Identity Federation** を開き、右上の **プールを作成** をクリックします。
   - **名前**: `fs-runner-gha` など判別しやすい ID。
   - **表示名**: `GitHub Actions Pool` など任意。
   - **場所**: 既定の `global` のままで構いません。
2. 作成したプールを開き、**プロバイダ** タブから **プロバイダを追加** → **OIDC** を選択します。以下の値を入力します。
   - **プロバイダ ID**: `github`
   - **発行元 URL**: `https://token.actions.githubusercontent.com`
   - **属性マッピング**:
     - `google.subject=assertion.sub`
     - `attribute.repository=assertion.repository`
   - **属性条件**: `attribute.repository == "neurify-goto/fs-runner"`（リポジトリに合わせて変更）
   - プレビューで問題が無いことを確認し、**作成** をクリックします。
3. プロバイダ詳細画面右側に表示されるリソース名（例: `projects/.../locations/global/workloadIdentityPools/.../providers/...`）を控えます。後で GitHub シークレットに登録します。
4. 同画面の **サービス アカウントへのアクセス権** セクションで **アクセス権を付与** をクリックし、Terraform 実行用サービスアカウント（例: `form-sender-terraform@<project>.iam.gserviceaccount.com`）に `roles/iam.workloadIdentityPoolUser` を付与します。必要に応じて属性条件 `attribute.repository=="neurify-goto/fs-runner"` を設定してください。

## 2. GitHub Actions のシークレット設定

GitHub リポジトリ → **Settings** → **Secrets and variables** → **Actions** から以下を登録します。

| シークレット名 | 用途 / 格納する値の例 |
| --- | --- |
| `GCP_PROJECT_ID` | Terraform / gcloud コマンドで参照するプロジェクト ID |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | 手順 1 で控えた Workload Identity Provider のリソース名 |
| `GCP_TERRAFORM_SERVICE_ACCOUNT` | Terraform 実行用サービスアカウント（例: `form-sender-terraform@<project>.iam.gserviceaccount.com`） |
| `DISPATCHER_BASE_URL` | Cloud Run dispatcher のベース URL。例: `https://form-sender-dispatcher-xxxx.a.run.app` |
| `DISPATCHER_AUDIENCE` | Cloud Tasks の OIDC Audience。通常は `DISPATCHER_BASE_URL` と同一 |
| `SUPABASE_URL_SECRET_ID` | Supabase URL を格納した Secret Manager リソースパス。例: `projects/formsalespaid/secrets/form_sender_supabase_url/versions/latest` |
| `SUPABASE_SERVICE_ROLE_SECRET_ID` | Supabase Service Role Key を格納した Secret Manager リソースパス。例: `projects/formsalespaid/secrets/form_sender_supabase_service_role/versions/latest` |
| `SUPABASE_URL_TEST_SECRET_ID` | （任意）テスト用 Supabase URL シークレットのリソースパス |
| `SUPABASE_SERVICE_ROLE_TEST_SECRET_ID` | （任意）テスト用 Service Role Key シークレットのリソースパス |

`.github/workflows/deploy-gcp-batch.yml` では `google-github-actions/auth@v2` を利用して上記シークレットを参照します。ワークフローに `permissions: id-token: write` が指定されていることを確認してください。

> ℹ️ `_SECRET_ID` 系のシークレットには **Secret Manager のリソースパス**（`projects/<project>/secrets/<name>/versions/latest` 形式）をそのまま入力してください。Terraform ワークフローがこの値をそのまま `google_secret_manager_secret_version` に渡すため、空文字や単なる URL を設定すると plan/apply が失敗します。

## 3. Cloud Build トリガーの使い方

1. Cloud Console → **Cloud Build** → **トリガー** を開き、Playwright Runner／Dispatcher 用トリガーを選択します。
2. 右側の **実行** ボタンから対象ブランチ（例: `main`）でビルドし、ログで `docker build` → `docker push` → Artifact Registry 反映まで成功していることを確認します。
3. 完了後、Artifact Registry → リポジトリ → 対象パッケージを開き、`latest` やコミットハッシュタグが更新されていることを確認します。GitHub Actions で参照するタグはここで確認したものを使用してください。

※ ローカルでの検証が必要な場合は `gcloud auth configure-docker` → `docker build` → `docker push` を実行しても構いませんが、本番では Cloud Build トリガー経由での更新のみを推奨します。

## 4. Terraform ワークフロー例

ワークフロー（例: `deploy-gcp-batch.yml`）の流れは以下のとおりです。

1. `google-github-actions/auth@v2` で Workload Identity Federation を利用して認証。
2. `hashicorp/setup-terraform` で Terraform をセットアップ。
3. `terraform init` → `terraform plan` を実行し、必要に応じて `workflow_dispatch` の入力値で `terraform apply` を有効化。
4. Supabase や Cloud Run/Batch の設定値は `.tfvars` または GitHub Secrets から環境変数として注入します。

## 5. 運用メモ

- GitHub Actions は任意のオプションです。GAS → GCP の本番ワークフローは本ドキュメントが無くても動作します。
- シークレット値は定期的にローテーションし、不要になったサービスアカウント権限は削除してください。
- 追加ブランチ・別リポジトリでも利用する場合は、Workload Identity Provider の属性条件（`attribute.repository` や `attribute.ref`）を調整します。

参照: メインのセットアップ手順は `docs/form_sender_gcp_batch_setup_guide.md` を参照してください。
