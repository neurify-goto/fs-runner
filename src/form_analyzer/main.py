#!/usr/bin/env python3
"""
Form Analyzer Main Entry Point

GitHub Actions ワークフロー用のメインエントリーポイント。
フォーム解析処理を実行する。
"""

import asyncio
import logging
import os
import sys

from .worker import FormAnalyzerWorker

# ロギング設定（サニタイズ付き）
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
try:
    from form_sender.security.log_sanitizer import setup_sanitized_logging
    setup_sanitized_logging()  # ルートロガーへ適用
except Exception:
    pass
logger = logging.getLogger(__name__)


async def main():
    """GitHub Actions用のメイン処理"""
    try:
        worker = FormAnalyzerWorker()
        await worker.run()
    except Exception as e:
        logger.error(f"Form Analyzer メイン処理エラー: {e}", exc_info=True)
        sys.exit(1)


def run_main():
    """メイン処理の実行"""
    if os.getenv("GITHUB_EVENT_PATH"):
        asyncio.run(main())
    else:
        logger.warning("GITHUB_EVENT_PATHが設定されていないため、ローカル実行をスキップします。")
        logger.warning("ローカルでテスト実行するには、ダミーのイベントファイルを用意してください。")
        sys.exit(0)


if __name__ == "__main__":
    run_main()
