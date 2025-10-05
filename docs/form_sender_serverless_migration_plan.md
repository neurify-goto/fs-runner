# Form Sender サーバーレス移行計画書

最終更新: 2025-10-02 (JST)
作成者: オートメーションチーム（草案）
対象範囲: GAS `form-sender` モジュール / `.github/workflows/form-sender.yml` / `src/form_sender_runner.py` 系列一式

---

## 1. 背景と目的
- 現状は GAS が GitHub Actions の `form-sender` ワークフロー（`.github/workflows/form-sender.yml:1`）を repository_dispatch で起動し、Ubuntu ランナー上で Playwright・Supabase RPC を実行している。
- GitHub Actions の可用性・Secrets 管理・長時間処理時の安定性・Playwright 依存ライブラリの毎回インストール負荷が増大している。
- 目標は **GAS → GCP サーバーレスコンテナ → Supabase** へフローを置き換え、GitHub Actions 依存を排除しつつ既存のフォーム送信ロジックとマルチワーカー挙動を維持すること。

---

## 2. 現状アーキテクチャ整理
### 2.1 GAS オーケストレーション
- `gas/form-sender/Code.gs` の時間トリガー関数が `processTargeting()` を通じ targeting 単位で連携。
- `triggerFormSenderWorkflow()` → `sendRepositoryDispatch()`（`gas/form-sender/GitHubClient.gs:86`）が GitHub Actions を起動。
- ワークフロー並列数は targeting シート `concurrent_workflow` により決定（GAS が run_total に設定）。
- 各ワークフロー内の Python ワーカー数は GAS 内定数 `CONFIG.WORKERS_PER_WORKFLOW`（workers_per_workflow）から決まり、Runner の `--num-workers` に渡される（現行値は 4）。

### 2.2 GitHub Actions ワークフロー
- `.github/workflows/form-sender.yml` は Ubuntu ランナーで Python 3.11 / Playwright / apt 依存を毎回セットアップし、`src/save_client_config.py` → `src/form_sender_runner.py` を実行。
- 実行後に `/tmp/client_config_*` を破棄。

### 2.3 Python Runner の責務
- `src/form_sender_runner.py` は Supabase RPC を用いて send_queue を claim → Playwright 送信 → 成功時に mark_done で確定し、当日送信済みの重複検知時は `SKIPPED_ALREADY_SENT_TODAY` として mark_done（再送キューへの戻しは行わない）。Supabase 側の例外などで重複判定が失敗した場合のみ、assigned 行を pending へ戻すフォールバック更新を実施して再試行に委ねる。
- `src/save_client_config.py` は GitHub Actions 固有の `GITHUB_EVENT_PATH` に依存。

### 2.4 現状課題
- GitHub Actions の起動遅延・同時実行制限がピーク性能を阻害。
- Secrets を GitHub に配布する運用コストが高い。
- CI ログサニタイズ等 GitHub Actions 固有の条件分岐がコードに増加。
- GAS → GitHub API 依存による rate limit / token ローテーションが負担。

---

## 3. 移行後のゴール & 非ゴール
- **ゴール**
  - GitHub Actions を排し、コンテナ化した Runner を GCP サーバーレス基盤上でオンデマンド実行。
  - GAS から targeting 単位で実行できる API を用意し、既存マルチワーカー・シャーディング挙動を維持。
  - Secrets (Supabase URL/KEY 等) を GCP Secret Manager で集中管理し、ログサニタイズ方針を継続。
  - Playwright 依存パッケージを Docker イメージにバンドルし起動時間を短縮。
- **非ゴール**
  - Supabase RPC / テーブル設計の抜本的変更。
  - `form_sender` パッケージのロジック刷新。
  - targeting スプレッドシート構造の変更。

---

## 4. 目標アーキテクチャ案

> **進捗ステータス (2025-10-03 時点)**
>
> - 以下の内容は「最終的に目指す構成」をまとめた計画書であり、まだ本番にリリースされていません。
> - Cloud Tasks/dispatcher/Cloud Run Job の接続コード・GitHub Actions からの切替・運用 Runbook などは別途実装タスクで進行中です。
> - `USE_SERVERLESS_FORM_SENDER=true` への切替は、Dispatcher 本番デプロイ完了と総合テスト（§8, §9）が揃うまで **絶対に行わないでください**。

```
[GAS トリガー]
   │  (targeting_id, client_config, run_index, use_extra_table 等)
   ▼
[Cloud Tasks]
   │  (サービスアカウント認証, リトライ制御)
   ▼
[Cloud Run Service "dispatcher"]
   │  1. HTTP POST payload を受信（GAS から 1 targeting = 1 タスク）
   │  2. GAS の `StorageClient`（新設）が GCS にアップロードした client_config の V4 署名付き HTTPS URL（payload.client_config_ref, 有効期限 15 時間）と GCS オブジェクトパス（payload.client_config_object）を検証
   │  3. Jobs API `projects.locations.jobs.run` を呼び出し
   │     - `taskCount = payload.execution.run_total`
   │     - `parallelism = payload.execution.parallelism`
   │     - `overrides.env` に targeting 情報・シャーディング条件・GCS URL を付与
   ▼
[Cloud Run Job "form-sender-runner"]
   │  1. 起動時引数・環境変数から metadata を取得
   │     - `CLOUD_RUN_TASK_INDEX`・`CLOUD_RUN_TASK_COUNT` などタスク固有環境変数を参照
   │     - dispatcher から渡された `JOB_EXECUTION_META`（Base64 JSON）を復元
   │  2. タスク固有の `run_index` と `shard_id` を算出
   │     - `run_index = payload.execution.run_index_base + CLOUD_RUN_TASK_INDEX + 1`  // 1 オリジンを維持
   │     - `shard_id = ((run_index - 1) % payload.execution.shards)`
   │  3. GCS から client_config をダウンロード → `/tmp/client_config.json` を生成
   │     - `FORM_SENDER_CLIENT_CONFIG_URL` を署名付き URL として受け取り、失効前に取得
   │     - `--shard-id` には算出結果を渡す
   │     - `--num-workers` は payload 指定値と `FORM_SENDER_MAX_WORKERS` の最小値
   │  4. Supabase RPC / ログ出力
   ▼
[Supabase] (claim_next_batch / mark_done)
   ▼
[Cloud Logging / Error Reporting]
```
- Cloud Run Job は HTTP リクエストを受けられないため、GAS からの payload は Cloud Run Service (dispatcher) が受け、Jobs API で `RunJobRequest` を発行して渡す。
- dispatcher Service は 60 秒以内に応答可能な軽量処理のみ担い、Job が 24 時間（Cloud Run Jobs の公式上限）までの長時間処理を担当。ただし運用上は 07:00〜19:00（JST）の営業時間内に完了するようトリガーし、Cloud Tasks 側で TTL を 19:00 JST までに設定して営業時間外の実行を抑止する。
- targeting ごとの shard/run_index は dispatcher が埋め込む `JOB_EXECUTION_META`（`run_index_base` / `shards` / `workers_per_workflow` 等）とシステム変数 `CLOUD_RUN_TASK_INDEX` を組み合わせて Job エントリーポイントが導出し、Supabase RPC (`p_shard_id`) に渡す。

### 4.1 Cloud Tasks → dispatcher payload スキーマ
```jsonc
{
  "targeting_id": 123,
  "client_config_ref": "https://storage.googleapis.com/fs-runner-client-config/2025-10-02/run-123.json?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=...&X-Goog-Expires=54000&X-Goog-Signature=...", // HTTPS V4 signed URL (15h TTL)
  "client_config_object": "gs://fs-runner-client-config/2025-10-02/run-123.json", // fallback object path for re-signing
  "tables": {
    "use_extra_table": false,
    "company_table": "companies",
    "send_queue_table": "send_queue"
  },
  "execution": {
    "run_total": 3,          // targeting の concurrent_workflow 値
    "parallelism": 3,        // Cloud Run Job の parallelism（≤ run_total）
    "run_index_base": 0,     // 1 オリジン算出のため 0 始まりで保持（run_index = base + CLOUD_RUN_TASK_INDEX + 1）
    "shards": 8,             // send_queue 作成時の shards 設定
    "workers_per_workflow": 4// form_sender_runner の --num-workers 既定値
  },
  "test_mode": false,
  "branch": "feature/abc",          // 任意: 手動テスト時に参照する Git ブランチ/コミット
  "workflow_trigger": "manual",     // automated | manual | branch_test
  "metadata": {
    "triggered_at_jst": "2025-10-02T07:00:00+09:00",
    "gas_trigger": "startFormSenderFromTriggerAt7"
  }
}
```
- `run_index_base` は GAS が targeting_id 単位で保持する 0 始まりのカウンタで、Job 側で `run_index = run_index_base + CLOUD_RUN_TASK_INDEX + 1` と計算して 1 オリジンを維持する。GAS が複数 targeting を順次処理する場合も、日次でリセットする単純なカウンタを維持する。
- `run_total` は従来の `concurrent_workflow` 値をそのまま採用する。`parallelism` は `resolveParallelism(concurrent_workflow)`（GAS 側で新設）で算出し、既定では `run_total` と同値を返す。`ScriptProperties.FORM_SENDER_PARALLELISM_OVERRIDE` に 1〜run_total の値が設定されている場合のみその値で上書きし、未設定や 0/空文字の場合は override せず現行性能を維持する。これにより負荷が高い時間帯だけ即時にスロットルできる。Cloud Tasks 側では 1 targeting につき payload 1 件のみ投入し、dispatcher は `taskCount = run_total`・`parallelism = parallelism` を Jobs API に渡す。
- `workers_per_workflow` は Cloud Run Job → form_sender_runner の `--num-workers` に渡され、`FORM_SENDER_MAX_WORKERS` との最小値が採用される。
- Cloud Tasks `taskName` は `fs-YYYYMMDD-targetingId-runIndexBase` 形式で固定化し、同一ターゲティング・run_index_base の二重投入を防ぐ。
- `branch` と `workflow_trigger` は workflow_dispatch / form_sender_branch_test 相当の手動動線を再現するために使用し、dispatcher が `client_config_ref`（HTTPS 署名 URL）と `client_config_object`（gs:// パス）を Job に渡す。未指定時は本番運用とみなす。

### 4.2 ブランチ/手動テスト動線
- GAS の `processTargeting()` からテストを起動する場合、`triggerFormSenderWorkflow()` のオプションで `branch`（Git リファレンス）と `test_mode=true` を渡し、Cloud Tasks payload に反映する。
- dispatcher は `branch` が指定された場合、Job へ `FORM_SENDER_GIT_REF` と Secret Manager の GitHub PAT／デプロイキーを `FORM_SENDER_GIT_TOKEN`（環境変数）として渡し、エントリーポイントで `GIT_ASKPASS=/tmp/git-askpass.sh`・`GIT_TERMINAL_PROMPT=0` を設定した上で `git clone --depth=1 --branch "$FORM_SENDER_GIT_REF" https://github.com/neurify-goto/fs-runner.git /tmp/workspace` を実行する。`git-askpass.sh` は実行時にのみ生成し、内容を `#!/bin/sh` と `exec printf '%s\\n' "${FORM_SENDER_GIT_TOKEN}"` で構成して chmod 700 → clone 成功後すぐに `shred` + `rm` することでトークンがプロセス引数やログに出力されないようにする。PAT/デプロイキーは read-only 権限 + IP 制限を必須とし、Job 完了後は `unset FORM_SENDER_GIT_TOKEN` と `/tmp/workspace` の削除を徹底する。
- Cloud Run Job はコンテナルートが読み取り専用のため、ブランチテスト時は `/tmp/workspace` 配下に shallow clone し、`PYTHONPATH` を調整してテスト対象コードを読み込む。`--ephemeral-storage=4Gi` を指定しているため `/tmp` には最大約 4 GiB まで書き込めるが、Playwright キャッシュや依存インストールを含め 3 GiB 以内に収まるよう `git clone --depth=1` と不要ファイル削除を徹底し、処理後は `/tmp/workspace` を確実に削除する。
- clone 後に追加依存が必要な場合は、Cloud Run の読み取り専用ルートを避けるため `PIP_FIND_LINKS=/opt/pip-wheelhouse` を指定しつつ `pip install --upgrade --target /tmp/workspace/.venv/lib/python3.11/site-packages --requirement /tmp/workspace/requirements.txt --cache-dir ${PIP_CACHE_DIR:-/tmp/pip-cache}` を実行し、`PYTHONPATH=/tmp/workspace/.venv/lib/python3.11/site-packages:$PYTHONPATH` を設定して差分を読み込む。容量が閾値を超える場合は Phase 2 の検討項目として、当該ブランチの一時イメージを Cloud Build で作成する運用へ切り替える。
- 依存差分が大きくパフォーマンスへ影響する場合に備え、Cloud Build でテスト対象ブランチの一時イメージをビルドするオプションを Phase 2 の検討項目に残し、`pip --target` 方式の適用条件（差分サイズ、INSTALL 時間）と切り替え基準を Runbook にまとめる。
- 手動実行（workflow_dispatch 相当）は GAS から `workflow_trigger="manual"` と `test_mode=true` を指定することで区別し、Supabase への書き込みやログ出力パスを軽量化する。

---

## 5. 技術選定と根拠
| 項目 | 候補 | 判定 | 根拠 |
|------|------|------|------|
| 実行基盤 | Cloud Run Jobs / Cloud Run Service | ✅ Cloud Run Jobs (実行), Cloud Run Service (制御) | Job が Playwright/Chromium を含むバッチを最大 24h 実行可能。HTTP は Service で受け、Jobs API で引数・環境を上書き可能 |
| 起動経路 | GAS → Cloud Tasks → Cloud Run Service → Jobs API | ✅ 採用 | Cloud Tasks で遅延・リトライ・キュー統制を実現し、Service が Jobs API を代理呼び出し |
| コンテナビルド | Cloud Build | ✅ | GCP 内でイメージをビルドし Artifact Registry に配置 |
| Secrets 管理 | Secret Manager | ✅ | Supabase Service Role Key 等を安全に注入 |

---

## 6. 実装タスク詳細
### 6.1 コンテナ基盤整備
 - Playwright 対応ベースイメージ（例: `mcr.microsoft.com/playwright/python:v1.40.0-jammy`）で `Dockerfile` を作成。
  - `pip install -r requirements.txt` をビルド時実行し、`playwright install chromium --with-deps` を RUN で実施。
  - GitHub Actions で導入していた `libnss3`, `libxss1`, `libasound2t64` 等を Dockerfile に明示。
  - `COPY src/ /app/src`, `COPY config/ /app/config`, `COPY scripts/ /app/scripts` などでアプリ本体・設定ファイルをイメージに配置し、`WORKDIR /app` で実行パスを統一する。
  - Cloud Tasks / Cloud Run / GCS を操作するため `google-cloud-tasks`, `google-cloud-run`, `google-cloud-storage`, `google-auth` など必要な GCP クライアントライブラリを `requirements.txt` に追加し、バージョン管理を行う。
- ブランチテストのセットアップ時間を抑えるため、Phase 0 の Docker ビルドで `pip install -r requirements.txt --target /opt/pip-wheelhouse` を実行して wheel を焼き込み、Job 実行時は `PIP_FIND_LINKS=/opt/pip-wheelhouse`（必要に応じ `PIP_NO_INDEX=0`）を設定したうえで `pip install --upgrade --requirement /tmp/workspace/requirements.txt --cache-dir ${PIP_CACHE_DIR:-/tmp/pip-cache}` を実行して差分インストールのみに絞る。Cloud Run Job の `/tmp` は実行毎に初期化されるため永続キャッシュは期待せず、Phase 1 で (a) Cloud Build によるブランチ専用イメージ ビルド、(b) Artifact Registry での wheelhouse 配布 の 2 案を比較検証し、より効果的なキャッシュ戦略を採用する。ジョブ終了時には `PIP_CACHE_DIR` を削除する運用を合わせて定義する。
  - ブランチテストで git clone を行うため、イメージビルド時に `apt-get install -y git` を追加し、Playwright 公式イメージに git が含まれないケースに備える。
- Cloud Run Jobs のリソース指定は `--cpu=4 --memory=14Gi --ephemeral-storage=4Gi` を初期値とし、現行設定 `config/worker_config.json` の `form_sender_multi_process.memory_per_worker_mb=3072` と GAS `CONFIG.WORKERS_PER_WORKFLOW=4` から算出した 4 ワーカー構成（約 12 Gi 消費）に 2 Gi のヘッドルームを加えた値を確保する。メモリ閾値をコードで扱う際は、同ファイルのコメントに合わせて `memory_per_worker_mb × workers_per_workflow + system_reserve` の積算式を用い、将来的に `resource_validation.total_memory_mb` を追加する場合でも整合性が取れるようにする。リソース不足が判明した場合のみワーカー数を 3 に丸めるが、Cloud Run メモリは 14 Gi 未満には縮小しない。
- Job 用エントリーポイント (`src/form_sender_job_entry.py` 仮称) を追加し、以下を担当:
  1. `JOB_EXECUTION_META` を復号し JSON を生成。
  2. `CLOUD_RUN_TASK_INDEX`・`CLOUD_RUN_TASK_COUNT` と payload.execution の `run_total` / `shards` 情報からタスク固有の `run_index`・`shard_id` を算出。（例: `run_index = run_index_base + CLOUD_RUN_TASK_INDEX + 1`, `shard_id = (run_index - 1) % shards`）
  3. `FORM_SENDER_CLIENT_CONFIG_URL`（署名付き URL）をダウンロードし `/tmp/client_config.json` を作成、`FORM_SENDER_RUN_INDEX`・`FORM_SENDER_SHARD_ID`・`FORM_SENDER_TARGETING_ID` 等の環境変数を設定。
  4. ブランチテスト時は `FORM_SENDER_GIT_REF` を検出し、Secret Manager に保管した GitHub PAT（`FORM_SENDER_GIT_TOKEN`）または GitHub App インストールトークンを `GIT_ASKPASS` スクリプト経由で注入して `git clone https://github.com/neurify-goto/fs-runner.git` を `/tmp/workspace` に shallow clone する。コマンドライン／プロセステーブルにトークン文字列を残さないよう、ジョブ起動時に `/tmp/git-askpass.sh`（`#!/bin/sh\nexec printf '%s\\n' "${FORM_SENDER_GIT_TOKEN}"`）を生成し、`chmod 700` の上で `env GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/tmp/git-askpass.sh` を付けて実行する。Phase 1 では GitHub App or Artifact Registry によるブランチ差分配布へ移行する可否も検証する。clone 後は `pip install --upgrade --requirement /tmp/workspace/requirements.txt --cache-dir ${PIP_CACHE_DIR:-/tmp/pip-cache}` を実行して差分依存のみ反映し、処理完了後に PAT を `unset` して `rm -rf /tmp/git-askpass.sh ${PIP_CACHE_DIR:-/tmp/pip-cache} /tmp/workspace` で掃除する。
  5. `FORM_SENDER_GIT_REF` が指定されている場合は `/tmp/workspace/src/form_sender_runner.py` を実行し、指定が無い場合はイメージ内 `/app/src/form_sender_runner.py` を実行する。
  6. `python ${FORM_SENDER_RUNNER_PATH:-/app/src/form_sender_runner.py} --targeting-id ${FORM_SENDER_TARGETING_ID} --config-file ${FORM_SENDER_CLIENT_CONFIG_PATH} --run-index {run_index} --shard-id {shard_id}` をサブプロセス起動し、必要に応じ `--num-workers` を payload から上書き。CLI 側の `--config-file` は引き続き必須とし、エントリーポイントで常にパスを明示する方針とする。
  7. 終了コード・ログ整形を統一。
  8. `JOB_EXECUTION_META.workers_per_workflow` と dispatcher から渡される `FORM_SENDER_MAX_WORKERS` を読み取り、`--num-workers` 引数・`FORM_SENDER_MAX_WORKERS` 環境変数・`FORM_SENDER_WORKERS_FROM_META`（新設）を同期してから Runner を起動する。override が無い場合は `payload.execution.run_total` をそのまま `--num-workers` に指定し、Cloud Run Job の `parallelism` と一致させる。これにより `_worker_entry` 側の `resolve_worker_count()` が `company_id` フラグや CLI オプションを考慮して最終的なワーカー数を決定できる。

### 6.2 Runner / ユーティリティ改修
- `src/save_client_config.py` を CLI ツール化し、JSON 文字列入力 → 任意パス出力に対応。GitHub Actions 用ラッパーは別モジュール化して後方互換を維持。
- `src/form_sender_runner.py` に以下を追加:
  - `RUN_ENV` 判定で Cloud Run 実行時は `_install_logging_policy_for_ci()` の CI モード（`GITHUB_ACTIONS=true` 前提の JSONFormatter + STDOUT 再設定）をバイパスし、Cloud Logging 向け JSON 形式 (optional) を提供する。`setup_sanitized_logging()` による企業名・URL マスキングは既に常時有効なため、Cloud Run では「出力量の調整と JSON 形式化」を追加で行うのが狙いとなる。`FORM_SENDER_LOG_SANITIZE=1` を導入して `GITHUB_ACTIONS` 判定を置き換え、CI 専用ハンドラの二重 `basicConfig` 呼び出しを避けながら Cloud Logging 向けのフィルタリングを適用する。
  - ログ判定用環境変数は `FORM_SENDER_ENV`（`cloud_run` / `github_actions` / `local`）と `FORM_SENDER_LOG_SANITIZE`（`1`/`0`）に一本化する。Phase 0 では `FORM_SENDER_ENV` が未設定の場合に限り既存の `GITHUB_ACTIONS` 判定へフォールバックし、Phase 1 で `GITHUB_ACTIONS` 依存を段階的に撤廃する。置換対象モジュール: `src/form_sender_runner.py`, `src/form_sender/security/log_sanitizer.py`, `src/form_analyzer/worker.py`, `src/form_finder/orchestrator/manager.py`, `src/form_finder_worker.py`, `src/form_sender/validation/config_validator.py`, `config/validation.json`, `tests/*`。Validation 方針: `config/validation.json` に `FORM_SENDER_ENV` の列挙値を追加し、`FORM_SENDER_LOG_SANITIZE` を boolean (0/1) として許可、`GITHUB_ACTIONS` は互換期間中は deprecated 扱いで警告ログを出す。Terraform/CI の環境変数定義は Phase 0 で並行更新し、Phase 2 で `GITHUB_ACTIONS` の読み取りを削除するロードマップとする。
  - `_worker_entry` とシャード関連ユーティリティに `resolve_shard_config()`（新設）を導入し、`FORM_SENDER_TOTAL_SHARDS` → `JOB_EXECUTION_META.shards` → `CONFIG.FORM_SENDER_SHARD_COUNT`（既定 8）→ `config/worker_config.json` の `runner.shard_num` の順で値を決定する。戻り値には `total_shards`, `rotation_enabled`, `rotation_strategy`, `no_work_probe_seconds` 等を含め、既存の `runner.shard_num` や `shard_rotation_enabled` 参照箇所（例: `src/form_sender_runner.py:1381-1393`）をこのヘルパー経由に差し替える。`_worker_entry` 起動時に環境変数 `FORM_SENDER_TOTAL_SHARDS` があれば優先し、指定が無い場合は `JOB_EXECUTION_META` を展開して取得する。`shard_id` は従来どおり `run_index` から算出するが、生成時に `total_shards` を使用する。
  - 既存の CLI 互換性: 手動テストや GitHub Actions 経路では `--shard-id`／`--shard-count` など CLI 引数の指定を許容し、引数が与えられた場合は `resolve_shard_config()` が戻り値の `total_shards` を上書きせず CLI 値を優先するフォールバック分岐を設ける。これにより既存の `form_sender_runner.py --shard-id 3 --num-workers 2` といった手動確認コマンドが動作し続ける。新設ヘルパーは CLI 明示値を検知した際に警告ログを出し、Cloud Run 実行時は環境変数経路が使われていることを確認するテレメトリを追加する。
  - `resolve_worker_count(args, env, meta)` を新設し、(1) `company_id` 指定時は常に 1 ワーカー、(2) `FORM_SENDER_MAX_WORKERS` 環境変数、(3) `JOB_EXECUTION_META.workers_per_workflow`、(4) CLI の `--num-workers`（Cloud Run エントリーポイントが `parallelism` もしくは override を設定）をこの順で比較し、`max(1, min(...))` で最終値を決定する。結果はログに出力し、`FORM_SENDER_ACTIVE_WORKERS` 環境変数へ書き戻して子プロセスに共有する。GitHub Actions 実行時は `JOB_EXECUTION_META` が無い想定のため、既存挙動と同じく CLI 値を優先するフォールバックを維持する。
  - CLI 互換性と Cloud Run 環境変数の共存を担保するため、`resolve_worker_count()` では `args.num_workers` が明示的に指定されている（デフォルト以外の）場合はそれを最終値として採用し、環境変数経路との差分を WARN ログに出す。Cloud Run エントリーポイントは常に `--num-workers` を指定する一方、GitHub Actions では従来どおり CLI の `--num-workers` が唯一のソースとなる。設計書上も `FORM_SENDER_WORKERS_FROM_META` が存在しない環境では後方互換が維持される旨を明記する。
  - `--run-index` CLI オプションと `FORM_SENDER_RUN_INDEX` 環境変数を追加し、Job 側からタスク固有の実行 ID を受け取れるようにする。
- `FORM_SENDER_MAX_WORKERS` 環境変数でワーカー数上限を制御。現行の `main()` では `min(4, max(1, args.num_workers))` にハードコードされているため、`resolve_worker_count()`（新設）で `FORM_SENDER_MAX_WORKERS` を整数として読み込み、`company_id` 指定時は強制的に 1、通常時は `min(env 上限, workers_per_workflow, args.num_workers)` を `max(1, …)` でクランプする実装へ置き換える。環境変数が未指定の場合は dispatcher から渡される `JOB_EXECUTION_META.workers_per_workflow`（GAS `CONFIG.WORKERS_PER_WORKFLOW` から転記）を優先し、それも欠落している場合のみ CLI 既定の 4 を採用する。メタ情報と CLI 指定値が乖離した場合は警告ログを出し最小値で調整する。`main()` の `worker_count` 算出はこのヘルパーの戻り値に差し替える。
  - `FORM_SENDER_TOTAL_SHARDS` （dispatcher が設定）と `RUN_INDEX` を受け取り、`_worker_entry` へ渡す `shard_id` を再計算。Supabase RPC 呼び出し時の `p_shard_id` に確実に渡す。環境変数が指定されない場合は `JOB_EXECUTION_META.shards` を参照し、そこにも値が無い場合のみ初期シャード数（GAS に新設する `CONFIG.FORM_SENDER_SHARD_COUNT` の既定 8）を使用する。`JOB_EXECUTION_META.shards` は GAS 側で `createQueueForTargeting` に渡した値と同一に保つ。モジュール読み込み時に決まってしまう `COMPANY_TABLE`／`FN_CLAIM` 等は廃止し、`apply_table_mode(mode: str)`（新設）を `main()` で呼んで `FORM_SENDER_TABLE_MODE` と CLI `--table-mode` から決定する。ヘルパー内で `COMPANY_TABLE`・`SEND_QUEUE_TABLE`・`FN_CLAIM`・`FN_MARK_DONE`・`FN_REQUEUE` を辞書ベースで再構成し、既存の extra/test フォールバック処理も同関数に集約する。
- `FORM_SENDER_CLIENT_CONFIG_PATH` を指定可能にし、エントリーポイントがダウンロードしたファイルを Runner が直接参照できるようにする。
- `FORM_SENDER_CLIENT_CONFIG_PATH` を指定可能にし、エントリーポイントがダウンロードしたファイルを Runner が直接参照できるようにする。Phase 0 で `save_client_config.py` を CLI 化する際に `--output-path`（必須 or 既定 `/tmp/client_config.json`）を追加し、Cloud Run 経路では dispatcher が常に `/tmp/client_config.json` を指定するよう統一する。GitHub Actions やローカル実行では明示的に `--output-path` を渡して衝突を避ける運用を Runbook に記載する。
- `FORM_SENDER_GIT_REF` が存在する場合は `git checkout` を行う手順を Runner 側でサポートし、テストブランチ実行でも同一バイナリを利用可能にする。
- `--test-mode` CLI オプションと `FORM_SENDER_TEST_MODE` 環境変数を追加し、テストフラグが true の場合は `test_mode` 用のテーブル（例: `send_queue_test`, `submissions_test`）へ書き込み、mark_done 呼び出し前に Supabase をテスト環境向けに切り替えるなどの安全運転を定義する（詳細は Phase 1 で実装）。
- Supabase `job_executions` テーブルへの結果更新ロジック（開始時に INSERT 済みレコードを `status=running` に更新、完了時に `status=success|failed`, `ended_at`, `attempt` を UPDATE）を実装し、dispatcher と整合させる。
- `FORM_SENDER_TABLE_MODE`（extra / default / test）をログ出力や挙動分岐に使用し、追加メトリクスにも反映する。`apply_table_mode()` は mode に応じて Supabase RPC 名／テーブル名を決定した後、環境変数への書き戻しと `os.environ` 更新を行い、ワーカー生成前に呼び出す。テストモードは `--test-mode` フラグまたは `FORM_SENDER_TEST_MODE` が true の場合に mode=`test` へ昇格させる。
- テストモードとテーブルモードに応じて Supabase RPC 名も切り替えられるよう `FN_CLAIM` / `FN_MARK_DONE` / `FN_REQUEUE` を再構成し、必要に応じてテスト用 RPC を追加実装（Supabase 側に `claim_next_batch_test`, `mark_done_test` 等を用意）。`apply_table_mode()` は mode ごとの RPC 名を戻り値で返し、`main()` から `_worker_entry` や Supabase 呼び出しユーティリティに渡す形へ改修する。
- mark_done は成功時に加えて現行同様のスキップ判定（例: `SKIPPED_ALREADY_SENT_TODAY`, `SKIPPED_BY_NAME_POLICY`）でも呼び出し、非再試行エラーを Supabase に確定させる。Supabase 例外で重複判定自体が失敗したケースに限り、既存コードと同じ pending へのフォールバック再割り当てを実行する。
- `RUN_ID` を `JOB_EXECUTION_ID`（dispatcher が付与）・`RUN_INDEX`・`CLOUD_RUN_TASK_ATTEMPT`（リトライ回数）・タイムスタンプの組み合わせで構築し、Cloud Logging 上でもタスク単位の識別ができるようにする。`build_run_id(job_execution_id, run_index, attempt)`（新設）を `main()` で呼び、`JOB_EXECUTION_ID` / `FORM_SENDER_RUN_INDEX` / `CLOUD_RUN_TASK_ATTEMPT` を優先的に参照しつつ、従来の `GITHUB_RUN_ID` fallback も残す。同ヘルパーで生成した run_id を `_worker_entry` の引数・Supabase RPC（`p_run_id`）・ログに渡す。
- LogSanitizer やその他 GITHUB_ACTIONS 判定に依存するモジュール（browser.manager, log_auditor, config_validator, validation_config, form_finder.orchestrator.manager, form_finder_worker, form_analyzer.worker, form_sender_runner 等）には `FORM_SENDER_ENV=cloud_run` / `FORM_SENDER_LOG_SANITIZE=1` を導入し、Cloud Run ではこれらを参照して CI 相当のマスクやログ抑制を適用する（`GITHUB_ACTIONS` 依存を段階的に置き換える）。
  - 環境変数バリデーション（`config/validation.json`, `validation_config.py`）も `FORM_SENDER_ENV` / `FORM_SENDER_LOG_SANITIZE` を許可するよう更新し、Cloud Run でも起動条件を満たせるようにする。

### 6.3 dispatcher Service 実装
- Cloud Run Service (Python FastAPI 等) を新設:
  1. Cloud Tasks から HTTP POST を受信。
  2. payload を検証 (`targeting_id`, `client_config_ref`, `client_config_object`, `execution`, `tables`, `test_mode`, `branch`, `workflow_trigger`)。`client_config_ref` は GAS 側で新設する `StorageClient` が生成した V4 署名付き HTTPS URL（RSA-SHA256 署名、15 時間有効、`Content-Type: application/json`）、`client_config_object` は同一ファイルを表す `gs://` パスであることを確認し、署名期限・バケット・オブジェクト名のフォーマットをチェックする。
  3. dispatcher は `client_config_ref` に対して署名済み `HEAD` リクエストを実行して存在確認を行い、レスポンスヘッダーで残存有効時間が 30 分未満または 4xx エラーの場合は dispatcher サービスアカウント（`roles/storage.objectAdmin` + `roles/iam.serviceAccountTokenCreator`）を用いて `client_config_object` から新しい V4 署名 URL を再生成する。再署名も失敗した場合はステータスコード 422 で Cloud Tasks へリトライを指示し、GAS 側に再アップロードを促す。
  4. Jobs API `projects.locations.jobs.run` を呼び出し、`taskCount = execution.run_total`、`parallelism = execution.parallelism`（未指定時は `taskCount` と同値）でジョブを起動。
     - `overrides.containerOverrides` に 1 件の要素を設定し、その `args` へエントリーポイントを指定する。
     - 同じく `overrides.containerOverrides[0].env` に `FORM_SENDER_CLIENT_CONFIG_URL`, `FORM_SENDER_MAX_WORKERS`, `FORM_SENDER_TOTAL_SHARDS`, `FORM_SENDER_GIT_REF`, `FORM_SENDER_WORKFLOW_TRIGGER`, `COMPANY_TABLE`, `SEND_QUEUE_TABLE`, `FORM_SENDER_GIT_TOKEN`（ブランチテスト時のみ）、`JOB_EXECUTION_META`, `JOB_EXECUTION_ID` などを設定。Extra テーブル指定時は `COMPANY_TABLE=companies_extra` / `SEND_QUEUE_TABLE=send_queue_extra` を指定し、追加で `FORM_SENDER_TABLE_MODE=extra` 等のラベルを渡してログ識別を容易にする。
     - `JOB_EXECUTION_META` は GAS で計算した `run_index_base` / `workers_per_workflow` / `shards` を含み、`CLOUD_RUN_TASK_INDEX` と組み合わせることでタスク固有の run_index/shard を導出する。
  5. Jobs API のレスポンスから execution ID / create time を取得し、Supabase `job_executions` に記録した後、Cloud Tasks へ 2xx で応答。
- Supabase 接続: dispatcher は環境変数 `DISPATCHER_SUPABASE_URL` / `DISPATCHER_SUPABASE_SERVICE_ROLE_KEY`（Secret Manager で管理）を用いて `job_executions` テーブルと監視用 RPC にアクセスする。鍵は Cloud Run サービスアカウントにのみ開示し、ログ出力時は LogSanitizer でマスクする。
- 認証: Service は Cloud Tasks 用サービスアカウントのみ許可。Jobs API 呼び出しには `roles/run.jobsRunner` を付与。
- 備考: Cloud Run の環境変数は 1 変数あたり 32KB 制限のため、client_config 全体は環境変数で渡さず GCS 経由で提供する。

### 6.4 Cloud Tasks 設定
- ターゲット: dispatcher Service（OIDC 署名付き）。
- 認証: GAS からタスク作成時はサービスアカウント (`gas-form-sender@...`) を使用し、Cloud Tasks の `oidcToken.serviceAccountEmail` に dispatcher 用サービスアカウントを指定する方式を採用（GAS 側で signJwt を扱わない）。必要な権限は GAS サービスアカウントの Cloud Tasks Enqueuer と、dispatcher サービスアカウントに対する `iam.serviceAccounts.actAs`。
- リトライ: `maxAttempts=3`, `minBackoff=60s`, `maxBackoff=600s` を初期値とし、Supabase 負荷に応じ調整。
- レイテンシ: タスク追加から dispatcher 呼び出しまで数秒以内。Jobs API は非同期実行であり、Job 完了は別途モニタリング。
- 重複防止: `taskName` に targeting_id + run_index_base + 実行日を含め、同一 targeting での多重投入を防止する。`ALREADY_EXISTS` や Cloud Tasks リトライ時は Supabase `job_executions` を参照し、同一 targeting/date/run_index_base の実行が存在する場合は再投入をスキップする idempotency チェックを dispatcher 側で行う。
- 営業時間制御: Cloud Tasks の `scheduleTime` を実行開始時刻に設定し、`retryConfig.maxRetryDuration` を「19:00 JST までの残時間」に調整することで営業時間内のみ再試行させる。19:00 を過ぎたタスクは dispatcher が Supabase にステータスを残して翌営業日のタスクとして再投入する。`retryConfig.maxRetryDuration` と `client_config_ref` の有効期限（15 時間）を比較し、Cloud Tasks TTL が残っている間は dispatcher が必要に応じて再署名する設計とする。

### 6.5 GAS 側改修
- `gas/form-sender/GitHubClient.gs` を `CloudRunDispatcherClient.gs` に置換し、repository_dispatch 呼び出しを Cloud Tasks 経由の単一タスク投入へリプレース。
  - `invokeFormSenderJob()` を新設し、**1 targeting あたり Cloud Tasks 1 件のみ**登録する。従来の `concurrent_workflow` ループはタスク生成前に `run_total = concurrent_workflow` として集約する。
- `sendRepositoryDispatch()` に実装されていた 2 シート構造の必須項目チェック・use_extra_table 正規化・テーブル選択・並列設定生成は `invokeFormSenderJob()` へ移し、最低限のフォーマット検証（`targeting_id`, `client_id`, `concurrent_workflow`, `use_extra_table` などの存在・型確認）のみ GAS 側で実行する。`save_client_config.transform_client_config()` に含まれる詳細バリデーションは GAS へ移植せず、Python 側に共通化した `client_config_validator` ライブラリ（`src/form_sender/config_validation/validator.py` 仮称）として切り出して dispatcher / Cloud Run Job / GitHub Actions のいずれからも同一ロジックを呼び出す。GAS は Cloud Tasks enqueue 前に dispatcher の `/validate-config` エンドポイント（同ライブラリを同期呼び出し）を叩き、結果が `OK` の時のみタスクをキューイングすることで二重管理を避ける。GitHub Actions 用のイベントファイル依存部は save_client_config ラッパーへ切り出す。バリデーションエラー時は Cloud Tasks を作成せず GAS 側でログを出力する。
  - `save_client_config.py` CLI 化の際に `--output-path` を必須引数とし、Cloud Run 経路では dispatcher が `/tmp/client_config.json` を指定、GitHub Actions 経路では暫定的に `${RUNNER_TEMP}/client_config.json` を指すようにする。これに連動して GAS 側も payload に出力パスを含め、`FORM_SENDER_CLIENT_CONFIG_PATH` と整合するよう手順書を更新する。
  - GAS → Cloud Tasks への切り替え制御は feature flag（例: `USE_SERVERLESS_FORM_SENDER`）で行い、flag ON の場合は Cloud Tasks 経路のみを呼び出す。OFF の間は GitHub Actions dispatch を維持し、二重起動を避ける。具体的には `processTargeting()` で `if (cfg.useServerless && getFlag('USE_SERVERLESS_FORM_SENDER')) { invokeFormSenderJob(...) } else { sendRepositoryDispatch(...) }` に分岐し、旧ルートでは現行どおり `createQueueForTargeting(targetingId, dateJst, sql, ngCompanies, 10000, /* shards */ 8, extraOpts)` を維持する。新ルートでは `const shardCount = resolveShardCount_()` を導入し、`createQueueForTargeting` 呼び出し時の第 6 引数に `shardCount` を渡した上で payload.execution.shards に同値を書き込む。Feature Flag を OFF にした状態でも shardCount resolver が 8 を返すようにし、移行期間中にコード差分のみ先行リリースできるようにする。リトライ／失敗時ハンドリングは各経路で明確に分岐させる。
  - `run_index_base` カウンタを targeting_id 単位, 日次リセットで保持（`PropertiesService` or Supabase 管理テーブル）。タスク作成時に原子的に `run_index_base` を読み書きし（`LockService` または Supabase `UPDATE ... RETURNING` を利用）、payload.execution に含めた後 `run_index_base += run_total` で次回の開始値を更新（run_index は Job 側で `base + index + 1` として 1 オリジン化）。
  - `resolveParallelism(concurrentWorkflow)` を GAS に追加し、既定では `concurrentWorkflow`（=run_total）をそのまま返す。`ScriptProperties.FORM_SENDER_PARALLELISM_OVERRIDE` が 1〜concurrentWorkflow の値で設定されている場合のみその値に置き換え、Cloud Tasks payload の `execution.parallelism` と Jobs API `parallelism` に反映する。Override は緊急時に GAS から即時切り替えできるよう日次トリガーの起動前に `PropertiesService` を更新する運用とし、実行ログには現在値と override 有無を出力する。
  - `execution.workers_per_workflow` は `CONFIG.WORKERS_PER_WORKFLOW` をベースにしつつ、`PropertiesService.FORM_SENDER_WORKERS_OVERRIDE` が存在する場合のみその値に差し替える。GAS は payload へ書き込んだ値を `JOB_EXECUTION_META.workers_per_workflow` にもコピーし、Cloud Run エントリーポイントが `FORM_SENDER_WORKERS_FROM_META` として環境変数に展開する。これによりランナー側の `resolve_worker_count()` が GAS の管理値を上限として参照できる。
  - `execution.shards` は `ScriptProperties.getProperty('FORM_SENDER_SHARD_COUNT')` から取得し、未設定時は GAS 定数 `CONFIG.FORM_SENDER_SHARD_COUNT`（既定 8）を初期値として ScriptProperties へ同期する。同じ値を `JOB_EXECUTION_META.shards` に埋め込み、`createQueueForTargeting` 呼び出し時の第6引数（現行 8 に固定）もこの値へ置き換えて Supabase `p_shards` と Cloud Run 側の環境変数を一致させる。将来的に Supabase 管理テーブルへ委譲する Feature Flag も検討する。
  - `buildSendQueueForTargetingChunked_` / `buildSendQueueForTargetingChunkedExtra_` などチャンク投入パスでも `resolveShardCount_()` を呼び出し、チャンクごとの `p_shards`・`shard_count` 引数に同じ値を使用する。現行の `const shards = 8` を廃止し、Feature Flag OFF 時は resolver が 8 を返すことで旧挙動を維持する。chunked フローを通る場合でも Cloud Run Job 側の `FORM_SENDER_TOTAL_SHARDS` と一致していることをログ出力で検証する。
- サービスアカウント認証は **サービスアカウント委任 (ServiceAccountCredentials)** に統一。
- タスク payload は既存 repository_dispatch payload をベースにしつつ、`execution` ブロック（run_total / parallelism / run_index_base / shards / workers_per_workflow）と `metadata` ブロックを追加する。`shards` の値は `CONFIG.FORM_SENDER_SHARD_COUNT` または ScriptProperties `FORM_SENDER_SHARD_COUNT` から取得し、Dispatcher が `JOB_EXECUTION_META.shards`・`FORM_SENDER_TOTAL_SHARDS` に同じ値を埋める。必要に応じて Supabase 側で Feature Flag 化する。workflow_dispatch 相当の手動実行では GAS から `client_config` を直接 JSON で渡し、`test_mode`, `use_extra_table`, `client_config` の取り扱いを以下のとおりに揃える:
  - `client_config`: GAS 側で新設する `StorageClient.gs` を用いて Cloud Storage JSON API (`uploadType=multipart`) にアップロードし、`gs://fs-runner-client-config/{date}/{execution_id}.json` を `client_config_object` として保持した上で、15 時間（54,000 秒）有効な V4 署名付き HTTPS URL を `client_config_ref` として生成し payload に格納する。StorageClient は ScriptProperties に格納したサービスアカウント鍵（`gas-form-sender-storage@<project-id>.iam.gserviceaccount.com`）から JWT を生成し、`https://oauth2.googleapis.com/token` でアクセストークンを取得して `POST https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o` を呼び出す。アップロード後は同じ鍵で `Utilities.computeRsaSha256Signature` を用いて署名し、署名・アップロードともに最大 3 回リトライする。15 時間を超えるリトライが発生した場合に備え、dispatcher が `client_config_object` から再署名できるよう情報を保持する。失敗時は Cloud Tasks を作成せず GAS 側でアラートを出す。
    - `StorageClient` ではバケット操作専用サービスアカウントに `roles/storage.objectCreator` と `roles/storage.objectViewer` を付与し、サービスアカウント鍵は Cloud KMS のラップ鍵（例: `projects/<project>/locations/asia-northeast1/keyRings/form-sender/cryptoKeys/gas-storage`）で暗号化したバイナリとして ScriptProperties に保持する。実行時は GAS サービスアカウントが `CloudKMS CryptoKey Decrypter` 権限で `https://cloudkms.googleapis.com/v1/...:decrypt` を呼び出し、得られた平文鍵を即時 `Utilities.base64Decode` して使用後にはメモリから削除する。月次ローテーション手順（新しい KMS バージョンで再暗号化 → ScriptProperties 更新 → 旧バージョン無効化）を Runbook に追加する。ローカル開発用の鍵は KMS で暗号化したファイルを `gas/credentials/` に配置し、`.claspignore` で除外する。
    - dispatcher 側では `client_config_ref` のホスト名・署名期限・`X-Goog-SignedHeaders` をチェックし、再署名した URL を Job に渡す際も `FORM_SENDER_CLIENT_CONFIG_URL` に書き換える。再署名後の URL は Cloud Tasks retry が続く限り再利用し、最終リトライでも失効した場合は 422 を返して GAS に再アップロードを促す。
    - `test_mode`: payload のトップレベルキーとして boolean で渡し、Job 側で `--test-mode` オプションに変換する。
    - `tables.use_extra_table`: true の場合は `COMPANY_TABLE=companies_extra`, `SEND_QUEUE_TABLE=send_queue_extra` を環境変数に設定。
- `sendWorkflowDispatchToBranch`, `testFormSenderOnBranch`, `testFormSenderManual` など既存の GAS 手動/ブランチテスト関数は Cloud Tasks 経由で dispatcher を呼び出す実装へ更新する（payload に branch/test 用パラメータを追加する）。GitHub Actions 依存 API 呼び出しは廃止する。
- `run_index_base` 更新時は `LockService` による短期ロック（最大30秒）または Supabase RPC `allocate_run_index_base(targeting_id, delta)` を呼び出し、原子的に値を取得して競合を防ぐ。
- 緊急停止: `stopAllRunningFormSenderTasks()` は Supabase `job_executions` から `status='running'` の execution を列挙し、dispatcher の停止エンドポイントを通じて Cloud Run Jobs API `projects.locations.executions.cancel` を呼び出す。キャンセル成功時は `job_executions.status` を `cancelled` に更新する。既存の GitHub Actions 列挙関数（`getCancelableWorkflowRuns` 等）は削除する。
- PropertiesService に保存している一時的な client_config（ブランチテスト用）を廃止し、Cloud Tasks エンキュー時にのみ payload/GCS に保持する。既存の保存ロジックはクリア処理を追加して移行完了後に削除する。
- 部分停止: `stopSpecificFormSenderTask(targetingId)` 相当の関数を新設し、指定 targeting の `job_executions` を Supabase から取得 → dispatcher 経由で実行中 execution のみキャンセルする流れに書き換える。Cloud Run Jobs API の executionId と targeting の紐付けは `job_executions` テーブルを参照する。
- GAS の test.gs にある GitHub Actions 依存のテスト・停止ユーティリティは Cloud Tasks / dispatcher / Supabase を利用する新 API に置き換え、不要になった GitHub API 呼び出しと ScriptProperties 保存を削除する。

### 6.6 設定・Secrets 管理
- Secret Manager
  - `form_sender/supabase_url`, `form_sender/supabase_service_role_key` を登録。
  - Cloud Run Job/Service に Secret バージョンを環境変数としてマウントし、名称は `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` に合わせる。テストモード向けには `form_sender/supabase_url_test`, `form_sender/supabase_service_role_key_test` を登録し、環境変数 `SUPABASE_URL_TEST` / `SUPABASE_SERVICE_ROLE_KEY_TEST` として公開する。`FORM_SENDER_TEST_MODE=true` または `FORM_SENDER_TABLE_MODE=test` の際はこれらテスト用シークレットを使用する。
  - dispatcher 用に `form_sender_dispatcher/supabase_url`, `form_sender_dispatcher/supabase_service_role_key` を追加し、環境変数 `DISPATCHER_SUPABASE_URL` / `DISPATCHER_SUPABASE_SERVICE_ROLE_KEY` として注入する。dispatcher サービスアカウントには当該シークレットの Accessor 権限と Supabase 側の job_executions 用ロールのみを付与する。
- Cloud Run Job 既定環境変数（静的設定）
  - `TZ=Asia/Tokyo`
  - `FORM_SENDER_MAX_WORKERS=4`（必要に応じ dispatcher で override）
  - `FORM_SENDER_TOTAL_SHARDS=8`（GAS `CONFIG.FORM_SENDER_SHARD_COUNT` と同じ初期値。dispatcher から override 可能）
  - `PYTHONUNBUFFERED=1`
  - `FORM_SENDER_ENV=cloud_run`
  - `FORM_SENDER_LOG_SANITIZE=1`
- dispatcher が RunJobRequest 発行時に `overrides.containerOverrides[].env` へ設定する per-run 値
  - `FORM_SENDER_CLIENT_CONFIG_URL`
  - `FORM_SENDER_CLIENT_CONFIG_PATH=/tmp/client_config.json`
  - `FORM_SENDER_GIT_REF`（ブランチテスト時のみ）
  - `FORM_SENDER_GIT_TOKEN`（Secret Manager 連携の PAT／デプロイキー、ブランチテスト時のみ）
  - `FORM_SENDER_WORKFLOW_TRIGGER`（manual / automated / branch_test）
  - `JOB_EXECUTION_ID`
  - `COMPANY_TABLE` / `SEND_QUEUE_TABLE`
  - `FORM_SENDER_TABLE_MODE`（default / extra / test）
  - `FORM_SENDER_TARGETING_ID`
- PAT は Job 実行時のみ環境変数として展開し、clone 完了後はエントリーポイントで必ず `unset FORM_SENDER_GIT_TOKEN` する。なお、PAT をランタイムに渡さずにブランチテストを実行する代替案（Cloud Build でブランチごとの一時イメージをビルド、または署名付きアーカイブ配布）を Phase 1 で評価する。

### 6.7 CI/CD パイプライン
- Cloud Build トリガー (main ブランチ push) で以下を実行:
  1. `docker build` → Artifact Registry へ push。
  2. `gcloud run jobs update` / `gcloud run services deploy dispatcher` をステージングに適用。
  3. Terraform の plan/apply を自動化（手動承認ステップ付き）。
- ローカル検証向け `make` タスクを整備（`make build-runner`, `make run-dispatcher`）。
- `.github/workflows/form-sender.yml` には Phase 0 の段階で `FORM_SENDER_ENV=github_actions` と `FORM_SENDER_LOG_SANITIZE=1` をジョブレベルの環境変数として追加し、Cloud Run 移行後も GitHub Actions 実行が期待どおりのマスク設定で動くことを担保する。互換期間中は既存の `GITHUB_ACTIONS=true` も維持し、Phase 1 で段階的に削除するタスクを backlog に入れる。

### 6.8 モニタリング・ログ
- Job 実行ログは Cloud Logging で `form_sender.lifecycle` の INFO のみ許可し、Playwright DEBUG は抑制。
- Cloud Monitoring に以下の指標を作成:
  - Job 成功率（Execution 成功/失敗数）
  - Supabase RPC エラー件数
  - Cloud Tasks キュー長
- エラーは Error Reporting で集約し、SRE チャネルへ通知。
- 緊急停止時は Supabase `job_executions` テーブルから最新の executionId を取得し、dispatcher の停止エンドポイントを経由して Jobs API `projects.locations.executions.cancel` を呼び出す。キャンセル結果は `job_executions` に反映する。
- 実行メタデータは Supabase 内の `job_executions` テーブル（新設）に保存し、`targeting_id` / `run_index_base` / `execution_id` / `task_count` / `status` / `started_at` / `ended_at` を追跡する。dispatcher が Jobs API 応答を受け取った時点で INSERT し、Job 完了時に Runner が PATCH（成功/失敗・終了時刻）する。緊急停止・再実行判定・ダッシュボードはこのテーブルを参照する。DDL は `scripts/table_schema/job_executions.sql`（新規）で管理し、マイグレーション時に Supabase 側へ適用する。
- `send_queue_test`, `submissions_test` などテスト用テーブルの DDL を `scripts/table_schema/send_queue_test.sql`, `scripts/table_schema/submissions_test.sql` として整備し、`--test-mode` 運用時に Supabase へ追加適用する。

### 6.9 テスト戦略
1. **ユニットテスト**: `save_client_config` のインターフェース、dispatcher API の JSON バリデーション。
2. **コンテナ内統合テスト**: `docker run` で `tests/test_form_sender_local.py` および `tests/data/test_client_data.py` を用いたエンドツーエンド検証（Supabase ステージング利用）。
3. **ステージング接続試験**: dispatcher → Job → Supabase までの流れを targeting 1 で実行。
4. **負荷試験**: Cloud Tasks に複数 targeting を投入し、Job `taskCount` + `parallelism` の設定を検証。

### 6.10 リリース手順とロールバック
1. ステージング環境で targeting 1 を新基盤に切り替え、24 時間観測。
2. GAS に feature flag `USE_SERVERLESS_FORM_SENDER` を導入し、targeting ごとに逐次 ON。
3. 問題発生時は flag を OFF にして GitHub Actions に即時切り戻し。
4. 安定運用後、GitHub Actions ワークフローと `sendRepositoryDispatch` 呼び出しを削除。

### 6.11 GitHub Actions 併用期間の運用整理
- **フラグ状態**: `USE_SERVERLESS_FORM_SENDER=OFF` の間は現行どおり GitHub Actions の repository_dispatch を既定ルートとし、Cloud Tasks はステージング検証用のみに限定する。`USE_SERVERLESS_FORM_SENDER=ON` に切り替えた targeting から順次 Cloud Tasks → Cloud Run Job 経路へ送出する。切り戻しは flag を OFF に戻すだけで完了するように実装する。
- **TEST_MODE/手動実行互換**: `testFormSenderManual`, `testFormSenderOnBranch`, `sendWorkflowDispatchToBranch` などの手動トリガーは flag 判定で分岐し、OFF 時は repository_dispatch を、ON 時は dispatcher の手動用エンドポイント (`/invoke-manual`) を呼び出して Cloud Run Job を起動する。手動実行 payload には `workflow_trigger='workflow_dispatch'` と `test_mode=true` を保持し、Cloud Run Job 側で `FORM_SENDER_TEST_MODE` とテーブル切替を行う。
- **GitHub Actions 並走期間**: Phase 1〜3 では GitHub Actions ワークフローを削除せず、手動実行・緊急時の fallback 手順を Runbook に記載する。Cloud Run 切替対象 targeting を増やす前に、GitHub Actions 経路での `workflow_dispatch` / `USE_TEST_MODE` を 1 回以上実行し、dispatcher 側でも同等の手順で `TEST_MODE` が動作することを運用チームに展開する。

---

## 7. フェーズ別マイルストーン
| フェーズ | 期間目安 | 主なアウトプット |
|----------|----------|--------------------|
| Phase 0: 詳細設計・PoC | 2025-10-01〜2025-10-10 | 本計画書改訂、GAS 認証方式/Cloud Tasks 設計確定、Jobs API + shard 算出 PoC |
| Phase 1: コンテナ化 & Runner 改修 | 2025-10-07〜2025-10-24 | Dockerfile、Job エントリーポイント、`save_client_config` 改修、ユニットテスト |
| Phase 2: dispatcher / インフラ構築 | 2025-10-21〜2025-11-08 | Cloud Run Service・Job・Cloud Tasks・Secret Manager・Terraform テンプレート |
| Phase 3: ステージング試験 | 2025-11-04〜2025-11-18 | targeting 1〜N でのエンドツーエンド検証、モニタリング調整 |
| Phase 4: 本番切替 | 2025-11-18〜2025-11-29 | Feature flag 切替、本番リリース、GitHub Actions 撤去 |

---

## 8. 想定リスクと対策
- **Jobs API 呼び出し失敗**: dispatcher で指数バックオフとアラートを実装、Cloud Tasks のリトライに委譲。
- **Playwright の OS 依存**: Job エントリーポイントで `--disable-gpu --no-sandbox` 等を強制し、CI と同条件で検証。
- **Supabase RPC 負荷**: Cloud Tasks の rate (例えば 5 rps) を設定し、`parallelism` を段階的に増加。
- **Secrets 漏洩**: Secret Manager 以外に保存禁止。定期ローテーション手順を Runbook 化。
- **dispatcher 遅延**: Dispatcher はシンプルな処理に限定し、Job 実行時間と切り分け。

---

## 9. 認証・セキュリティ整理
- GAS → Cloud Tasks: `UrlFetchApp.fetch` と `ServiceAccountCredentials` を用いた OAuth2 認証。キュー作成権限のみ付与。
- Cloud Tasks → dispatcher: OIDC トークン (aud = dispatcher URL)。dispatcher は受信時に検証。
- dispatcher → Jobs API: `roles/run.jobsRunner` を持つサービスアカウントで呼び出し。
- Job → Supabase: Secret Manager から注入した Service Role Key を使用。ログにはマスクした値のみ出力。

---

## 10. オープン課題
- **GAS run_index_base 管理方式の決定**（Phase 0, owners: GAS チーム）
  - `PropertiesService` での日次リセット vs Supabase メタテーブル保持のどちらを採用するかを決定し、`invokeFormSenderJob()` 実装に反映する。
  - 同値更新時の排他制御は `LockService`（単一 GAS 実行内）か Supabase の `UPDATE ... RETURNING` を利用した原子的カウンタ更新で実装方針を決める。
- **Cloud Tasks payload フォーマットの確定実装**（Phase 1, owners: dispatcher/GAS）
  - `execution` / `metadata` ブロックの JSON スキーマをコードに落とし込み、互換性テストを行う。
  - `invokeFormSenderJob()` で save_client_config CLI を呼び出す統合テスト（不正 2 シート構造／use_extra_table 正規化／shards 設定）を追加する。
- **taskName 重複防止ポリシーの設計**（Phase 1, owners: GAS）
  - targeting ごとのロック戦略（taskName や Supabase フラグ）を決め、cw² 起動を防ぐ。
- **form_sender_runner の `--run-index` オプション導入**（Phase 1, owners: Python チーム）
  - Cloud Run Job で設定する環境変数との整合性テストを完了させる。
- **form_sender_runner の `--test-mode` サポート**（Phase 1, owners: Python チーム）
  - フラグ ON 時にテスト用テーブルへ書き込み、Supabase 接続先や環境変数を検証用途へ切り替える実装を追加する。
- **テスト用 RPC/テーブルの Supabase 対応**（Phase 1, owners: DBA/Supabase）
  - `claim_next_batch_test`, `mark_done_test`, `requeue_stale_assigned_test` など必要な RPC を Supabase に実装し、Runner の `FORM_SENDER_TABLE_MODE=test` に連動させる。
- **FORM_SENDER_TOTAL_SHARDS 反映ロジック**（Phase 1, owners: Python チーム）
  - ランナーのシャード数取得を `FORM_SENDER_TOTAL_SHARDS` → `JOB_EXECUTION_META.shards` → GAS 由来の `CONFIG.FORM_SENDER_SHARD_COUNT`（既定 8）の優先順に解決し、いずれの値も Supabase 側に投入された `p_shards` と一致していることを検証するユニットテストを追加する。
- **test 用テーブル DDL 整備と適用**（Phase 1, owners: DBA/SRE）
  - `send_queue_test`, `submissions_test` 等の DDL を `scripts/table_schema` に追加し、Supabase マイグレーション手順を確立する。
- **client_config 配送経路の確定と GCS ライフサイクル**（Phase 1, owners: dispatcher/SRE）
  - 署名付き URL の有効期限・バケットの暗号化・30日以内削除ルールを定義し、Job 完了後のクリーンアップ処理を実装する。
- **ブランチテストのコード取得方式**（Phase 1, owners: Python チーム）
  - `FORM_SENDER_GIT_REF` を受けた際の clone 手順（トークン管理・キャッシュ）と CI との差異を整理する。
- **ブランチ依存差分の適用戦略**（Phase 1, owners: Python チーム）
  - `/tmp/workspace` での `pip install -r requirements.txt` 実行手順・キャッシュ方法・タイムアウト閾値を確定し、必要に応じ Cloud Build による一時イメージビルドを検討する。
- **ブランチテスト用コード配布方式の検証**（Phase 1, owners: Python/Platform）
  - PAT を展開せずにブランチ差分を適用する代替案（Artifact Registry のブランチ別イメージ、署名付きアーカイブ配布など）を比較検証し、採用方針を決定する。
- **executionId 永続化と停止 API の設計**（Phase 1, owners: dispatcher/Supabase）
  - Supabase `job_executions` テーブルのスキーマと API を実装し、`stopAllRunningFormSenderTasks()` が executionId を取得・キャンセルできるようにする。
- **job_executions テーブル DDL 整備**（Phase 0, owners: DBA/SRE）
  - `scripts/table_schema/job_executions.sql` を追加し、`targeting_id` / `execution_id` / `run_index_base` / `status` / `started_at` / `ended_at` 等の定義と権限付与を記述する。マイグレーション手順を docs に反映する。
- **GCP クライアントライブラリの導入**（Phase 1, owners: Python/Platform）
  - `google-cloud-tasks`, `google-cloud-run`, `google-cloud-storage`, `google-auth` など新規依存を `requirements.txt` に追加し、互換性を検証する。
- **scriptProperties shard count 運用**（Phase 1, owners: GAS）
  - `FORM_SENDER_SHARD_COUNT` の初期同期処理と Supabase からの切り替え手順を実装し、設定変更時の手順書を更新する。
- **GITHUB_ACTIONS 依存ロジックの環境変数統一**（Phase 1, owners: Python チーム）
  - `FORM_SENDER_ENV` / `FORM_SENDER_LOG_SANITIZE` を `browser.manager`, `log_auditor`, `config_validator`, `validation_config`, `form_finder` 系、`form_analyzer` 系、`form_sender_runner` など全モジュールに適用し、Cloud Run でも従来と同じマスク・タイムアウト・監査が機能することを確認する。
- **save_client_config CLI 化**（Phase 1, owners: Python チーム）
  - `save_client_config.py` を `--input-json` / `--output-path` 引数で再利用できるよう改修し、GitHub Actions 用ラッパーから GITHUB_EVENT_PATH 依存部を分離する。CLI の入出力テストを追加し、dispatcher からの再利用を保証する。

---

## 付録
### A. 主要ソースコード参照
- GAS 時間トリガー/再スケジュール: `gas/form-sender/Code.gs:99-210`
- targeting 処理・GHA 呼び出し: `gas/form-sender/Code.gs:489-680`
- Extra テーブル対応ロジック: `gas/form-sender/Code.gs:1180-1340`
- 旧 Repository Dispatch 実装: `gas/form-sender/GitHubClient.gs:1-210`
- GitHub Actions ワークフロー: `.github/workflows/form-sender.yml:1-200`
- Client Config 保存ロジック: `src/save_client_config.py:70-360`
- Runner 本体（シャーディング/マルチプロセス）: `src/form_sender_runner.py:1-1611`

### B. Cloud Run Job で上書きする主なパラメータ
| 項目 | 設定例 | 説明 |
|------|--------|------|
| `taskCount` | targeting の `concurrent_workflow` 値 | Job 全体のタスク数（= GAS が起動した run_total）|
| `parallelism` | 1〜run_total | 同時に実行するタスク数。Supabase 負荷に応じ dispatcher で制御 |
| `args` | `bin/form_sender_job_entry.py` | タスク固有情報はエントリーポイント側で算出 |
| `env` | `FORM_SENDER_CLIENT_CONFIG_URL`, `FORM_SENDER_TOTAL_SHARDS`, `FORM_SENDER_MAX_WORKERS`, `FORM_SENDER_WORKFLOW_TRIGGER`, `FORM_SENDER_GIT_REF`, `COMPANY_TABLE` 等 | 機微情報とシャード条件を環境変数で渡し、エントリーポイントで復元・加工（`FORM_SENDER_RUN_INDEX` / `FORM_SENDER_SHARD_ID` はエントリーポイントで設定） |
| Task 固有情報 | `CLOUD_RUN_TASK_INDEX`, `CLOUD_RUN_TASK_COUNT` (システム付与) | エントリーポイントで `run_index` / `shard_id` を導出 |

---

以上。
