# Form Sender AWS Batch スポット移行計画書

最終更新: 2025-10-07 (JST)
作成者: オートメーションチーム
対象範囲: GAS `form-sender` モジュール / `.github/workflows/form-sender.yml` / `src/form_sender_runner.py` 系列一式
- 主要ファイル: `gas/form-sender/Code.gs`, `.github/workflows/form-sender.yml`, `src/form_sender_runner.py`

---

## 1. 背景と目的
- **現行運用:** GAS (`gas/form-sender/Code.gs`) から Supabase の send_queue を整列し、GitHub Actions (`.github/workflows/form-sender.yml`) を repository_dispatch で呼び出して Python Runner (`src/form_sender_runner.py`) を実行している。リポジトリ checkout → `pip install` → Playwright ブラウザ展開を毎回行うため、100 targeting を超える並列度ではワークフロー実行時間が 2〜3 時間に達し、ジョブあたりの課金が膨らむ。
- **Cloud Run 案:** GAS → Cloud Tasks → Cloud Run Jobs への移行を試験実装し（`docs/form_sender_serverless_setup_guide.md` / `bin/form_sender_job_entry.py` を参照）、コストシミュレーションの時点で vCPU/メモリ従量単価（vCPU $0.000018/秒, メモリ $0.000002/秒・GiB）が GitHub Actions より割高になるケースが明らかになったため、本番ジョブの実行には踏み切っていない。
- **スポット環境への適合:** Runner は `_mark_job_failed_once()` と `job_executions` テーブルを通じた状態管理を備えており、プリエンプト前提の環境でも冪等性を維持しやすい。より低コストなスポット基盤に載せ替えることで、既存の GAS トリガーと Supabase スキーマを変えずに大量 targeting を裁けるようにしたい。
- **採用方針:** AWS Batch + EC2 Spot を用い、GAS → AWS API (API Gateway or Lambda) → Batch というパイプラインを構築する。Supabase を aws-us-east-1 に移すことで Batch ↔ Supabase 間の通信を同一リージョン内に収め、データ転送料とレイテンシを削減する。

---

## 2. 目標と非目標
### 2.1 目標
1. GAS トリガーから AWS Batch へのディスパッチ経路を新設し、100 targeting 並列 / 各 4 ワーカー構成を安定運用できること。
2. Docker イメージを Amazon ECR に配置し、Spot 中断時でも冪等性が維持されるランナー起動フローを整備。
3. Secrets / 設定値の安全な注入 (AWS Secrets Manager + Parameter Store) を実装。
4. 監視・アラート・ログマスキングを AWS 環境へ転用し、CloudWatch Logs 上でも機微情報が漏えいしないよう LogSanitizer を再利用。
5. スポット中断検知とリトライ戦略を Batch / Runner の両面で明文化し、自動再投入できるようにする。

### 2.2 非目標
- Supabase RPC や send_queue テーブル構造の変更。
- Playwright ベースのフォーム送信アルゴリズムの大幅改修。
- targeting スプレッドシートの構造変更。

---

## 3. 想定アーキテクチャ概要

```
[GAS form-sender Trigger]
   │ (targeting_id, client_config_object, execution.run_total, shards, table_mode, test_mode)
   ▼
[新 API Gateway + Lambda dispatcher]
   │ 1. GAS が生成した client_config を S3 へ保存 (署名付き URL)
   │ 2. Lambda が payload 検証・Secrets 取得
   │ 3. AWS Batch SubmitJob (array job サイズ = run_total)
   ▼
[AWS Batch Job Queue]
   ▼
[Compute Environment: c6a.xlarge Spot > On-demand fallback]
   │ - タスク毎に FORM_SENDER_RUN_INDEX = AWS_BATCH_JOB_ARRAY_INDEX + 1
   │ - SUPABASE_URL / KEY を Secrets Manager から注入
   ▼
[Docker コンテナ (Playwright ランナー)]
   │ - S3 署名 URL から client_config を取得
   │ - src/form_sender_runner.py が Supabase RPC を実行
   ▼
[Supabase job_executions / send_queue]
```

補足:
- 既存の Cloud Tasks → Cloud Run 経路 (`docs/form_sender_serverless_setup_guide.md`) は短期的にフォールバックとして維持する。GAS 側で `USE_SERVERLESS_FORM_SENDER` と `USE_AWS_BATCH` の両方を制御し、段階的に AWS Batch 経路へ切り替える。
- 現行の `src/dispatcher/` 実装は Cloud Run 前提のため、API Gateway + Lambda へ機能移植した後もロジックの差分を最小化できるよう共通モジュール (`src/dispatcher/service.py` の Supabase/queue 処理) を再利用する。

---

## 4. コスト最適化戦略
1. **インスタンスタイプ選定**: `c6a.xlarge (4 vCPU / 8 GiB)` を基準に、一次リージョンを **us-east-1 (N. Virginia)** とする。スポット最小価格帯を確保するため、`us-east-2` (Ohio) をセカンダリ、`us-west-2` (Oregon) を第3候補として Compute Environment を構成する。
2. **スポット優先**: Compute Environment で Spot 比率 100%、`AllocationStrategy = SPOT_CAPACITY_OPTIMIZED` を指定。`minvCpus=0`、`desiredvCpus` を GAS 側パラメータから算出し、`maxvCpus` は 100 targeting × 4 workers をカバーできる 400 vCPU 相当で設計する。
3. **オンデマンドフォールバック**: Spot 枯渇時の SLA を確保するため、同一ジョブキューにオンデマンド Compute Environment (同インスタンスファミリ) を `order=2` でアタッチ。スポット価格がオンデマンドの 80% を超過した場合は GAS 側が同時数を減らすフェイルバックルールを適用する。
4. **中断情報活用**: Runner に `AWS_BATCH_JOB_ATTEMPT`、`AWS_BATCH_JOB_ID`、`AWS_BATCH_JOB_ARRAY_INDEX` をログ出力し、Supabase 側 `job_executions` に attempt を反映して再試行ログを可視化する。`utils/aws_batch.py` で attempt / array index を共通変換するユーティリティを提供する。
5. **データ転送料**: S3 ↔ コンテナ間は同リージョン内で無料。Supabase を **aws-us-east-1** へ移行し、Batch と同リージョン内で通信させることでクロスリージョンのデータ転送料金とレイテンシを同時に低減する。
6. **ログコスト**: CloudWatch Logs の保持期間を 14 日に短縮し、`FORM_SENDER_ENV=aws_batch` 設定で LogSanitizer を起動する。Supabase の接続文字列や企業名は GitHub Actions と同等にマスクする。
7. **S3 ストレージ**: client_config は 1 targeting ≒ 数 KB のため、`STANDARD` クラス固定とし、Lifecycle ルールで 7 日後に削除。Batch 実行完了後の Lambda によるクリーンアップに失敗した場合でもコストが肥大化しないよう 30 日自動削除をバックアップルールに設定する。

---

## 5. 実装変更一覧 (現状ギャップと対応方針)

| 領域 | 現状 (2025-10-07 時点) | 対応方針 | 対象ファイル |
| --- | --- | --- | --- |
| Python Runner | `FORM_SENDER_ENV` は `cloud_run`/`github_actions`/`local` のみ対応。`src/form_sender_runner.py` で AWS Batch 固有の環境変数 (`AWS_BATCH_JOB_ID` 等) を参照していない。`bin/form_sender_job_entry.py` は GCS URL 前提。 | `aws_batch` 環境を追加し、Batch メタを run_id / Supabase 更新に反映。S3 署名 URL 読み込み・Spot attempt 連携を実装。 | `src/form_sender_runner.py`, `bin/form_sender_job_entry.py`, `src/utils/env.py`, `config/worker_config.json`, 新規 `src/utils/aws_batch.py` |
| GAS | `gas/form-sender/Code.gs` は GitHub Actions/Cloud Run の切替のみ。`StorageClient.gs` は GCS 固定。AWS 認証周りのモジュールなし。 | `USE_AWS_BATCH` ScriptProperty を追加し、S3 アップロード + API Gateway 呼び出しに分岐。AWS SDK を UrlFetch 署名で実装するクライアントを新設。 | `gas/form-sender/Code.gs`, `gas/form-sender/StorageClient.gs`, 新規 `gas/form-sender/AWSBatchClient.gs`, `gas/form-sender/ServiceAccountClient.gs` 拡張 |
| Dispatcher | `src/dispatcher/` は Cloud Run Service 前提。Lambda 用コードは未整備。 | 共通ビジネスロジックをモジュール化しつつ、AWS Lambda ハンドラを新規作成。API Gateway 連携と Secrets/Parameter 取得を追加。 | 新設 `aws/dispatcher/app.py`, `aws/dispatcher/supabase.py` (予定), 既存 `src/dispatcher/service.py` の共通化 |
| インフラ | AWS Batch/S3/ECR/Secrets の IaC 未作成。Cloud Run 用 Terraform のみ存在。 | `infrastructure/aws/batch/` 以下に Terraform (もしくは CDK) を新設し、マルチリージョン(us-east-1/us-east-2/us-west-2)対応 Compute Environment を構築。 | `infrastructure/aws/batch/*.tf`, 変数/locals 定義, outputs |
| CI/CD | AWS デプロイ用ワークフロー無し。既存 `form-sender.yml` は GitHub Actions 実行専用。 | ECR ビルド・Terraform plan/apply・Lambda デプロイをカバーする GitHub Actions を新規作成し、Slack 通知を追加。 | `.github/workflows/deploy-aws-batch.yml` (新規)、既存ワークフローのシークレット整理 |
| 監視/運用 | CloudWatch/SNS 設定無し。LogSanitizer は GitHub Actions/Cloud Run 用パターンのみ。 | CloudWatch Logs/Metric Filter/SNS を IaC 化し、`FORM_SENDER_ENV=aws_batch` 向けパターンをログサニタイザへ追加。 | `aws/monitoring/` (新設)、`src/form_sender/security/log_sanitizer.py` |
| Supabase | 現行は ap-northeast-1。Batch との距離が長く、帯域課金・レイテンシ増大の懸念。 | Supabase プロジェクトを `aws-us-east-1` へ移行し、DB データをダンプ/リストア。接続 URL・鍵を Secrets Manager へ切替。 | `scripts/table_schema/*` (差分確認)、`config/.env` 類、Terraform から参照するシークレット名 |

---

## 6. 詳細実装計画

### 6.1 Python ランナー改修
1. `src/form_sender_runner.py`
   - 既存コードは `utils.env.get_runtime_environment()` の戻り値が `cloud_run` か `github_actions` の場合のみ分岐している。`aws_batch` を追加し、LogSanitizer 初期化・run_id 生成・Supabase ステータス更新で Batch メタを反映する。
   - `_resolve_run_id()` を改修し、`AWS_BATCH_JOB_ID`/`AWS_BATCH_JOB_ATTEMPT`/`AWS_BATCH_JOB_ARRAY_INDEX` を優先採用。無い場合は従来どおり `JOB_EXECUTION_ID` や `GITHUB_RUN_ID` にフォールバック。
   - Spot 中断 (2 分前通知) を検知できるよう、`SIGTERM` ハンドラで `_mark_job_failed_once()` を呼び Supabase 側の `attempt` をインクリメントする。必要なら `AWS_BATCH_JOB_ATTEMPT` を metadata patch に書き込む。

2. `bin/form_sender_job_entry.py`
   - `FORM_SENDER_CLIENT_CONFIG_URL` が `https://` (S3 署名 URL) か `gs://` かを判別し、S3 場合は HTTP GET、GCS 場合は既存処理と互換性を保つ。時間制限は 120 秒リトライ (指数バックオフ) を実装。
   - `delete_client_config_object()` を S3 対応へ拡張 (`s3://bucket/object` の削除 API)。Batch 実行失敗時はオブジェクト保持して再実行できるようメッセージを出す。
   - `prepare_workspace()` は Batch 実行ホストで書き込み可能な `/tmp` のみに限定し、`requirements.txt` が存在するブランチ検証時も `.venv` を `/tmp/workspace/.venv` に展開する。既存の Cloud Run 挙動にも影響がないようガードを入れる。

3. 新規ユーティリティ
   - `src/utils/aws_batch.py` を追加し、`AWS_BATCH_*` 環境変数から `BatchMeta(run_index, shard_id, attempt, array_size)` を構築。`FORM_SENDER_TOTAL_SHARDS` が未指定の場合は `JOB_EXECUTION_META` もしくは GAS payload の `shards` を利用する。

4. `config/worker_config.json`
   - `cpu_profiles` に `aws_spot` プロファイル (max_workers=4、`timeout_sec` など Batch 用パラメータ) を追加し、Cloud Run プロファイルと重複する設定はコメントで明示する。
   - Batch 実行時の Playwright メモリ上限やリトライ間隔を `runner` セクションに追加し、`src/form_sender_runner.py` の `_get_cpu_profile_settings()` から参照する。

### 6.2 GAS 改修
1. `gas/form-sender/StorageClient.gs`
   - 現在は GCS 固定 (`FORM_SENDER_GCS_BUCKET`) のため、S3 へのアップロード処理 (`uploadClientConfigToS3`) を追加する。AWS STS で取得した一時クレデンシャルを `UrlFetchApp` の Authorization ヘッダに載せ、署名付き URL を Lambda へ渡す。
   - S3 署名 URL の生成は GAS 内で行うのではなく、API Gateway/Lambda での再署名を想定し、GAS 側は `PUT` 用の pre-signed URL を `AWSBatchClient` から取得する。

2. `gas/form-sender/AWSBatchClient.gs` (新規)
   - `submit(targetingId, payload)` で API Gateway を呼び出し、レスポンスの `jobId`, `arraySize`, `submittedAt` を targeting シートに書き戻す。IAM 署名は `AWS4-HMAC-SHA256` を Apps Script 上で計算する。
   - 失敗時は指数バックオフ (1s, 4s, 9s) で 3 回リトライし、最終的に Spreadsheet のステータスを `FAILED_API_GATEWAY` に設定して再実行対象へ戻す。

3. `gas/form-sender/Code.gs`
   - Script Properties に `USE_AWS_BATCH` を追加し、`USE_SERVERLESS_FORM_SENDER` (Cloud Run 経路) と排他・併用のどちらを許可するかをフラグで制御する。初期段階は targeting ごとに `useAwsBatch` カラムで絞り込む。
   - targeting 情報から `execution.run_total`、`execution.shards`、`tables.use_extra_table`、`test_mode` を payload 化し、Supabase `job_executions` へ記録するメタ情報を含める。

### 6.3 Dispatcher (Lambda)
1. `aws/dispatcher/app.py`
   - 既存の `src/dispatcher/service.py`／`schemas.py` にあるバリデーション/ジョブ実行ロジックをモジュール化して再利用しつつ、Lambda ハンドラから呼び出す。`pydantic` ではなく `jsonschema` で Apps Script からのリクエストを検証する。
   - Secrets Manager (`/fs-runner/${env}/supabase/url`、`/key`) と Parameter Store (`/fs-runner/${env}/batch/job-definition`) から設定を読み取る。Supabase への `job_executions` insert は Lambda から直接行い、GAS との整合性を保つ。
   - `boto3.client('batch').submit_job()` で array job (`arrayProperties={'size': run_total}`) を実行。`retryStrategy` と `jobAttemptDurationSeconds` を us-east-1 のスポット特性に合わせて設定する。

2. API Gateway
   - `POST /form-sender/submit` のみを公開し、IAM 認証 (Signature Version 4) を必須化。Apps Script は `AWSBatchClient` 内で署名を生成する。
   - スロットル (Burst=50, Rate=10) と WAF ルールで誤送信時の暴発を防止。CloudWatch Logs への出力には LogSanitizer を利用し、Supabase URL・顧客名をマスクする。

### 6.4 IaC (Terraform 想定)
1. `infrastructure/aws/batch/variables.tf`
   - `environment`, `aws_region`, `supabase_secret_arn`, `batch_instance_types`, `spot_bid_percentage` 等を定義。

2. `main.tf`
   - S3 バケット (client-config)、Lifecycle Policy (7日後削除)。
   - ECR リポジトリ `fs-runner`。
   - Batch jobDefinition, computeEnvironment, jobQueue。
   - Lambda + API Gateway + IAM。

3. `outputs.tf`
   - API Gateway Invoke URL、Batch jobQueue ARN を出力し、GAS と GitHub Actions に渡す。

### 6.5 CI/CD
1. GitHub Actions ワークフロー (`.github/workflows/deploy-aws-batch.yml`)
   - `on: workflow_dispatch` + `push` (main) で起動。
   - ステップ: `aws-actions/configure-aws-credentials` → `docker build/push` → `terraform init/plan/apply` → `aws lambda update-function-code`。
   - 成功後に Slack 通知。

2. テスト自動化
   - `pytest -k aws_batch` を CI に追加。S3 アクセスは moto を利用したモックで代替。

### 6.6 Supabase リージョン移行
1. **準備**
   - 現行本番 (ap-northeast-1) のスナップショットを取得し、`pg_dump` による論理バックアップを S3 (us-east-1) に保管。
   - Supabase ダッシュボードで `aws-us-east-1` リージョンの新規プロジェクトを作成し、VPC Peering を無効化 (Batch とはパブリック通信想定)。

2. **マイグレーション**
   - `scripts/table_schema/` の最新 DDL を新プロジェクトへ適用後、`pg_restore` で `companies` / `send_queue` / `job_executions` 等のデータを移行。UUID 衝突を避けるため、移行前に停止ウィンドウを 6 時間確保する。
   - Cloud Run / GitHub Actions 経路の接続先を環境変数で切り替え、整合性が確認できたら `USE_AWS_BATCH` 対象 targeting を順次移動。

3. **切替後**
   - Secrets Manager の `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` を us-east-1 向けに更新し、Lambda/Bash Runner が新リージョンを参照することを確認。
   - 旧 supabase プロジェクトの保持期間を 30 日とし、ログを監査後に削除。DNS キャッシュ対策として GAS からの HTTP タイムアウトを 120 秒に延長。

---

## 7. マイグレーション手順
1. **PoC**: ECR へイメージ登録 → 手動 `submit_job` → 既存 Supabase (ap-northeast-1) へ書き込み確認。`FORM_SENDER_ENV=aws_batch` ブランチのローカルテストを通す。
2. **Staging**: Supabase ステージング (us-east-1) で GAS ステージングを動かし、array job / Spot 中断 (手動 `Terminate Job`) のリトライを検証。
3. **Supabase 本番移行ウィンドウ**: 作業停止 → `pg_dump` → us-east-1 プロジェクトへ `pg_restore`。移行直後に Cloud Run / GitHub Actions / GAS 全経路の接続先を更新し、read-only 検証を実施。
4. **AWS Batch 切替**: targeting ごとに `useAwsBatch` を true に切替。GitHub Actions repository_dispatch は 2 週間フォールバックとして保持し、問題なければ停止。
5. **安定化**: CloudWatch ダッシュボード公開、SNS 通知しきい値チューニング、Supabase 旧プロジェクトのアーカイブ。

---

## 8. テスト計画
| フェーズ | テスト内容 | 成功基準 |
| --- | --- | --- |
| 単体 | `src/utils/aws_batch.py` のユニットテスト | Array Index → Shard 計算が期待値通り |
| 結合 | Lambda → Batch → Runner → Supabase のエンドツーエンド | `job_executions.status` が `succeeded` になる |
| 運用 | Spot 中断を想定したシナリオテスト | `attempt` がインクリメントされ、最終的に成功 or 失敗が確定 |
| 性能 | 100 targeting / 8h 連続実行 | Timeout 率 < 5%、Supabase 500 エラー率 < 1% |

---

## 9. リスクと対策
1. **Spot 枯渇**: オンデマンド Compute Environment (order=2) と自動 SNS 通知で検知。GAS の `USE_AWS_BATCH` targeting 数を即座に減らせるオペレーション Runbook を整備する。
2. **レイテンシ/帯域**: Supabase を us-east-1 へ移行するが、移行前後で RTT が 100ms を超える場合は CloudWatch Synthetics で監視し、Batch のセカンダリリージョン (us-east-2) へ切替できるよう IaC にパラメータ化する。
3. **Secrets 漏えい**: Lambda/Batch IAM ロールは最小権限。LogSanitizer に `aws_batch` パターンを追加し、CloudWatch Logs に顧客名称や URL を残さない。
4. **GAS 呼び出し失敗**: API Gateway 呼び出しが連続失敗した場合は Spreadsheet ステータスを `FAILED_API_GATEWAY` に戻し、Apps Script のリトライ (指数バックオフ 3 回) で再送。
5. **料金高騰**: Spot 価格がオンデマンドの 80% を超過したら SNS → Slack 通知を発砲し、`desiredvCpus` を自動で 30% 削減する運用ルールを定義。オンデマンド比率が 50% を超えたら Cloud Run フォールバックを検討する。

---

## 10. スケジュール（例）
| 週 | 作業 | 成果物 |
| --- | --- | --- |
| Week 1 | AWS アカウント整備、Terraform 雛形作成 | `infrastructure/aws/batch/` 初期 commit |
| Week 2 | Lambda dispatcher / GAS クライアント実装 | ステージング SubmitJob 成功 |
| Week 3 | Runner 改修、ユニットテスト追加 | `tests/test_aws_batch_meta.py`、ECR イメージ v0.1 |
| Week 4 | 負荷・スポット中断試験 | 試験レポート、改善リスト |
| Week 5 | 本番デプロイ、72h モニタリング | CloudWatch ダッシュボード、運用 Runbook |

---

## 11. 料金試算根拠 (2025-10-07 時点)
- AWS EC2 `c6a.xlarge` us-east-1: オンデマンド **$0.153/h**、スポット平均 **$0.0653/h**（出典: aws-pricing.com, 2025-10-07閲覧）。
- Cloud Run (us-east1): vCPU $0.000018/秒、メモリ $0.000002/秒・GiB（参考: cloud.google.com/run/pricing）。
- 同一ワークロード想定: 100 targeting × 4 workers × 8 時間 × 22 営業日 → 17,600 vCPU 時間/月。
  - AWS Batch Spot: 17,600 × $0.0653 ≒ **$1,149/月**
  - AWS Batch オンデマンド: 17,600 × $0.153 ≒ **$2,693/月**
  - Cloud Run: 17,600 × ($0.000018×3600 + 8 GiB×$0.000002×3600) ≒ **$5,576/月**（従来試算と同一条件）
- Supabase は `aws-us-east-1` へ移行し、従来の ap-northeast-1 → us-east-1 間アウトバウンド料金 (約 $0.09/GB) を回避。移行後は Batch ↔ Supabase 間がリージョン内通信となり追加コストは発生しない。

---

本計画書をベースに詳細設計レビューを実施し、承認後に IaC・実装タスクを順次着手する。
