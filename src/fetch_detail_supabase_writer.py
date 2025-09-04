#!/usr/bin/env python3
"""
Supabase Writer for GitHub Actions
GitHub ActionsワークフローからSupabaseに処理結果を直接書き込むためのユーティリティ
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from supabase import create_client, Client

# 直接supabaseクライアントを使用するため、追加のインポートは不要

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SupabaseWriter:
    """Fetch Detail用Supabase書き込み管理クラス"""
    
    def __init__(self, supabase_url: str, supabase_key: str):
        """
        Supabaseクライアント初期化
        
        Args:
            supabase_url: SupabaseプロジェクトURL
            supabase_key: Supabaseサービスロールキー
        """
        try:
            self.supabase = create_client(supabase_url, supabase_key)
            logger.info("Supabaseクライアント初期化完了")
        except Exception as e:
            logger.error(f"Supabaseクライアント初期化エラー: {e}")
            raise
    
    def save_batch_results(self, batch_id: str, results_data: List[Dict[str, Any]], status: str) -> bool:
        """
        Fetch Detail処理結果をSupabaseに保存
        
        Args:
            batch_id: バッチID
            results_data: 処理結果データ
            status: 処理ステータス ('success', 'failure')
            
        Returns:
            保存成功可否
        """
        try:
            # 成功したレコードのみを抽出してバッチ処理
            successful_results = [r for r in results_data if r.get('status') == 'success' and r.get('record_id')]
            
            # 失敗したレコードも処理
            failed_results = [r for r in results_data if r.get('status') == 'failed' and r.get('record_id')]
            
            logger.info(f"バッチ処理開始: 成功={len(successful_results)}件, 失敗={len(failed_results)}件")
            
            success_updated = 0
            failure_updated = 0
            
            # 成功結果の一括更新
            if successful_results:
                success_updated = self._batch_update_success_results(successful_results)
                
            # 失敗結果の一括更新  
            if failed_results:
                failure_updated = self._batch_update_failure_results(failed_results)
            
            total_updated = success_updated + failure_updated
            logger.info(f"バッチ更新完了: 合計{total_updated}件更新 (成功={success_updated}, 失敗={failure_updated})")
            
            return total_updated > 0
            
        except Exception as e:
            logger.error(f"Fetch Detail結果保存エラー: {e}")
            return False
    
    def _batch_update_success_results(self, successful_results: List[Dict[str, Any]]) -> int:
        """成功結果の効率的なバッチ更新（詳細データ対応版）"""
        try:
            # 成功結果データを構築（JSON形式で全詳細データを含む）
            success_data = []
            
            for result in successful_results:
                record_id = result.get('record_id')
                # セキュリティ: record_idの型検証
                if not isinstance(record_id, int) or record_id <= 0:
                    logger.warning(f"不正なrecord_id: {record_id}")
                    continue
                
                # 詳細データを抽出（nullや空文字列でないもののみを含む）
                detail_data = {
                    'record_id': record_id
                }
                
                # 各フィールドをnullチェックして追加
                for field in ['company_url', 'representative', 'capital', 'employee_count', 
                             'postal_code', 'tel', 'established_year', 'established_month', 
                             'closing_month', 'average_age', 'average_salary', 'national_id']:
                    value = result.get(field)
                    if value is not None and value != '' and value != 'None':
                        detail_data[field] = value
                
                success_data.append(detail_data)
            
            if not success_data:
                logger.warning("有効な成功結果データが見つかりません")
                return 0
            
            logger.info(f"成功結果の詳細データ更新: {len(success_data)}件")
            
            # 新しいRPC関数で詳細データを含む一括更新
            result = self.supabase.rpc('bulk_update_fetch_detail_success_with_data', {
                'success_data': success_data
            }).execute()
            
            # 改善されたエラーハンドリング
            if result.data is not None:
                # より堅牢な戻り値処理
                updated_count = result.data if isinstance(result.data, int) else (
                    len(result.data) if isinstance(result.data, list) else 0
                )
                logger.info(f"成功結果詳細データ更新完了: {updated_count}件")
                
                # 詳細データの更新状況をログ出力
                if success_data:
                    sample_data = success_data[0]
                    updated_fields = [k for k in sample_data.keys() if k != 'record_id']
                    logger.info(f"更新フィールド例: {updated_fields}")
                
                return updated_count
            else:
                logger.warning("成功結果詳細データ更新: レスポンスデータが空")
                return 0
                
        except Exception as e:
            logger.error(f"RPC関数による成功結果詳細データ更新失敗: {e}")
            logger.error(f"エラー詳細: 処理対象レコード数={len(successful_results)}")
            return 0
    
    def _batch_update_failure_results(self, failed_results: List[Dict[str, Any]]) -> int:
        """失敗結果の効率的なバッチ更新"""
        try:
            # レコードIDリスト作成（整数のみを許可）
            record_ids = []
            
            for result in failed_results:
                record_id = result.get('record_id')
                # セキュリティ: record_idの型検証
                if not isinstance(record_id, int) or record_id <= 0:
                    logger.warning(f"不正なrecord_id: {record_id}")
                    continue
                record_ids.append(record_id)
            
            if not record_ids:
                logger.warning("有効なrecord_idが見つかりません")
                return 0
            
            # RPC関数で効率的な一括更新
            result = self.supabase.rpc('bulk_update_fetch_detail_failure', {
                'record_ids': record_ids
            }).execute()
            
            # 改善されたエラーハンドリング
            if result.data is not None:
                # より堅牢な戻り値処理
                updated_count = result.data if isinstance(result.data, int) else (
                    len(result.data) if isinstance(result.data, list) else 0
                )
                logger.info(f"失敗結果バッチ更新完了: {updated_count}件")
                return updated_count
            else:
                logger.warning("失敗結果バッチ更新: レスポンスデータが空")
                return 0
                
        except Exception as e:
            logger.error(f"RPC関数による失敗結果バッチ更新失敗: {e}")
            return 0
    
    def early_termination_cleanup(self, batch_id: str, results_data: List[Dict[str, Any]], 
                                 all_batch_record_ids: List[int], status: str) -> bool:
        """
        早期終了時の専用クリーンアップ処理
        
        Args:
            batch_id: バッチID
            results_data: 実際に処理された結果データ
            all_batch_record_ids: バッチ内の全record_idリスト
            status: 処理ステータス
            
        Returns:
            クリーンアップ成功可否
        """
        try:
            logger.info(f"早期終了時のクリーンアップ処理開始: batch_id={batch_id}")
            
            # 処理済みレコードIDを抽出
            processed_record_ids = [r.get('record_id') for r in results_data if r.get('record_id')]
            processed_record_ids = [rid for rid in processed_record_ids if isinstance(rid, int)]
            
            # 成功・失敗結果を分離
            successful_results = [r for r in results_data if r.get('status') == 'success' and r.get('record_id')]
            failed_results = [r for r in results_data if r.get('status') == 'failed' and r.get('record_id')]
            
            logger.info(f"処理済み件数: 成功={len(successful_results)}, 失敗={len(failed_results)}")
            logger.info(f"バッチ総数: {len(all_batch_record_ids)}, 処理済み: {len(processed_record_ids)}")
            
            success_updated = 0
            failure_updated = 0
            reset_count = 0
            
            # 1. 成功結果の保存（詳細データ含む）
            if successful_results:
                logger.info(f"成功結果の詳細データ保存開始: {len(successful_results)}件")
                success_updated = self._batch_update_success_results(successful_results)
            
            # 2. 失敗結果の保存
            if failed_results:
                failure_updated = self._batch_update_failure_results(failed_results)
            
            # 3. 未処理レコードのキューリセット
            reset_count = self._reset_unprocessed_queue(processed_record_ids)
            
            total_operations = success_updated + failure_updated + reset_count
            logger.info(f"早期終了クリーンアップ完了: 成功更新={success_updated}, "
                       f"失敗更新={failure_updated}, 未処理リセット={reset_count}, 総操作数={total_operations}")
            
            return total_operations > 0
            
        except Exception as e:
            logger.error(f"早期終了クリーンアップエラー: {e}")
            return False
    
    def _reset_unprocessed_queue(self, processed_record_ids: List[int]) -> int:
        """未処理レコードのキューをリセット"""
        try:
            # 型検証済みのレコードIDのみを渡す
            validated_ids = [rid for rid in processed_record_ids if isinstance(rid, int) and rid > 0]
            
            logger.info(f"未処理キューリセット開始: 処理済み={len(validated_ids)}件")
            
            # 新しいRPC関数を呼び出し
            result = self.supabase.rpc('reset_unprocessed_fetch_detail_queue', {
                'processed_record_ids': validated_ids
            }).execute()
            
            if result.data:
                # 複数行の結果を想定 
                if isinstance(result.data, list) and len(result.data) > 0:
                    reset_count = result.data[0].get('reset_count', 0)
                    error_message = result.data[0].get('error_message', '')
                    
                    if error_message != 'Success':
                        logger.warning(f"キューリセット警告: {error_message}")
                    else:
                        logger.info(f"未処理キューリセット完了: {reset_count}件")
                        
                    return reset_count
                else:
                    logger.warning("キューリセット: 予期しない応答形式")
                    return 0
            else:
                logger.warning("キューリセット: レスポンスデータが空")
                return 0
                
        except Exception as e:
            logger.error(f"未処理キューリセット失敗: {e}")
            return 0
    


def load_results_file(file_path: str) -> Dict[str, Any]:
    """
    結果ファイルを読み込み（リファクタリング版）
    
    Args:
        file_path: 結果ファイルパス
        
    Returns:
        結果データ
    """
    try:
        if not os.path.exists(file_path):
            logger.error(f"結果ファイルが見つかりません: {file_path}")
            return {}
            
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 基本データ検証
        required_keys = ['total_processed', 'results']
        for key in required_keys:
            if key not in data:
                logger.warning(f"必須キー '{key}' が見つかりません")
                
        logger.info("結果ファイル読み込み完了")
        return data
        
    except Exception as e:
        logger.error(f"結果ファイル読み込みエラー: {e}")
        return {}


def main():
    """メイン処理"""
    parser = argparse.ArgumentParser(description='Supabase Writer for GitHub Actions')
    parser.add_argument('--batch-id', required=True, help='バッチID')
    parser.add_argument('--results-file', required=True, help='処理結果JSONファイルパス')
    parser.add_argument('--status', required=True, choices=['success', 'failure'], help='処理ステータス')
    
    args = parser.parse_args()
    
    # 環境変数から設定取得
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    
    # より詳細な設定確認
    logger.info("=== Supabase Writer 設定確認 ===")
    logger.info(f"ステータス: {args.status}")
    logger.info("環境変数確認中...")
    logger.info("認証情報チェック完了")
    
    if not supabase_url or not supabase_key:
        logger.error("必要な環境変数が設定されていません")
        logger.error("Supabase接続に必要な環境変数を確認してください")
        sys.exit(1)
    
    try:
        # 結果ファイル読み込み
        results_data = load_results_file(args.results_file)
        
        if not results_data:
            logger.warning("結果データが空です")
            sys.exit(1)
        
        # Supabase書き込み実行
        logger.info("SupabaseWriter初期化開始...")
        writer = SupabaseWriter(supabase_url, supabase_key)
        logger.info("SupabaseWriter初期化完了")
        
        # 早期終了判定
        terminated_early = results_data.get('terminated_early', False)
        all_batch_record_ids = results_data.get('all_batch_record_ids', [])
        results_list = results_data.get('results', [])
        
        if terminated_early:
            logger.info("早期終了が検出されました。専用クリーンアップ処理を実行します。")
            success = writer.early_termination_cleanup(
                args.batch_id, 
                results_list, 
                all_batch_record_ids,
                args.status
            )
        else:
            logger.info("通常処理として結果を保存します。")
            success = writer.save_batch_results(args.batch_id, results_list, args.status)
        
        if success:
            logger.info("Supabase書き込み完了")
            sys.exit(0)
        else:
            logger.error("Supabase書き込みに失敗しました")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()