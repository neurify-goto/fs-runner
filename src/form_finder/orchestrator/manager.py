"""
Form Finder マルチプロセス・オーケストレーター管理

form_senderのオーケストレーターを参考に、
マルチワーカープロセスでの企業フォーム探索処理を統括する
"""

import asyncio
import json
import logging
import multiprocessing as mp
import signal
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from ..worker.isolated_worker import worker_process_main
from form_sender.communication.queue_manager import (
    QueueManager,
    WorkerResult,
    ResultStatus,
    QueueOverflowError,
    WorkerCommunicationError,
)
from form_sender.security.log_sanitizer import sanitize_for_log
from config.manager import get_worker_config
from utils.env import is_github_actions

logger = logging.getLogger(__name__)

# アーティファクトディレクトリ
ARTIFACTS_DIR = Path("artifacts")
ARTIFACTS_DIR.mkdir(exist_ok=True)


class FormFinderOrchestrator:
    """Form Finderマルチプロセス・オーケストレーター管理クラス"""

    def __init__(self, batch_id: str, batch_data: List[Dict[str, Any]], num_workers: int = 2):
        """
        初期化

        Args:
            batch_id: バッチID
            batch_data: 処理対象の企業データリスト
            num_workers: ワーカープロセス数
        """
        self.batch_id = batch_id
        self.batch_data = batch_data
        self.num_workers = num_workers

        # プロセス間通信管理
        self.queue_manager = QueueManager(num_workers)

        # ワーカープロセス管理
        self.worker_processes = []
        self.worker_status = {}  # worker_id -> status
        self._status_lock = threading.Lock()

        # 処理統計
        self.start_time = time.time()
        self.orchestrator_stats = {
            "start_time": self.start_time,
            "batches_processed": 0,
            "total_companies_sent": 0,
            "total_results_received": 0,
            "active_tasks": 0,
        }

        # 結果収集用
        self.results = []
        self.results_lock = threading.Lock()
        
        # フォーム探索統計（スレッドセーフ）
        self.stats_lock = threading.Lock()
        self.total_processed = 0
        self.total_successful = 0    # 技術的成功（エラーなし）
        self.total_failed = 0        # 技術的失敗（エラーあり）
        self.business_successful = 0 # ビジネス成功（フォーム発見）
        self.business_failed = 0     # ビジネス失敗（フォーム未発見）
        self.total_forms_found = 0   # 総フォーム発見数

        # 制御フラグ
        self.is_running = False
        self.should_stop = False

        logger.info(f"FormFinderOrchestrator initialized: batch_id={batch_id}, "
                   f"companies={len(batch_data)}, workers={num_workers}")

    async def monitor_and_recover_workers(self, check_interval: float = 30) -> None:
        """
        ワーカー監視・復旧バックグラウンドタスク（オーケストレーター統合版）

        Args:
            check_interval: チェック間隔（秒）
        """
        logger.info(f"Starting worker monitoring task (interval: {check_interval}s)")

        while self.is_running and not self.should_stop:
            try:
                # ヘルスチェック
                health_status = self.check_worker_health()
                unhealthy_workers = [
                    w_id for w_id, status in health_status.items() if status in ["dead", "unresponsive"]
                ]

                if unhealthy_workers:
                    logger.warning(f"Unhealthy form finder workers detected: {unhealthy_workers}")
                    # form_finderでは自動復旧は設定で無効化されているためログのみ
                    # 必要に応じて復旧機能を追加可能

                # 保留中タスクの監視
                try:
                    pending_count = self.queue_manager.get_pending_task_count()
                    if pending_count > 25:  # form_finder用の閾値
                        logger.warning(f"High number of pending form finder tasks: {pending_count}")
                except Exception as queue_e:
                    logger.debug(f"Could not get pending task count: {queue_e}")

                await asyncio.sleep(check_interval)

            except Exception as e:
                logger.error(f"Error in form finder worker monitoring task: {e}")
                await asyncio.sleep(check_interval)

        logger.info("Form finder worker monitoring task stopped")

    async def start_workers(self) -> bool:
        """
        ワーカープロセスを起動

        Returns:
            bool: 全ワーカーが正常起動したかどうか
        """
        try:
            logger.info(f"Starting {self.num_workers} form finder worker processes...")

            # ワーカープロセス起動
            for worker_id in range(self.num_workers):
                process = mp.Process(
                    target=worker_process_main,
                    args=(worker_id, self.queue_manager.task_queue, self.queue_manager.result_queue),
                    name=f"form-finder-worker-{worker_id}",
                )
                process.start()
                self.worker_processes.append(process)
                with self._status_lock:
                    self.worker_status[worker_id] = "starting"

                logger.info(f"Form Finder Worker {worker_id} process started (PID: {process.pid})")

            # ワーカーの準備完了を待機
            ready_worker_ids = set()
            timeout_start = time.time()
            max_startup_time = 60  # 最大60秒で起動

            while len(ready_worker_ids) < self.num_workers and (time.time() - timeout_start) < max_startup_time:
                results = self.queue_manager.get_all_available_results()

                for result in results:
                    if result.status == ResultStatus.WORKER_READY and result.worker_id not in ready_worker_ids:
                        ready_worker_ids.add(result.worker_id)
                        with self._status_lock:
                            self.worker_status[result.worker_id] = "ready"
                        logger.info(f"Form Finder Worker {result.worker_id} is ready ({len(ready_worker_ids)}/{self.num_workers})")

                if len(ready_worker_ids) < self.num_workers:
                    await asyncio.sleep(1)

            if len(ready_worker_ids) == self.num_workers:
                logger.info("All form finder workers are ready!")
                self.is_running = True
                return True
            else:
                logger.error(f"Only {len(ready_worker_ids)}/{self.num_workers} workers became ready within timeout")
                return False

        except Exception as e:
            logger.error(f"Error starting form finder workers: {e}")
            return False

    async def process_companies_batch(self) -> Dict[str, Any]:
        """
        企業バッチ処理（マルチワーカー版）

        Returns:
            Dict[str, Any]: 処理結果サマリー
        """
        if not self.is_running:
            raise RuntimeError("Workers are not running")

        batch_start_time = time.time()
        batch_stats = {
            "companies_sent": 0,
            "results_received": 0,
            "success_count": 0,
            "failed_count": 0,
            "error_count": 0,
        }

        try:
            companies = self.batch_data

            # デバッグ: バッチデータの詳細確認（安全なログ）
            logger.info(f"Batch data type: {type(companies)}")
            logger.info(f"Batch data length: {len(companies) if companies else 0}")
            
            if companies and len(companies) > 0:
                sample_company = companies[0]
                logger.info(f"Sample company type: {type(sample_company)}")
                if isinstance(sample_company, dict):
                    logger.info(f"Sample company keys: {list(sample_company.keys())}")
                    logger.info(f"Sample has record_id: {'record_id' in sample_company}")
                    logger.info(f"Sample has company_url: {'company_url' in sample_company}")
                    logger.info(f"Sample record_id value: {sample_company.get('record_id', 'MISSING')}")
                    logger.info(f"Sample company_url length: {len(str(sample_company.get('company_url', '')))}")
                    if not sample_company.get('company_url'):
                        logger.error(f"Sample company_url is empty: {repr(sample_company.get('company_url'))}")
                else:
                    logger.error(f"Sample company is not dict: {sample_company}")

            if not companies:
                logger.info("No companies to process in this batch")
                return batch_stats

            logger.info(f"Processing batch of {len(companies)} companies with {self.num_workers} workers")

            # ワーカーにタスクを分散送信
            task_ids = []
            queue_overflow_count = 0
            communication_error_count = 0

            for company in companies:
                try:
                    # 企業データの検証
                    self.validate_company_data(company)

                    # 企業データを直接渡す（Form Finder用の2フィールド構造をそのまま送信）
                    task_id = self.queue_manager.send_task(company)  # client_data, targeting_idは自動的にNoneが設定される
                    task_ids.append(task_id)
                    batch_stats["companies_sent"] += 1
                    self.orchestrator_stats["total_companies_sent"] += 1

                except QueueOverflowError as e:
                    queue_overflow_count += 1
                    logger.warning(f"Queue overflow for company {company.get('record_id')}: {e}")
                    # 改善されたリトライ処理
                    if queue_overflow_count <= 3:
                        # 指数バックオフでリトライ
                        backoff_time = min(2 ** (queue_overflow_count - 1), 8)  # 1, 2, 4秒
                        await asyncio.sleep(backoff_time)
                        try:
                            self.validate_company_data(company)
                            task_id = self.queue_manager.send_task(company)
                            task_ids.append(task_id)
                            batch_stats["companies_sent"] += 1
                            self.orchestrator_stats["total_companies_sent"] += 1
                            logger.info(f"Retry successful for company {company.get('record_id')} after {queue_overflow_count} attempts")
                        except (QueueOverflowError, WorkerCommunicationError) as retry_comm_e:
                            logger.error(f"Communication retry failed for company {company.get('record_id')}: {retry_comm_e}")
                            continue
                        except Exception as retry_e:
                            logger.error(f"General retry failed for company {company.get('record_id')}: {retry_e}")
                            # バッチ統計に失敗カウントを追加
                            batch_stats["retry_failures"] = batch_stats.get("retry_failures", 0) + 1
                            continue
                    else:
                        logger.error(f"Max queue overflow retries exceeded ({queue_overflow_count}), skipping company {company.get('record_id')}")
                        batch_stats["skipped_overflow"] = batch_stats.get("skipped_overflow", 0) + 1
                        continue

                except WorkerCommunicationError as e:
                    communication_error_count += 1
                    logger.error(f"Worker communication error for company {company.get('record_id')}: {e}")
                    if communication_error_count > len(companies) * 0.3:  # 30%以上でエラー
                        logger.error("Too many worker communication errors, aborting batch")
                        break
                    continue

                except ValueError as e:
                    logger.warning(f"Company data validation failed for {company.get('record_id')}: {e}")
                    batch_stats["validation_errors"] = batch_stats.get("validation_errors", 0) + 1
                    continue

                except Exception as e:
                    logger.error(f"Unexpected error sending task for company {company.get('record_id')}: {e}")
                    continue

            if not task_ids:
                logger.warning("No tasks were successfully sent to workers")
                return batch_stats

            # ワーカーからの結果を収集
            pending_tasks = len(task_ids)
            
            # 設定から動的にタイムアウト時間を取得
            try:
                form_finder_config = get_worker_config().get("form_finder_multi_process", {})
                max_wait_time = form_finder_config.get("batch_processing_timeout", 2400)  # デフォルト40分
                logger.info(f"Batch processing timeout configured to {max_wait_time} seconds ({max_wait_time/60:.1f} minutes)")
            except Exception as e:
                logger.warning(f"Could not load batch timeout from config, using default 40 minutes: {e}")
                max_wait_time = 2400
            
            last_activity = time.time()

            while pending_tasks > 0 and (time.time() - batch_start_time) < max_wait_time:
                results = self.queue_manager.get_all_available_results()

                for result in results:
                    if result.status in [ResultStatus.SUCCESS, ResultStatus.FAILED, ResultStatus.ERROR]:
                        pending_tasks -= 1
                        batch_stats["results_received"] += 1
                        self.orchestrator_stats["total_results_received"] += 1
                        last_activity = time.time()

                        # 結果を収集
                        await self._collect_worker_result(result)

                        # 統計更新
                        if result.status == ResultStatus.SUCCESS:
                            batch_stats["success_count"] += 1
                        elif result.status == ResultStatus.FAILED:
                            batch_stats["failed_count"] += 1
                        else:  # ERROR
                            batch_stats["error_count"] += 1

                        logger.debug(f"Processed result for company {result.record_id}: {result.status.value}")

                # 結果がなかった場合の待機
                if not results:
                    await asyncio.sleep(0.5)

                # 進捗監視とタイムアウトチェック
                current_time = time.time()
                elapsed_wait_time = current_time - last_activity
                total_elapsed = current_time - batch_start_time
                
                # 30秒間隔で詳細な進捗ログを出力
                if elapsed_wait_time > 30:
                    completed_count = len(task_ids) - pending_tasks
                    completion_rate = (completed_count / len(task_ids)) * 100 if task_ids else 0
                    estimated_remaining_time = (total_elapsed / completed_count * pending_tasks) if completed_count > 0 else 0
                    
                    logger.info(f"処理進捗: {completed_count}/{len(task_ids)}件完了 ({completion_rate:.1f}%)")
                    logger.info(f"経過時間: {total_elapsed/60:.1f}分, 残り推定時間: {estimated_remaining_time/60:.1f}分")
                    logger.info(f"未完了タスク: {pending_tasks}件, 最終活動から: {elapsed_wait_time:.1f}秒")
                    
                    # 10分間無活動の場合は警告
                    if elapsed_wait_time > 600:
                        logger.warning(f"⚠️ 10分間処理活動がありません。ワーカーの状態を確認中...")
                        health_status = self.check_worker_health()
                        logger.warning(f"ワーカー健康状態: {health_status}")
                        
                    # 20分間無活動の場合は詳細分析
                    if elapsed_wait_time > 1200:
                        logger.error(f"❌ 20分間処理活動がありません。キューマネージャーの状態を確認します")
                        queue_stats = self.queue_manager.get_stats()
                        logger.error(f"キューマネージャー統計: {queue_stats}")

            # バッチ処理完了
            batch_elapsed = time.time() - batch_start_time
            self.orchestrator_stats["batches_processed"] += 1

            # 統計情報をサニタイズしてログ出力
            safe_batch_stats = sanitize_for_log(batch_stats)
            logger.info(
                f"バッチ処理完了: 送信={safe_batch_stats['companies_sent']}件, "
                f"受信={safe_batch_stats['results_received']}件, "
                f"成功={safe_batch_stats['success_count']}件, "
                f"失敗={safe_batch_stats['failed_count']}件, "
                f"エラー={safe_batch_stats['error_count']}件, "
                f"処理時間={batch_elapsed:.2f}秒"
            )

            if pending_tasks > 0:
                incomplete_rate = (pending_tasks / len(task_ids)) * 100 if task_ids else 0
                logger.error(f"❌ バッチ処理タイムアウト: {pending_tasks}件が未完了 ({incomplete_rate:.1f}%)")
                logger.error(f"タイムアウト時間: {max_wait_time}秒 ({max_wait_time/60:.1f}分)")
                
                # 未完了タスクの詳細分析
                try:
                    queue_stats = self.queue_manager.get_stats()
                    logger.error(f"最終キューマネージャー統計: {queue_stats}")
                    
                    health_status = self.check_worker_health()
                    logger.error(f"最終ワーカー健康状態: {health_status}")
                except Exception as analysis_error:
                    logger.error(f"未完了タスク分析エラー: {analysis_error}")
            else:
                logger.info(f"✅ 全{len(task_ids)}件のタスクが正常に完了しました")

            return batch_stats

        except Exception as e:
            logger.error(f"Error processing companies batch: {e}")
            raise

    async def _collect_worker_result(self, result: WorkerResult):
        """
        ワーカー結果を収集・変換

        Args:
            result: ワーカー処理結果
        """
        try:
            # ワーカー結果をform_finder形式に変換（完全スレッドセーフ版）
            additional_data = result.additional_data or {}
            form_urls = additional_data.get('form_urls', [])
            
            form_finder_result = {
                'record_id': result.record_id,
                'form_urls': form_urls,
                'form_found': result.status == ResultStatus.SUCCESS and len(form_urls) > 0,
                'status': 'success' if result.status == ResultStatus.SUCCESS else 'failed',
                'business_status': 'success' if len(form_urls) > 0 else 'failed',
                'error_message': result.error_message if result.status != ResultStatus.SUCCESS else None,
                'processed_at': datetime.utcnow().isoformat(),
                'exploration_details': additional_data.get('exploration_details', {})
            }

            # 結果リストとすべての統計をアトミックに更新
            with self.results_lock:
                self.results.append(form_finder_result)
                
            with self.stats_lock:
                self.total_processed += 1

                # 技術的成功・失敗のカウント
                if result.status == ResultStatus.SUCCESS:
                    self.total_successful += 1
                else:
                    self.total_failed += 1

                # ビジネス成功・失敗のカウント
                if form_finder_result['business_status'] == 'success':
                    self.business_successful += 1
                    self.total_forms_found += len(form_finder_result['form_urls'])
                else:
                    self.business_failed += 1

        except Exception as e:
            logger.error(f"Error collecting worker result: {e}")

    def validate_company_data(self, company_data: Dict[str, Any]) -> bool:
        """
        企業データの妥当性を検証

        Args:
            company_data: 検証対象の企業データ

        Returns:
            bool: データが有効な場合True

        Raises:
            ValueError: データが無効な場合
        """
        # 必須フィールドのチェック
        required_fields = ["record_id", "company_url"]
        for field in required_fields:
            if not company_data.get(field):
                raise ValueError(f"Missing required field: {field}")

        # URL形式の検証
        company_url = company_data.get("company_url", "")
        if not company_url.startswith(("http://", "https://")):
            raise ValueError("Invalid company URL format")

        # データ型の検証
        if not isinstance(company_data.get("record_id"), int):
            try:
                company_data["record_id"] = int(company_data["record_id"])
            except (ValueError, TypeError):
                raise ValueError("Record ID must be an integer")

        return True

    def check_worker_health(self) -> Dict[int, str]:
        """
        ワーカーのヘルス状態をチェック

        Returns:
            Dict[int, str]: worker_id -> status のマップ
        """
        # プロセス生存チェック
        for i, process in enumerate(self.worker_processes):
            if process.is_alive():
                current_status = self.worker_status.get(i, "unknown")
                if current_status not in ["ready", "healthy"]:
                    with self._status_lock:
                        self.worker_status[i] = "healthy"
            else:
                with self._status_lock:
                    self.worker_status[i] = "dead"
                logger.error(f"Worker {i} process is dead (exit code: {process.exitcode})")

        # キューマネージャーのヘルスチェックも実行
        queue_health = self.queue_manager.check_worker_health()

        # 結果をマージ
        for worker_id, status in queue_health.items():
            if worker_id < len(self.worker_processes):
                if status == "unresponsive":
                    with self._status_lock:
                        self.worker_status[worker_id] = "unresponsive"

        with self._status_lock:
            return self.worker_status.copy()

    def calculate_form_discovery_rate(self) -> float:
        """フォーム発見率を計算（スレッドセーフ版）"""
        try:
            with self.stats_lock:
                if self.total_processed <= 0:
                    return 0.0
                rate = (self.business_successful / self.total_processed) * 100
                return round(rate, 1)
        except (ZeroDivisionError, TypeError, ValueError):
            return 0.0

    def save_results(self):
        """処理結果をJSONファイルに保存（スレッドセーフ版）"""
        try:
            execution_time = time.time() - self.start_time

            # データを一括でスナップショット取得
            with self.results_lock:
                results_snapshot = self.results.copy()
            
            with self.stats_lock:
                stats_snapshot = {
                    'total_processed': self.total_processed,
                    'total_successful': self.total_successful,
                    'total_failed': self.total_failed,
                    'business_successful': self.business_successful,
                    'business_failed': self.business_failed,
                    'total_forms_found': self.total_forms_found,
                }

            # メイン結果ファイル
            results_data = {
                'batch_id': self.batch_id,
                'processed_at': datetime.utcnow().isoformat(),
                'execution_time': round(max(0, execution_time), 2),
                **stats_snapshot,
                'form_discovery_rate': self.calculate_form_discovery_rate(),
                'results': results_snapshot
            }

            results_file = ARTIFACTS_DIR / 'form_finder_results.json'
            with open(results_file, 'w', encoding='utf-8') as f:
                json.dump(results_data, f, ensure_ascii=False, indent=2)

            logger.info(f"結果ファイル保存完了: {results_file}")

            # エラー詳細ファイル（失敗がある場合のみ）
            failed_results = [r for r in results_snapshot if r['status'] == 'failed']
            if failed_results:
                error_data = {
                    'batch_id': self.batch_id,
                    'error_count': len(failed_results),
                    'errors': failed_results
                }

                error_file = ARTIFACTS_DIR / 'form_finder_error_report.json'
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(error_data, f, ensure_ascii=False, indent=2)

                logger.info(f"エラーレポート保存完了: {error_file}")

        except Exception as e:
            logger.error(f"結果保存エラー: {e}")
            raise

    async def shutdown_workers(self, timeout: float = 30) -> bool:
        """
        ワーカープロセスを正常終了

        Args:
            timeout: 終了タイムアウト（秒）

        Returns:
            bool: 全ワーカーが正常終了したかどうか
        """
        if not self.is_running:
            return True

        try:
            logger.info("Shutting down form finder worker processes...")

            # 終了シグナル送信
            self.queue_manager.send_shutdown_signal()

            # ワーカーの終了を待機
            shutdown_success = self.queue_manager.wait_for_workers_shutdown(timeout)

            # プロセス終了を確認・強制終了
            all_terminated = True
            for i, process in enumerate(self.worker_processes):
                if process.is_alive():
                    logger.warning(f"Worker {i} still alive after shutdown signal")
                    try:
                        process.terminate()
                        process.join(timeout=5)

                        if process.is_alive():
                            logger.error(f"Force killing worker {i}")
                            process.kill()
                            process.join(timeout=2)
                            all_terminated = False
                    except Exception as e:
                        logger.error(f"Error terminating worker {i}: {e}")
                        all_terminated = False
                else:
                    logger.info(f"Worker {i} terminated successfully")

            self.is_running = False
            return shutdown_success and all_terminated

        except Exception as e:
            logger.error(f"Error during worker shutdown: {e}")
            return False

    def get_processing_summary(self) -> Dict[str, Any]:
        """
        処理サマリーを取得

        Returns:
            Dict[str, Any]: 処理サマリー
        """
        elapsed_time = time.time() - self.start_time

        return {
            "processing_mode": "multi_process",
            "batch_id": self.batch_id,
            "num_workers": self.num_workers,
            "total_companies": len(self.batch_data),
            "processed_count": self.total_processed,
            "success_count": self.total_successful,
            "failed_count": self.total_failed,
            "business_successful_count": self.business_successful,
            "business_failed_count": self.business_failed,
            "total_forms_found": self.total_forms_found,
            "form_discovery_rate": self.calculate_form_discovery_rate(),
            "elapsed_time": elapsed_time,
            "orchestrator_stats": {
                "batches_processed": self.orchestrator_stats["batches_processed"],
                "total_companies_sent": self.orchestrator_stats["total_companies_sent"],
                "total_results_received": self.orchestrator_stats["total_results_received"],
            },
            "worker_health": self.check_worker_health(),
            "queue_stats": self.queue_manager.get_stats(),
        }

    async def cleanup(self):
        """リソース全体のクリーンアップ"""
        try:
            logger.info("Starting form finder orchestrator cleanup...")

            # ワーカーシャットダウン
            await self.shutdown_workers(timeout=30)

            # キューマネージャークリーンアップ
            self.queue_manager.cleanup()

            logger.info("Form finder orchestrator cleanup completed")

        except Exception as e:
            logger.error(f"Error during form finder orchestrator cleanup: {e}")


class ConfigurableFormFinderOrchestrator(FormFinderOrchestrator):
    """設定ファイル対応Form Finderオーケストレーター"""

    def __init__(self, batch_id: str, batch_data: List[Dict[str, Any]]):
        """
        設定ファイルからワーカー数を自動取得

        Args:
            batch_id: バッチID
            batch_data: 処理対象の企業データリスト
        """
        try:
            worker_config = get_worker_config()
            # form_finder専用設定を優先、フォールバックでmulti_process設定
            form_finder_config = worker_config.get("form_finder_multi_process", {})
            if form_finder_config:
                num_workers = form_finder_config.get("num_workers", 2)
                # GitHub Actions環境の場合はより適切な値を使用
                if is_github_actions():
                    github_workers = form_finder_config.get("github_actions_workers", 2)
                    num_workers = min(github_workers, 3)  # 安全のため最大3
                    logger.info(
                        "GitHub Actions detected, using %s form_finder workers",
                        num_workers,
                    )
            else:
                # フォールバック：既存のmulti_process設定
                multi_process_config = worker_config.get("multi_process", {})
                num_workers = multi_process_config.get("num_workers", 2)
                logger.info("Using fallback multi_process config for form_finder")

        except Exception as e:
            logger.warning(f"Could not load worker config, using default (2 workers): {e}")
            num_workers = 2

        super().__init__(batch_id, batch_data, num_workers)
        logger.info(f"ConfigurableFormFinderOrchestrator initialized with {num_workers} workers")
