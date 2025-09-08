#!/usr/bin/env python3
"""
Form Finder Worker (GitHub Actions版 - マルチプロセス対応エントリポイント)

GitHub ActionsからGASデータを受け取り、マルチプロセスでフォーム探索処理を実行します。

（参考）旧 form_sender_worker.py のマルチプロセス・アーキテクチャに基づく設計：
- マルチワーカープロセスによる並列処理
- オーケストレーターによる統括管理
- GitHub Actions環境での最適化
- 各企業処理完了時の即座結果保存
"""

import asyncio
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
from datetime import datetime

# マルチプロセス対応コンポーネントをインポート
from form_finder.orchestrator.manager import ConfigurableFormFinderOrchestrator

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# HTTPXのHTTPリクエストログを無効化（URL情報削除のため）
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def get_event_data_from_github_actions() -> tuple:
    """GitHub Actionsイベントデータを取得"""
    try:
        # 環境変数からイベントデータを取得
        event_path = os.environ.get('GITHUB_EVENT_PATH')
        if not event_path:
            raise ValueError("GITHUB_EVENT_PATH環境変数が見つかりません")
        
        with open(event_path, 'r', encoding='utf-8') as f:
            event_data = json.load(f)
            
        logger.info("GitHub Actionsイベントデータ取得成功")
        
        # client_payloadからデータを抽出
        client_payload = event_data.get('client_payload', {})
        batch_id = client_payload.get('batch_id')
        batch_data = client_payload.get('batch_data', [])
        
        # デバッグ: イベントデータの詳細確認（安全なログ）
        logger.info(f"Event data keys: {list(event_data.keys())}")
        logger.info(f"Client payload keys: {list(client_payload.keys()) if client_payload else 'None'}")
        logger.info(f"Batch data type: {type(batch_data)}")
        logger.info(f"Batch data length: {len(batch_data) if batch_data else 0}")
        
        if batch_data and len(batch_data) > 0:
            sample_data = batch_data[0]
            logger.info(f"Sample data keys: {list(sample_data.keys()) if isinstance(sample_data, dict) else 'Not dict'}")
            logger.info(f"Sample data types: {type(sample_data)}")
            if isinstance(sample_data, dict):
                logger.info(f"Sample has record_id: {'record_id' in sample_data}")
                logger.info(f"Sample has company_url: {'company_url' in sample_data}")
                logger.info(f"Sample company_url length: {len(str(sample_data.get('company_url', '')))}")
        
        logger.info(f"バッチID: {batch_id}")
        logger.info(f"処理対象: {len(batch_data)}件")
        
        return batch_id, batch_data
        
    except Exception as e:
        logger.error(f"イベントデータ取得エラー: {e}")
        raise


def save_error_results(batch_id: str, error_message: str):
    """エラー時の最低限結果ファイルを作成"""
    try:
        from pathlib import Path
        
        ARTIFACTS_DIR = Path("artifacts")
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        
        error_results = {
            'batch_id': batch_id if batch_id else 'unknown',
            'processed_at': datetime.utcnow().isoformat(),
            'execution_time': 0,
            'total_processed': 0,
            'total_successful': 0,
            'total_failed': 1,
            'business_successful': 0,
            'business_failed': 0,
            'total_forms_found': 0,
            'form_discovery_rate': 0.0,
            'error': error_message,
            'results': []
        }
        
        error_file = ARTIFACTS_DIR / 'form_finder_results.json'
        with open(error_file, 'w', encoding='utf-8') as f:
            json.dump(error_results, f, ensure_ascii=False, indent=2)
            
        logger.info(f"エラー時結果ファイル保存: {error_file}")
            
    except Exception as save_error:
        logger.error(f"エラー時結果保存失敗: {save_error}")


async def main():
    """メイン処理（マルチプロセス・アーキテクチャ版）"""
    # 一時的にデバッグログを有効化してデータフローを完全追跡
    logging.getLogger('form_finder.orchestrator.manager').setLevel(logging.DEBUG)
    logging.getLogger('form_finder.worker.isolated_worker').setLevel(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)  # ルートロガーもデバッグレベルに
    
    logger.info(f"=== Form Finder Multi-Process Processing Started ===")
    logger.info(f"Processing Mode: Multi-Process with Worker Pool")
    logger.info(f"Environment: {'GitHub Actions' if os.getenv('GITHUB_ACTIONS') == 'true' else 'Local'}")
    logger.info("DEBUG LOGGING ENABLED FOR DATA FLOW ANALYSIS")
    
    # マルチプロセス環境設定（既存設定を確認してから適用）
    if mp.get_start_method(allow_none=True) is None:
        mp.set_start_method('spawn')  # クロスプラットフォーム互換性
        logger.info("Set multiprocessing start method to 'spawn'")
    else:
        current_method = mp.get_start_method()
        logger.info(f"Using existing multiprocessing method: {current_method}")
    
    batch_id = None
    orchestrator = None
    
    try:
        # GitHub Actionsイベントデータ取得
        batch_id, batch_data = get_event_data_from_github_actions()
        
        if not batch_data:
            logger.error("処理対象データが見つかりません")
            save_error_results(batch_id, "処理対象データが見つかりません")
            sys.exit(1)
        
        # オーケストレーターを初期化（設定ファイルからワーカー数を自動取得）
        orchestrator = ConfigurableFormFinderOrchestrator(batch_id, batch_data)
        
        # シグナルハンドラー設定（緊急終了用）
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            orchestrator.should_stop = True
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        
        # ワーカープロセス起動
        logger.info("Starting worker processes...")
        worker_start_success = await orchestrator.start_workers()
        
        if not worker_start_success:
            logger.error("Failed to start all worker processes")
            save_error_results(batch_id, "ワーカープロセスの起動に失敗しました")
            sys.exit(1)
        
        logger.info(f"All {orchestrator.num_workers} workers are ready!")
        
        # ワーカー監視タスクを開始（バックグラウンド）
        monitoring_task = asyncio.create_task(
            orchestrator.monitor_and_recover_workers(check_interval=60)
        )
        logger.info("Worker monitoring task started")
        
        # 企業バッチ処理実行（マルチワーカーで並列処理）
        logger.info("Starting form finder batch processing...")
        batch_stats = await orchestrator.process_companies_batch()
        
        # バッチ処理サマリー
        logger.info(f"Batch processing completed: "
                   f"sent={batch_stats['companies_sent']}, "
                   f"success={batch_stats['success_count']}, "
                   f"failed={batch_stats['failed_count']}, "
                   f"errors={batch_stats['error_count']}")
        
        # 最終サマリー
        final_summary = orchestrator.get_processing_summary()
        logger.info(f"=== Form Finder Multi-Process Processing Completed ===")
        logger.info(f"Total Companies Processed: {final_summary['processed_count']}")
        logger.info(f"Technical Success: {final_summary['success_count']}")
        logger.info(f"Technical Failed: {final_summary['failed_count']}")
        logger.info(f"Business Success (Forms Found): {final_summary['business_successful_count']}")
        logger.info(f"Business Failed (No Forms): {final_summary['business_failed_count']}")
        logger.info(f"Total Forms Found: {final_summary['total_forms_found']}")
        logger.info(f"Form Discovery Rate: {final_summary['form_discovery_rate']}%")
        logger.info(f"Total Execution Time: {final_summary['elapsed_time']:.2f} seconds")
        logger.info(f"Workers Used: {final_summary['num_workers']}")
        
        # 結果保存
        orchestrator.save_results()
        logger.info("Form finder processing results saved successfully")
        
    except Exception as e:
        logger.error(f"Main process error: {e}")
        
        # セキュアなスタックトレース出力（機密情報をサニタイズ）
        try:
            import traceback
            from form_sender.security.log_sanitizer import sanitize_for_log
            stack_trace = traceback.format_exc()
            sanitized_trace = sanitize_for_log(stack_trace)
            logger.error(f"サニタイズ済みスタックトレース: {sanitized_trace}")
        except Exception as trace_error:
            logger.error(f"スタックトレース出力エラー: {trace_error}")
        
        # エラー時結果保存
        save_error_results(batch_id, str(e))
        sys.exit(1)
        
    finally:
        # リソースクリーンアップ
        logger.info("Cleaning up resources...")
        
        # 監視タスクをキャンセル（安全性向上版）
        monitoring_task_local = locals().get('monitoring_task')
        if monitoring_task_local is not None and not monitoring_task_local.done():
            logger.info("Cancelling worker monitoring task...")
            monitoring_task_local.cancel()
            try:
                await monitoring_task_local
            except asyncio.CancelledError:
                logger.info("Worker monitoring task cancelled")
            except Exception as e:
                logger.warning(f"Error during monitoring task cancellation: {e}")
        
        # オーケストレーターのクリーンアップ
        if orchestrator:
            await orchestrator.cleanup()
            
        logger.info("Form finder cleanup completed")


if __name__ == '__main__':
    # メイン処理実行（監視機能はオーケストレーターに統合済み）
    asyncio.run(main())
