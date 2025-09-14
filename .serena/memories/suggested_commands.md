# よく使うコマンド（macOS / Darwin）

## セットアップ
- Python仮想環境: `python3 -m venv .venv && source .venv/bin/activate`
- 依存インストール: `pip install -r requirements.txt`
- Playwrightブラウザ: `python -m playwright install`

## ローカル実行（GUIで観察）
- 送信ワーカー（GUI）: `python src/form_sender_runner.py --targeting-id 1 --config-file tests/tmp/client_config_test.json --headless false`
- ローカル送信テスト一発: `python tests/test_form_sender_local.py`
- 企業詳細取得ワーカー: `python src/fetch_detail_worker.py`
- フォーム探索ワーカー: `python src/form_finder_worker.py`
- 解析ワーカー: `python src/form_analyzer_worker.py`

## テスト
- 全テスト: `pytest -q`
- 特定テスト: `pytest tests/test_field_mapping_analyzer.py -q`

## 品質（フォーマット/リント）
- 自動整形: `black .`
- Lint: `flake8 src tests`（または `ruff check src tests`）

## 補助
- 環境変数ロード: `. .venv/bin/activate && export $(grep -v '^#' .env | xargs)`（開発では `python-dotenv` でも可）
- タイムゾーン確認: `env | grep TZ`（CIは `TZ=Asia/Tokyo`）

## GitHub Actions（概念）
- Repository Dispatchで `form-sender.yml` などが起動。
- テーブル切替: `COMPANY_TABLE=companies(_extra)`, `SEND_QUEUE_TABLE=send_queue(_extra)`
