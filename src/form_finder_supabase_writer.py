#!/usr/bin/env python3
"""
Supabase Writer for Form Finder (GitHub Actions)
Form Finder処理結果をSupabaseに書き込むためのユーティリティ
"""

import argparse
import logging
import os
import sys

# form_finderから必要なクラスとユーティリティ関数をインポート
from form_finder.supabase_writer import SupabaseFormFinderWriter, load_results_file

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """メイン処理"""
    parser = argparse.ArgumentParser(description='Supabase Writer for Form Finder (GitHub Actions)')
    parser.add_argument('--batch-id', required=True, help='バッチID')
    parser.add_argument('--results-file', required=True, help='処理結果JSONファイルパス')
    parser.add_argument('--status', required=True, choices=['success', 'failure'], help='処理ステータス')
    
    args = parser.parse_args()
    
    # 環境変数から設定取得
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    company_table = os.getenv('COMPANY_TABLE', 'companies')
    
    # 設定確認
    logger.info("=== Supabase Form Finder Writer 設定確認 ===")
    logger.info(f"ステータス: {args.status}")
    logger.info(f"書き込み先テーブル: {company_table}")
    
    if not supabase_url or not supabase_key:
        logger.error("必要な環境変数が設定されていません")
        sys.exit(1)
    
    try:
        # 結果ファイル読み込み
        results_data = load_results_file(args.results_file)
        
        if not results_data:
            logger.warning("結果データが空です")
            sys.exit(1)
        
        # Supabase書き込み実行
        logger.info("SupabaseFormFinderWriter初期化開始...")
        writer = SupabaseFormFinderWriter(supabase_url, supabase_key, target_table=company_table)
        logger.info("SupabaseFormFinderWriter初期化完了")
        
        # 個別結果の保存
        results_list = results_data.get('results', [])
        success = writer.save_form_finder_results(args.batch_id, results_list, args.status)
        
        if success:
            logger.info("Supabase Form Finder書き込み完了")
            sys.exit(0)
        else:
            logger.error("Supabase Form Finder書き込みに失敗しました")
            sys.exit(1)
            
    except Exception as e:
        # セキュリティ: エラーメッセージをサニタイゼーション（作成済みのwriterインスタンスがあれば使用）
        try:
            sanitized_error = writer._sanitize_error_message(str(e)) if 'writer' in locals() else str(e)
        except:
            sanitized_error = "エラー詳細取得失敗"
        logger.error(f"予期しないエラー: {sanitized_error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
