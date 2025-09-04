#!/usr/bin/env python3
"""
Supabase Form Analyzer Writer Main Entry Point

GitHub Actions ワークフロー用のSupabaseライターメインエントリーポイント。
"""

import argparse
import asyncio
import logging
import sys

from .supabase_writer import SupabaseFormAnalyzerWriter

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """メイン関数"""
    parser = argparse.ArgumentParser(description='フォーム解析結果をSupabaseに書き込み')
    parser.add_argument('--batch-id', required=True, help='バッチID')
    parser.add_argument('--results-file', required=True, help='処理結果ファイル')
    parser.add_argument('--status', required=True, help='処理ステータス')
    
    args = parser.parse_args()
    
    try:
        writer = SupabaseFormAnalyzerWriter()
        
        result = asyncio.run(writer.write_results(
            args.batch_id, 
            args.results_file, 
            args.status
        ))
        
        if result['success']:
            logger.info(f"書き込み処理成功: {result['message']}")
            logger.info(f"更新企業数: {result['companies_updated']}")
            if result.get('batch_created'):
                logger.info("batch_requestレコードも作成されました")
            sys.exit(0)
        else:
            logger.error(f"書き込み処理失敗: {result.get('error', '不明なエラー')}")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"メイン処理エラー: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()