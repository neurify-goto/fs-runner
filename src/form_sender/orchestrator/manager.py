"""
マルチプロセス・オーケストレーター管理

既存のContinuousProcessControllerを活用して、
マルチワーカープロセスでの企業データ処理を統括する
"""

import asyncio
import contextlib
import logging
import multiprocessing as mp
import os
import signal
import time
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from ..control.continuous_processor import ContinuousProcessController
from ..utils.error_classifier import ErrorClassifier
from ..communication.queue_manager import (
    QueueManager,
    WorkerResult,
    ResultStatus,
    TaskType,
    QueueOverflowError,
    WorkerCommunicationError,
)
from ..worker.isolated_worker import worker_process_main
from ..security.log_sanitizer import sanitize_for_log
from ..validation.config_validator import ConfigValidator
from ..detection.prohibition_detector import ProhibitionDetector
from ..utils.config_helper import get_form_sender_config
from ..utils.config_loader import get_performance_monitoring_config
from config.manager import get_worker_config

logger = logging.getLogger(__name__)


class MultiProcessOrchestrator:
    """マルチプロセス・オーケストレーター管理クラス"""

    def __init__(self, targeting_id: int, num_workers: int = 2, headless: bool = None):
        """
        初期化

        Args:
            targeting_id: ターゲティングID
            num_workers: ワーカープロセス数
            headless: ブラウザヘッドレスモード (None=環境自動判定, True=強制ヘッドレス, False=強制GUI)
        """
        self.targeting_id = targeting_id
        self.num_workers = num_workers
        self.headless = headless
        
        # プロセス状態管理の競合回避用ロック
        self._process_lock = threading.Lock()

        # 設定値の妥当性検証
        try:
            worker_config = get_worker_config()
            is_valid, validation_errors = ConfigValidator.validate_full_config(worker_config)

            if not is_valid:
                logger.error(f"Configuration validation failed: {validation_errors}")
                # 重大な設定エラーの場合は推奨設定を表示
                recommendations = ConfigValidator.get_safe_config_recommendations()
                logger.info(f"Recommended safe configuration: {recommendations}")
                # 設定エラーでも動作を継続（警告レベル）

        except Exception as e:
            logger.warning(f"Configuration validation could not be performed: {e}")

        # 即時保存モード（既定: 有効）
        # True: ワーカー結果を受領次第DBへ個別保存
        # False: 結果バッファに貯めてバッチ保存
        self.immediate_save: bool = True

        # 既存の制御クラスを活用
        self.controller = ContinuousProcessController(targeting_id)

        # プロセス間通信管理
        self.queue_manager = QueueManager(num_workers)

        # ワーカープロセス管理
        self.worker_processes = []
        self.worker_status = {}  # worker_id -> status
        self._status_lock = threading.Lock()  # 競合状態対策

        # 処理統計
        self.orchestrator_stats = {
            "start_time": time.time(),
            "batches_processed": 0,
            "total_companies_sent": 0,
            "total_results_received": 0,
            "active_tasks": 0,
        }

        # バッファ管理設定の読み込み（パフォーマンス監視設定から取得）
        try:
            performance_config = get_performance_monitoring_config()
            buffer_config = performance_config.buffer_management
            overflow_config = buffer_config.overflow_buffer
            
            # バッファ設定の初期化
            self.result_buffer = []
            self.buffer_lock = threading.Lock()
            self.last_buffer_flush = time.time()
            
            # 背圧制御設定（設定ファイルから取得）
            self.backpressure_levels = buffer_config.backpressure_levels
            self.MAX_OVERFLOW_SIZE = overflow_config.max_overflow_size
            self.EMERGENCY_FILE_PREFIX = overflow_config.emergency_file_prefix
            self.CLEANUP_AFTER_HOURS = overflow_config.cleanup_after_hours
            
            # DB書き込み設定の読み込み（既存の設定と統合）
            db_batch_config = get_form_sender_config("db_batch_writing")
            self.BATCH_SIZE = db_batch_config.get("batch_size", 20)
            self.BUFFER_TIMEOUT = db_batch_config.get("buffer_timeout_seconds", 30)
            self.MAX_BUFFER_SIZE = db_batch_config.get("max_buffer_size", 100)
            self.RETRY_BACKOFF_BASE = db_batch_config.get("retry_backoff_base", 2)
            self.RETRY_BACKOFF_MAX = db_batch_config.get("retry_backoff_max_seconds", 60)
            self.MAX_PARALLEL_DB_WRITES = db_batch_config.get("max_parallel_writes", 5)
        except Exception as e:
            logger.warning(f"Could not load buffer management config, using defaults: {e}")
            # デフォルト値を使用
            self.result_buffer = []
            self.buffer_lock = threading.Lock()
            self.last_buffer_flush = time.time()
            
            self.BATCH_SIZE = 20
            self.BUFFER_TIMEOUT = 30
            self.MAX_BUFFER_SIZE = 100
            self.RETRY_BACKOFF_BASE = 2
            self.RETRY_BACKOFF_MAX = 60
            
            # デフォルトの背圧制御設定
            from ..utils.config_loader import BackpressureLevels, OverflowBuffer
            self.backpressure_levels = BackpressureLevels(
                level_1_threshold=0.8,
                level_2_threshold=0.9,
                level_3_threshold=0.95,
                level_4_threshold=1.0
            )
            self.MAX_OVERFLOW_SIZE = 1000
            self.EMERGENCY_FILE_PREFIX = "emergency_overflow"
            self.CLEANUP_AFTER_HOURS = 24

        logger.info(
            f"DB Batch writing config: batch_size={self.BATCH_SIZE}, "
            f"timeout={self.BUFFER_TIMEOUT}s, max_buffer={self.MAX_BUFFER_SIZE}"
        )

        # DB書き込みのセマフォ（即時保存時の過度な並列を抑制）
        try:
            self._db_write_sem = asyncio.Semaphore(max(1, int(self.MAX_PARALLEL_DB_WRITES)))
        except Exception:
            self._db_write_sem = asyncio.Semaphore(5)

        # 制御フラグ（改良版）
        self.is_running = False
        self.should_stop = False
        
        # 復旧処理ロック管理用
        self._recovery_in_progress = False
        
        # 高度営業禁止検出機能（Form Analyzer準拠）
        self.prohibition_detector = ProhibitionDetector()
        self.prohibition_detection_stats = {
            'total_checked': 0,
            'prohibition_detected_count': 0,
            'skipped_companies': []
        }

        # シャットダウン待機中の非同期排出制御
        self._shutdown_draining: bool = False
        self._shutdown_bg_tasks = set()

        logger.info(f"MultiProcessOrchestrator initialized: targeting_id={targeting_id}, workers={num_workers}")
        logger.info("Advanced prohibition detection enabled (Form Analyzer compatible)")

    @contextlib.contextmanager
    def _acquire_ordered_locks(self):
        """
        デッドロック防止のための順序統一ロック取得
        
        常に以下の順序でロックを取得してデッドロックを防ぐ：
        1. _process_lock (プロセス状態管理)
        2. _status_lock (ワーカーステータス)  
        3. buffer_lock (バッファ操作)
        """
        with self._process_lock:
            with self._status_lock:
                with self.buffer_lock:
                    yield

    @contextlib.contextmanager  
    def _acquire_process_status_locks(self):
        """プロセスとステータスロックのみの順序統一取得"""
        with self._process_lock:
            with self._status_lock:
                yield

    @contextlib.contextmanager
    def _acquire_status_buffer_locks(self):
        """ステータスとバッファロックのみの順序統一取得"""
        with self._status_lock:
            with self.buffer_lock:
                yield

    def initialize_supabase(self):
        """Supabase初期化（コントローラー経由）"""
        self.controller.initialize_supabase()
        logger.info("Supabase initialized via controller")

    async def start_workers(self) -> bool:
        """
        ワーカープロセスを起動

        Returns:
            bool: 全ワーカーが正常起動したかどうか
        """
        try:
            logger.info(f"Starting {self.num_workers} worker processes...")

            # ワーカープロセス起動
            for worker_id in range(self.num_workers):
                process = mp.Process(
                    target=worker_process_main,
                    args=(worker_id, self.queue_manager.task_queue, self.queue_manager.result_queue, self.headless),
                    name=f"form-sender-worker-{worker_id}",
                )
                process.start()
                self.worker_processes.append(process)
                self.worker_status[worker_id] = "starting"

                logger.info(f"Worker {worker_id} process started (PID: {process.pid})")

            # ワーカーの準備完了を待機（重複カウント防止版）
            ready_worker_ids = set()
            timeout_start = time.time()
            max_startup_time = 60  # 最大60秒で起動

            while len(ready_worker_ids) < self.num_workers and (time.time() - timeout_start) < max_startup_time:
                results = self.queue_manager.get_all_available_results()

                for result in results:
                    if result.status == ResultStatus.WORKER_READY and result.worker_id not in ready_worker_ids:
                        ready_worker_ids.add(result.worker_id)
                        self.worker_status[result.worker_id] = "ready"
                        logger.info(f"Worker {result.worker_id} is ready ({len(ready_worker_ids)}/{self.num_workers})")

                if len(ready_worker_ids) < self.num_workers:
                    await asyncio.sleep(1)

            if len(ready_worker_ids) == self.num_workers:
                logger.info("All workers are ready!")
                self.is_running = True
                return True
            else:
                logger.error(f"Only {len(ready_worker_ids)}/{self.num_workers} workers became ready within timeout")
                return False

        except Exception as e:
            logger.error(f"Error starting workers: {e}")
            return False

    async def process_companies_batch(
        self, client_config: Dict[str, Any], client_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        企業バッチ処理（マルチワーカー版）

        Args:
            client_config: クライアント設定
            client_data: クライアントデータ

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
            # 企業データを取得（既存のコントローラー使用）
            companies = self.controller.get_target_companies_batch(client_config)

            # 【テスト限定】 --test-batch-size が指定されている場合は、
            # 送信対象を「残り必要件数」のみ（例: 1件）に事前に絞る。
            # これにより2件目以降がキューに投入されることを防止する。
            try:
                if hasattr(self, 'test_batch_size') and self.test_batch_size is not None:
                    already = getattr(self, 'processed_count', 0)
                    remaining = max(0, int(self.test_batch_size) - int(already))
                    if remaining <= 0:
                        logger.info("Test batch limit already reached before dispatch. Skipping this batch.")
                        # 後段の処理に進まず即返す
                        return batch_stats
                    if len(companies) > remaining:
                        logger.info(f"Test mode active: limiting companies from {len(companies)} to {remaining}")
                        companies = companies[:remaining]
            except Exception as _e:
                # テスト制御に失敗しても通常動作は継続（安全側）
                logger.warning(f"Test batch size limiting skipped due to error: {_e}")

            if not companies:
                logger.info("No companies to process in this batch")
                return batch_stats

            logger.info(f"Processing batch of {len(companies)} companies with {self.num_workers} workers")

            # ワーカーにタスクを分散送信（RuleBasedAnalyzerリアルタイム解析、エラーハンドリング強化）
            task_ids, error_stats = await self._dispatch_companies_to_workers(
                companies, client_data, batch_stats
            )

            # エラー統計のログ出力
            queue_overflow_count = error_stats["queue_overflow_count"]
            communication_error_count = error_stats["communication_error_count"]
            
            if queue_overflow_count > 0:
                logger.warning(f"Queue overflow errors in batch: {queue_overflow_count}")
            if communication_error_count > 0:
                logger.warning(f"Worker communication errors in batch: {communication_error_count}")
            
            # 営業禁止検出統計のログ出力（失敗の一種として）
            prohibition_detected_failures = batch_stats.get("prohibition_detected_failures", 0)
            if prohibition_detected_failures > 0:
                logger.info(f"営業禁止文言検出による失敗: {prohibition_detected_failures}件")

            if not task_ids:
                if prohibition_detected_failures > 0:
                    logger.info(f"全{prohibition_detected_failures}件の企業が営業禁止文言検出により失敗しました")
                else:
                    logger.warning("No tasks were successfully sent to workers")
                return batch_stats

            # ワーカーからの結果を収集（デッドロック対策強化版）
            pending_tasks = len(task_ids)
            max_wait_time = 300  # 最大5分待機
            last_activity = time.time()
            no_activity_count = 0
            max_no_activity_cycles = 60  # 30秒 × 60 = 30分の無活動まで許容

            while pending_tasks > 0 and (time.time() - batch_start_time) < max_wait_time:
                # 結果取得
                results = self.queue_manager.get_all_available_results()

                for result in results:
                    if result.status in [ResultStatus.SUCCESS, ResultStatus.FAILED, ResultStatus.ERROR, ResultStatus.PROHIBITION_DETECTED]:
                        # 企業処理結果の場合
                        pending_tasks -= 1
                        batch_stats["results_received"] += 1
                        self.orchestrator_stats["total_results_received"] += 1
                        last_activity = time.time()
                        no_activity_count = 0  # 活動があったのでリセット

                        # 結果をバッファに追加（バッチDB書き込み対応）
                        await self._buffer_worker_result(result)

                        # 統計更新（PROHIBITION_DETECTEDはFAILEDとして扱う）
                        if result.status == ResultStatus.SUCCESS:
                            batch_stats["success_count"] += 1
                        elif result.status in [ResultStatus.FAILED, ResultStatus.PROHIBITION_DETECTED]:
                            batch_stats["failed_count"] += 1
                            # 営業禁止検出の詳細統計
                            if result.status == ResultStatus.PROHIBITION_DETECTED:
                                batch_stats["prohibition_detected_failures"] = batch_stats.get("prohibition_detected_failures", 0) + 1
                        else:  # ERROR
                            batch_stats["error_count"] += 1

                        logger.debug(f"Processed result for company {result.record_id}: {result.status.value}")

                # 結果がなかった場合の待機と監視
                if not results:
                    await asyncio.sleep(0.5)
                    no_activity_count += 1

                    # 定期的なワーカーヘルスチェック（10秒ごと）
                    if no_activity_count % 20 == 0:
                        health_status = self.check_worker_health()
                        dead_workers = [w_id for w_id, status in health_status.items() if status == "dead"]

                        if dead_workers:
                            logger.error(f"Dead workers detected during result waiting: {dead_workers}")
                            # 緊急復旧を試行
                            recovery_attempted = await self.recover_failed_workers()
                            if recovery_attempted:
                                logger.info("Emergency worker recovery attempted")
                            else:
                                logger.error("Emergency worker recovery failed")

                    # 無活動中にオーバーフローバッファの再送も試行
                    try:
                        await self._process_overflow_buffer()
                    except Exception as _ofe:
                        logger.debug(f"Overflow reprocess during wait failed: {_ofe}")

                # 段階的タイムアウトアラート
                elapsed_wait_time = time.time() - last_activity
                if elapsed_wait_time > 30 and no_activity_count % 20 == 0:  # 30秒以上無活動
                    logger.warning(f"No activity for {elapsed_wait_time:.1f} seconds, pending tasks: {pending_tasks}")

                    # 極めて長時間の無活動（5分以上）の場合は強制中断を検討
                    if elapsed_wait_time > 300:
                        logger.error(f"Extremely long inactivity ({elapsed_wait_time:.1f}s), forcing batch completion")
                        break

                # 無活動サイクル上限チェック（30分相当）
                if no_activity_count >= max_no_activity_cycles:
                    logger.error(f"Maximum no-activity cycles reached ({no_activity_count}), forcing batch completion")
                    break

            # バッチ処理完了
            batch_elapsed = time.time() - batch_start_time
            self.orchestrator_stats["batches_processed"] += 1

            # 統計情報をサニタイズしてログ出力（営業禁止検出を失敗の内訳として含む）
            safe_batch_stats = sanitize_for_log(batch_stats)
            prohibition_detected_failures_final = safe_batch_stats.get('prohibition_detected_failures', 0)
            
            # ベースログ情報
            log_msg = (
                f"Batch completed: sent={safe_batch_stats['companies_sent']}, "
                f"received={safe_batch_stats['results_received']}, "
                f"success={safe_batch_stats['success_count']}, "
                f"failed={safe_batch_stats['failed_count']}, "
                f"errors={safe_batch_stats['error_count']}, "
                f"time={batch_elapsed:.2f}s"
            )
            
            # 営業禁止検出による失敗の詳細情報
            if prohibition_detected_failures_final > 0:
                log_msg += f" (prohibition_failures={prohibition_detected_failures_final})"
            
            logger.info(log_msg)

            if pending_tasks > 0:
                logger.warning(f"{pending_tasks} tasks are still pending after batch completion")

            # バッチ完了時に残りバッファをフラッシュ
            await self._flush_result_buffer()

            # バッチ終了時にオーバーフローバッファの再送を試行
            try:
                await self._process_overflow_buffer()
            except Exception as _ofe2:
                logger.debug(f"Overflow reprocess at batch end failed: {_ofe2}")

            return batch_stats

        except Exception as e:
            logger.error(f"Error processing companies batch: {e}", exc_info=True)
            raise

    async def _buffer_worker_result(self, result: WorkerResult):
        """
        ワーカー結果をバッファに追加（バッチ書き込み用）

        Args:
            result: ワーカー処理結果
        """
        try:
            # シャットダウン排出中は、DB保存や背圧フラッシュを行わず最小限のappendのみ
            if getattr(self, '_shutdown_draining', False):
                with self.buffer_lock:
                    self.result_buffer.append(result)
                return

            if result.record_id is None:
                logger.warning(f"Worker result missing record_id: {result.task_id}")
                return

            # Race condition回避：time.time()をロック外で取得
            current_time = time.time()

            # 即時保存モード: バッファを使わず直ちにDBへ保存（失敗時はフォールバック）
            if getattr(self, 'immediate_save', False):
                try:
                    await self._save_single_result(result)
                    return
                except Exception as save_err:
                    logger.error(f"Immediate save failed for {result.record_id}: {save_err}")
                    # フォールバック1: オーバーフローバッファ
                    try:
                        saved = await self._save_to_overflow_buffer(result)
                        if saved:
                            logger.warning(f"Saved result {result.record_id} to overflow buffer after immediate save failure")
                            return
                    except Exception as overflow_err:
                        logger.error(f"Overflow buffer save failed for {result.record_id}: {overflow_err}")
                    # フォールバック2: 一時ファイル
                    try:
                        await self._save_to_temp_file(result)
                        logger.critical(f"Emergency temporary file saved for {result.record_id}")
                        return
                    except Exception as temp_err:
                        logger.error(f"Emergency temp save failed for {result.record_id}: {temp_err}")
                        # ここまで失敗したら諦めて例外を再送出
                        raise

            # 段階的バックプレッシャー機構の実装
            buffer_utilization = len(self.result_buffer) / self.MAX_BUFFER_SIZE
            backpressure_applied = await self._apply_graduated_backpressure(buffer_utilization, result)
            
            # バックプレッシャーで処理完了した場合は早期リターン
            if backpressure_applied:
                return

            with self.buffer_lock:

                # 通常の場合：バッファに追加
                self.result_buffer.append(result)
                buffer_size = len(self.result_buffer)
                should_flush = (
                    buffer_size >= self.BATCH_SIZE or 
                    (current_time - self.last_buffer_flush) >= self.BUFFER_TIMEOUT or
                    buffer_size >= int(self.MAX_BUFFER_SIZE * 0.9)  # 90%到達時も積極フラッシュ
                )

            # バッファサイズまたはタイムアウト到達時にフラッシュ
            if should_flush:
                await self._flush_result_buffer()

        except Exception as e:
            logger.error(f"Error buffering worker result: {e}", exc_info=True)

    async def _flush_result_buffer(self):
        """
        バッファ内の結果をバッチでDBに書き込み
        """
        results_to_flush = []

        # アトミックにバッファを取得・クリア
        with self.buffer_lock:
            if not self.result_buffer:
                return
            results_to_flush = self.result_buffer.copy()
            self.result_buffer.clear()
            self.last_buffer_flush = time.time()

        if not results_to_flush:
            return

        logger.info(f"Flushing {len(results_to_flush)} results to database")

        try:
            # バッチで処理（並列実行）
            flush_tasks = []
            for result in results_to_flush:
                task = asyncio.create_task(self._save_single_result(result))
                flush_tasks.append(task)

            # 全て並列実行
            flush_results = await asyncio.gather(*flush_tasks, return_exceptions=True)

            # 結果の集約
            success_count = 0
            error_count = 0

            for i, flush_result in enumerate(flush_results):
                if isinstance(flush_result, Exception):
                    error_count += 1
                    logger.error(f"Failed to save result {results_to_flush[i].record_id}: {flush_result}")
                else:
                    success_count += 1

            logger.info(f"Batch DB write completed: {success_count} success, {error_count} errors")

        except Exception as e:
            logger.error(f"Critical error during batch flush: {e}", exc_info=True)
            # バッチ失敗時は個別リトライ
            await self._retry_failed_results(results_to_flush)

    async def _drain_result_queue(self, max_wait_seconds: float = 5.0, idle_sleep: float = 0.2) -> int:
        """結果キューから未処理結果をすべて回収し、即時保存/フラッシュする。

        - クリーンアップ直前/直後に呼び出し、取りこぼしを防ぐ。
        - 既存の単体保存ロジック（_save_single_result）を利用。

        Args:
            max_wait_seconds: 最大待機時間（秒）
            idle_sleep: 無活動時のスリープ間隔（秒）

        Returns:
            回収・保存した結果数
        """
        try:
            start = time.time()
            drained = 0
            idle_loops = 0
            while (time.time() - start) < max_wait_seconds:
                results = self.queue_manager.get_all_available_results()
                if not results:
                    idle_loops += 1
                    await asyncio.sleep(idle_sleep)
                    # 一定回数無活動なら終了
                    if idle_loops >= int(max_wait_seconds / max(idle_sleep, 0.1)):
                        break
                    continue

                # 活動があればカウンタをリセット
                idle_loops = 0

                for result in results:
                    try:
                        # ワーカー準備通知はスキップ
                        if result.status not in [ResultStatus.SUCCESS, ResultStatus.FAILED, ResultStatus.ERROR, ResultStatus.PROHIBITION_DETECTED]:
                            continue
                        await self._save_single_result(result)
                        drained += 1
                    except Exception as e:
                        logger.error(f"Error draining single result {getattr(result, 'record_id', None)}: {e}")

            # 念のためバッファをフラッシュ
            await self._flush_result_buffer()
            if drained:
                logger.info(f"Drained and saved {drained} pending results from queue")
            return drained
        except Exception as e:
            logger.error(f"Error draining result queue: {e}")
            return 0

    async def _save_single_result(self, result: WorkerResult):
        """
        単一結果のDB保存（バッチ処理用 + 即時SHUTDOWN機能）

        Args:
            result: ワーカー処理結果
        """
        try:
            # 【重要】 テストバッチサイズ制御をDB保存前に実行（即時停止のため）
            shutdown_requested = False
            if hasattr(self, 'test_batch_size') and self.test_batch_size is not None:
                self.processed_count += 1
                logger.info(f"Test mode: processed {self.processed_count}/{self.test_batch_size} records")
                
                if self.processed_count >= self.test_batch_size:
                    logger.info(f"Test batch size limit reached ({self.test_batch_size}). Setting stop flag and sending shutdown tasks IMMEDIATELY.")
                    self.should_stop = True
                    shutdown_requested = True
                    
                    # 【即時SHUTDOWN】 DB保存と並列でSHUTDOWNシグナルを送信
                    shutdown_task = asyncio.create_task(self._send_shutdown_tasks_to_all_workers())
            
            # 既存のコントローラーの保存メソッドを使用
            # PROHIBITION_DETECTEDもfailedとして扱う
            status_string = "success" if result.status == ResultStatus.SUCCESS else "failed"

            async with self._db_write_sem:
                # DB保存とinstruction_valid更新を並列実行（単一結果内での並列）
                save_tasks = []
                
                # 基本の結果保存
                # 詳細分類（可能なら付与）
                detail = self._make_classify_detail(result.error_type, result.error_message)
                # 追加入力リトライのメタがあれば classify_detail に統合
                try:
                    if result.additional_data and isinstance(result.additional_data, dict):
                        retry_meta = result.additional_data.get('retry')
                        if retry_meta:
                            if detail is None:
                                detail = {}
                            # 機微情報を含まない最小限のメタのみ格納
                            safe_retry = {
                                'attempted': bool(retry_meta.get('attempted')),
                                'reason': retry_meta.get('reason'),
                                'invalid_count': int(retry_meta.get('invalid_count', 0)),
                                'filled_count': int(retry_meta.get('filled_count', 0)),
                                'filled_categories': list(retry_meta.get('filled_categories', []))[:10],
                                'result': retry_meta.get('result'),
                            }
                            detail['retry'] = safe_retry
                except Exception:
                    pass

                save_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.controller.save_result_immediately,
                        result.record_id,
                        status_string,
                        result.error_type,
                        result.instruction_valid_updated,
                        result.bot_protection_detected,
                        detail,
                    )
                )
                save_tasks.append(save_task)
                
                # instruction_valid更新が必要な場合
                if result.instruction_valid_updated:
                    is_valid = result.status == ResultStatus.SUCCESS
                    if hasattr(self.controller, 'update_instruction_validity'):
                        validity_task = asyncio.create_task(
                            asyncio.to_thread(
                                self.controller.update_instruction_validity,
                                result.record_id,
                                is_valid
                            )
                        )
                        save_tasks.append(validity_task)
                    else:
                        logger.debug("Instruction validity update skipped (method not available)")
                
                await asyncio.gather(*save_tasks)
            
            # SHUTDOWNタスクがある場合は完了を待機
            if shutdown_requested and 'shutdown_task' in locals():
                await shutdown_task
                logger.info(f"SHUTDOWN signal sent successfully after processing record {result.record_id}")

        except Exception as e:
            logger.error(f"Error saving single result to database: {e}")
            raise

    async def _emergency_flush_buffer(self):
        """
        緊急時用の部分バッファフラッシュ
        バックプレッシャー適用時に使用
        """
        emergency_batch_size = max(50, int(self.MAX_BUFFER_SIZE * 0.3))  # バッファの30%または50件のうち大きい方
        results_to_flush = []

        # アトミックに部分バッファを取得
        with self.buffer_lock:
            if len(self.result_buffer) >= emergency_batch_size:
                results_to_flush = self.result_buffer[:emergency_batch_size]
                self.result_buffer = self.result_buffer[emergency_batch_size:]
                logger.info(f"Emergency flush: processing {len(results_to_flush)} results, {len(self.result_buffer)} remaining")
            else:
                # バッファサイズが小さい場合は全てを処理
                results_to_flush = self.result_buffer.copy()
                self.result_buffer.clear()
                logger.info(f"Emergency flush: processing all {len(results_to_flush)} remaining results")

        if not results_to_flush:
            return

        # 緊急フラッシュでは個別保存でリスクを最小化
        successful_count = 0
        failed_results = []
        
        for result in results_to_flush:
            try:
                await self._save_single_result(result)
                successful_count += 1
            except Exception as e:
                logger.error(f"Emergency flush failed for result {result.record_id}: {e}")
                failed_results.append(result)

        logger.info(f"Emergency flush completed: {successful_count} successful, {len(failed_results)} failed")

    async def _apply_graduated_backpressure(self, buffer_utilization: float, result: 'WorkerResult') -> bool:
        """
        段階的バックプレッシャー機構
        
        Args:
            buffer_utilization: バッファ使用率 (0.0-1.0)
            result: 処理対象の結果
            
        Returns:
            bool: True=バックプレッシャーで処理完了, False=通常処理続行
        """
        try:
            # 設定から閾値を取得
            bp = self.backpressure_levels
            
            if buffer_utilization < bp.level_1_threshold:
                # レベル1未満：通常処理
                return False
                
            elif buffer_utilization < bp.level_2_threshold:
                # レベル1-2：部分フラッシュトリガー
                logger.info(f"Backpressure Level 1: Buffer at {buffer_utilization:.1%}, triggering partial flush (threshold: {bp.level_1_threshold:.1%})")
                await self._emergency_flush_buffer()
                return False
                
            elif buffer_utilization < bp.level_3_threshold:
                # レベル2-3：処理速度制限 + 積極フラッシュ
                logger.warning(f"Backpressure Level 2: Buffer at {buffer_utilization:.1%}, applying processing throttle (threshold: {bp.level_2_threshold:.1%})")
                await asyncio.sleep(0.1)  # 処理速度を制限
                await self._emergency_flush_buffer()
                return False
                
            elif buffer_utilization < bp.level_4_threshold:
                # レベル3-4：新規受付一時停止 + 強制フラッシュ
                logger.warning(f"Backpressure Level 3: Buffer at {buffer_utilization:.1%}, pausing intake and force flushing (threshold: {bp.level_3_threshold:.1%})")
                
                flush_attempts = 0
                max_flush_attempts = 3
                
                while len(self.result_buffer) >= int(self.MAX_BUFFER_SIZE * bp.level_3_threshold) and flush_attempts < max_flush_attempts:
                    flush_attempts += 1
                    logger.info(f"Force flush attempt #{flush_attempts}")
                    await self._emergency_flush_buffer()
                    await asyncio.sleep(0.5)  # フラッシュ間隔
                    
                # フラッシュ後にスペースができれば通常処理続行
                if len(self.result_buffer) < int(self.MAX_BUFFER_SIZE * bp.level_2_threshold):
                    return False
                else:
                    # フラッシュに失敗した場合は個別保存へ進行
                    pass
                    
            # レベル4以上：緊急個別保存モード
            logger.critical(f"Backpressure Level 4: Buffer overflow ({buffer_utilization:.1%}), emergency data preservation (threshold: {bp.level_4_threshold:.1%})")
            
            try:
                # 永続化オーバーフローバッファへの保存
                overflow_saved = await self._save_to_overflow_buffer(result)
                if overflow_saved:
                    logger.info(f"Result {result.record_id} saved to overflow buffer")
                    return True
                    
                # オーバーフローバッファも失敗した場合は直接DB保存
                await self._save_single_result(result)
                logger.info(f"Emergency direct save successful for result {result.record_id}")
                return True
                
            except Exception as emergency_error:
                logger.error(f"All emergency preservation methods failed for {result.record_id}: {emergency_error}")
                
                # 最後の手段：一時ファイル保存
                try:
                    await self._save_to_temp_file(result)
                    logger.critical(f"Result {result.record_id} saved to temporary file as last resort")
                    return True
                except Exception as temp_error:
                    logger.error(f"Temporary file save failed for {result.record_id}: {temp_error}")
                    # データ損失を防ぐため例外を発生させる
                    raise ResourceWarning(f"Critical: Unable to preserve data for {result.record_id} - all fallback methods exhausted")
                    
        except Exception as e:
            logger.error(f"Backpressure mechanism error: {e}")
            return False

    async def _save_to_overflow_buffer(self, result: 'WorkerResult') -> bool:
        """
        永続化オーバーフローバッファへの保存
        
        Args:
            result: 保存する結果
            
        Returns:
            bool: 保存成功時True
        """
        try:
            import tempfile
            import json
            
            # オーバーフローディレクトリの作成（設定からプレフィックスを取得）
            overflow_dir = Path(tempfile.gettempdir()) / "form_sender_overflow"
            overflow_dir.mkdir(exist_ok=True)
            
            # 結果をJSON形式で保存（設定からファイル名プレフィックスを使用）
            overflow_file = overflow_dir / f"{self.EMERGENCY_FILE_PREFIX}_{result.record_id}_{int(time.time())}.json"
            
            result_data = {
                'record_id': result.record_id,
                'status': result.status.value if hasattr(result.status, 'value') else str(result.status),
                'error_type': result.error_type,
                'instruction_valid_updated': result.instruction_valid_updated,
                'bot_protection_detected': result.bot_protection_detected,
                'timestamp': time.time(),
                'overflow_reason': 'buffer_overflow'
            }
            
            with open(overflow_file, 'w', encoding='utf-8') as f:
                json.dump(result_data, f, ensure_ascii=False, indent=2)
                
            logger.info(f"Result {result.record_id} saved to overflow buffer: {overflow_file}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save to overflow buffer: {e}")
            return False

    async def _save_to_temp_file(self, result: 'WorkerResult') -> None:
        """
        最後の手段：一時ファイルへの保存
        
        Args:
            result: 保存する結果
        """
        import tempfile
        import json
        from pathlib import Path
        
        try:
            # 緊急用ディレクトリの作成
            emergency_dir = Path(tempfile.gettempdir()) / "form_sender_emergency"
            emergency_dir.mkdir(exist_ok=True)
            
            # タイムスタンプ付きで保存
            emergency_file = emergency_dir / f"emergency_{result.record_id}_{int(time.time())}.json"
            
            result_data = {
                'record_id': result.record_id,
                'status': result.status.value if hasattr(result.status, 'value') else str(result.status),
                'error_type': result.error_type,
                'instruction_valid_updated': result.instruction_valid_updated,
                'bot_protection_detected': result.bot_protection_detected,
                'timestamp': time.time(),
                'emergency_reason': 'all_buffers_exhausted'
            }
            
            with open(emergency_file, 'w', encoding='utf-8') as f:
                json.dump(result_data, f, ensure_ascii=False, indent=2)
                
            logger.critical(f"Emergency save completed: {emergency_file}")
            
        except Exception as e:
            logger.error(f"Emergency save failed: {e}")
            raise

    async def _process_overflow_buffer(self) -> None:
        """
        オーバーフローバッファの処理（システム回復時に実行）
        """
        try:
            import tempfile
            import json
            from pathlib import Path
            
            overflow_dir = Path(tempfile.gettempdir()) / "form_sender_overflow"
            if not overflow_dir.exists():
                return
                
            overflow_files = list(overflow_dir.glob("overflow_*.json"))
            if not overflow_files:
                logger.info("No overflow files to process")
                return
                
            logger.info(f"Processing {len(overflow_files)} overflow files")
            processed_count = 0
            failed_count = 0
            
            for overflow_file in overflow_files:
                try:
                    with open(overflow_file, 'r', encoding='utf-8') as f:
                        result_data = json.load(f)
                    
                    # データベースに保存
                    await self._save_overflow_result_to_db(result_data)
                    
                    # 処理完了後にファイル削除
                    overflow_file.unlink()
                    processed_count += 1
                    
                except Exception as e:
                    logger.error(f"Failed to process overflow file {overflow_file}: {e}")
                    failed_count += 1
                    
            logger.info(f"Overflow buffer processing completed: {processed_count} processed, {failed_count} failed")
            
        except Exception as e:
            logger.error(f"Overflow buffer processing error: {e}")

    async def _save_overflow_result_to_db(self, result_data: dict) -> None:
        """
        オーバーフローバッファからデータベースへの保存
        
        Args:
            result_data: 保存するデータ
        """
        try:
            # 詳細分類（可能なら付与）
            detail = self._make_classify_detail(result_data.get('error_type'), result_data.get('error_message'))

            self.controller.save_result_immediately(
                result_data['record_id'],
                "success" if result_data['status'] == "SUCCESS" else "failed",
                result_data['error_type'],
                result_data['instruction_valid_updated'],
                result_data['bot_protection_detected'],
                detail,
            )
            
            if result_data['instruction_valid_updated']:
                is_valid = result_data['status'] == "SUCCESS"
                if hasattr(self.controller, 'update_instruction_validity'):
                    self.controller.update_instruction_validity(result_data['record_id'], is_valid)
                else:
                    logger.debug("Instruction validity update skipped (method not available)")
                
            logger.info(f"Overflow result {result_data['record_id']} successfully saved to database")
            
        except Exception as e:
            logger.error(f"Failed to save overflow result to database: {e}")
            raise

    def _make_classify_detail(self, error_type: Optional[str], error_message: Optional[str]) -> Optional[dict]:
        """classify_detail の共通呼び出し。安全に失敗をログ化して None を返す。"""
        try:
            msg = (error_message or error_type or "")
            detail = ErrorClassifier.classify_detail(
                error_message=msg,
                page_content="",
                http_status=None,
            )
            if error_type:
                detail['code'] = error_type
            return detail
        except (ValueError, RuntimeError, KeyError) as _known:
            logger.warning(f"classify_detail failed (known): {type(_known).__name__}: {_known}")
            return None
        except Exception as _unknown:
            # 予期しないエラーは型も含めて警告
            logger.warning(f"classify_detail failed (unexpected {type(_unknown).__name__}): {_unknown}")
            return None

    async def _retry_failed_results(self, failed_results: List[WorkerResult]):
        """
        失敗した結果の指数バックオフリトライ

        Args:
            failed_results: 失敗した結果リスト
        """
        logger.warning(f"Retrying {len(failed_results)} failed results individually with backoff")

        for result in failed_results:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await self._save_single_result(result)
                    logger.debug(f"Retry successful for result {result.record_id} on attempt {attempt + 1}")
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        # 指数バックオフ計算
                        backoff_time = min(self.RETRY_BACKOFF_BASE**attempt, self.RETRY_BACKOFF_MAX)
                        logger.warning(
                            f"Retry attempt {attempt + 1} failed for result {result.record_id}: {e}, "
                            f"retrying in {backoff_time}s"
                        )
                        await asyncio.sleep(backoff_time)
                    else:
                        logger.error(f"All retry attempts failed for result {result.record_id}: {e}")
                        # 最終的に失敗した場合もログのみ（データ損失防止）

    def check_business_conditions(self, client_config: Dict[str, Any]) -> Tuple[bool, str]:
        """
        営業条件チェック（既存のコントローラー使用）

        Args:
            client_config: クライアント設定

        Returns:
            Tuple[bool, str]: (継続可能, 理由)
        """
        # 時間制限・日次制限チェック
        can_continue, reason = self.controller.should_continue_processing(client_config)
        if not can_continue:
            return False, reason

        # 営業時間チェック
        if not self.controller.is_within_business_hours(client_config):
            return False, "営業時間外"

        return True, "継続可能"

    def update_worker_status_atomic(self, worker_id: int, new_status: str) -> None:
        """
        ワーカー状態をアトミックに更新

        Args:
            worker_id: ワーカーID
            new_status: 新しい状態
        """
        with self._status_lock:
            old_status = self.worker_status.get(worker_id, "unknown")
            self.worker_status[worker_id] = new_status
            if old_status != new_status:
                logger.info(f"Worker {worker_id}: {old_status} -> {new_status}")

    def check_worker_health(self) -> Dict[int, str]:
        """
        ワーカーのヘルス状態をチェック

        Returns:
            Dict[int, str]: worker_id -> status のマップ
        """
        # プロセス生存チェック（アトミック更新）
        for i, process in enumerate(self.worker_processes):
            if process.is_alive():
                current_status = self.worker_status.get(i, "unknown")
                if current_status not in ["ready", "healthy"]:
                    self.update_worker_status_atomic(i, "healthy")
            else:
                self.update_worker_status_atomic(i, "dead")
                logger.error(f"Worker {i} process is dead (exit code: {process.exitcode})")

        # キューマネージャーのヘルスチェックも実行
        queue_health = self.queue_manager.check_worker_health()

        # 結果をマージ（アトミック更新）
        for worker_id, status in queue_health.items():
            if worker_id < len(self.worker_processes):
                if status == "unresponsive":
                    self.update_worker_status_atomic(worker_id, "unresponsive")

        with self._status_lock:
            return self.worker_status.copy()

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
        required_fields = ["id", "form_url", "company_name"]
        for field in required_fields:
            if not company_data.get(field):
                raise ValueError(f"Missing required field: {field}")

        # URL形式の検証
        form_url = company_data.get("form_url", "")
        if not form_url.startswith(("http://", "https://")):
            raise ValueError("Invalid form URL format")

        # データ型の検証
        if not isinstance(company_data.get("id"), int):
            try:
                company_data["id"] = int(company_data["id"])
            except (ValueError, TypeError):
                raise ValueError("Company ID must be an integer")

        # 企業名の文字数制限
        company_name = company_data.get("company_name", "")
        if len(company_name) > 200:
            raise ValueError(f"Company name too long: {len(company_name)} chars (max 200)")

        # セキュリティチェック（基本的なサニタイゼーション）
        for field_name in ["company_name", "form_url"]:
            field_value = str(company_data.get(field_name, ""))
            # 危険な文字列パターンのチェック
            dangerous_patterns = ["<script", "javascript:", "data:text/html"]
            if any(pattern in field_value.lower() for pattern in dangerous_patterns):
                logger.warning(f"Potentially dangerous content in {field_name}: {sanitize_for_log(field_value)}")
                raise ValueError(f"Invalid content detected in {field_name}")

        logger.debug(f"Company data validation passed for ID: {company_data['id']}")
        return True

    async def recover_failed_workers(self) -> bool:
        """
        異常終了したワーカーの復旧処理

        Returns:
            bool: 復旧処理が成功したかどうか
        """
        if not self.is_running:
            return False

        try:
            health_status = self.check_worker_health()
            failed_workers = [
                worker_id for worker_id, status in health_status.items() if status in ["dead", "unresponsive"]
            ]

            if not failed_workers:
                return True  # 失敗したワーカーなし

            logger.warning(f"Attempting to recover {len(failed_workers)} failed workers: {failed_workers}")

            # 並列復旧処理（効率化）
            recovery_tasks = []
            for worker_id in failed_workers:
                recovery_task = asyncio.create_task(self._restart_single_worker_with_logging(worker_id))
                recovery_tasks.append(recovery_task)

            # 全復旧タスクを並列実行
            try:
                recovery_results = await asyncio.gather(*recovery_tasks, return_exceptions=True)

                # 結果の集約
                successful_recoveries = []
                failed_recoveries = []

                for i, result in enumerate(recovery_results):
                    worker_id = failed_workers[i]
                    if isinstance(result, Exception):
                        logger.error(f"Exception during worker {worker_id} recovery: {result}")
                        failed_recoveries.append(worker_id)
                    elif result is True:
                        successful_recoveries.append(worker_id)
                    else:
                        failed_recoveries.append(worker_id)

                # サマリーログ
                if successful_recoveries:
                    logger.info(f"Successfully recovered workers: {successful_recoveries}")
                if failed_recoveries:
                    logger.error(f"Failed to recover workers: {failed_recoveries}")

                # 全て成功した場合のみTrueを返す
                return len(failed_recoveries) == 0

            except Exception as e:
                logger.error(f"Critical error during parallel worker recovery: {e}")
                return False

        except Exception as e:
            logger.error(f"Error during worker recovery: {e}")
            return False

    async def _restart_single_worker_with_logging(self, worker_id: int) -> bool:
        """
        ログ付き単一ワーカー再起動（並列処理用）

        Args:
            worker_id: 再起動するワーカーID

        Returns:
            bool: 再起動が成功したかどうか
        """
        try:
            logger.info(f"Starting recovery for worker {worker_id}")
            success = await self._restart_single_worker(worker_id)

            if success:
                logger.info(f"Worker {worker_id} recovery completed successfully")
            else:
                logger.error(f"Worker {worker_id} recovery failed")

            return success

        except Exception as e:
            logger.error(f"Exception during worker {worker_id} recovery: {e}")
            return False

    async def _restart_single_worker(self, worker_id: int) -> bool:
        """
        単一ワーカーの再起動処理

        Args:
            worker_id: 再起動するワーカーID

        Returns:
            bool: 再起動が成功したかどうか
        """
        try:
            # 既存プロセスの強制終了
            if worker_id < len(self.worker_processes):
                old_process = self.worker_processes[worker_id]
                if old_process.is_alive():
                    logger.info(f"Terminating old worker {worker_id} process")
                    old_process.terminate()
                    old_process.join(timeout=5)

                    if old_process.is_alive():
                        logger.warning(f"Force killing worker {worker_id}")
                        old_process.kill()
                        old_process.join(timeout=2)

            # 新しいプロセスの起動
            logger.info(f"Starting new worker {worker_id} process")
            new_process = mp.Process(
                target=worker_process_main,
                args=(worker_id, self.queue_manager.task_queue, self.queue_manager.result_queue, self.headless),
                name=f"form-sender-worker-{worker_id}",
            )
            new_process.start()

            # プロセスリストを更新
            if worker_id < len(self.worker_processes):
                self.worker_processes[worker_id] = new_process
            else:
                self.worker_processes.append(new_process)

            self.worker_status[worker_id] = "starting"
            logger.info(f"New worker {worker_id} process started (PID: {new_process.pid})")

            # ワーカーの準備完了を待機
            timeout_start = time.time()
            max_startup_time = 60  # 最大60秒で起動

            while (time.time() - timeout_start) < max_startup_time:
                results = self.queue_manager.get_all_available_results()

                for result in results:
                    if result.status == ResultStatus.WORKER_READY and result.worker_id == worker_id:
                        self.worker_status[worker_id] = "ready"
                        logger.info(f"Restarted worker {worker_id} is ready")
                        return True

                await asyncio.sleep(1)

            logger.error(f"Restarted worker {worker_id} did not become ready within timeout")
            return False

        except Exception as e:
            logger.error(f"Error restarting worker {worker_id}: {e}")
            return False

    async def monitor_and_recover_workers(self, check_interval: float = 30) -> None:
        """
        ワーカー監視・復旧バックグラウンドタスク

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
                    logger.warning(f"Unhealthy workers detected: {unhealthy_workers}")

                    # 復旧処理実行
                    recovery_success = await self.recover_failed_workers()
                    if recovery_success:
                        logger.info("Worker recovery completed successfully")
                    else:
                        logger.error("Worker recovery failed or incomplete")

                # 保留中タスクの監視
                pending_count = self.queue_manager.get_pending_task_count()
                if pending_count > 50:  # 閾値は設定可能
                    logger.warning(f"High number of pending tasks: {pending_count}")

                await asyncio.sleep(check_interval)

            except Exception as e:
                logger.error(f"Error in worker monitoring task: {e}")
                await asyncio.sleep(check_interval)

        logger.info("Worker monitoring task stopped")

    def get_error_statistics(self) -> Dict[str, Any]:
        """
        エラー統計情報を取得

        Returns:
            Dict[str, Any]: エラー統計
        """
        try:
            health_status = self.check_worker_health()
            stats = self.queue_manager.get_stats()

            return {
                "worker_health": health_status,
                "queue_errors": stats.get("errors", 0),
                "pending_tasks": stats.get("pending_tasks", 0),
                "dead_workers": len([w for w in health_status.values() if w == "dead"]),
                "unresponsive_workers": len([w for w in health_status.values() if w == "unresponsive"]),
                "healthy_workers": len([w for w in health_status.values() if w in ["ready", "healthy"]]),
                "error_rate": (stats.get("errors", 0) / max(stats.get("results_received", 1), 1)) * 100,
            }

        except Exception as e:
            logger.error(f"Error getting error statistics: {e}")
            return {"error": str(e)}

    def safe_check_alive_workers(self) -> list:
        """
        スレッドセーフなワーカー生存チェック
        
        Returns:
            list: 生存しているワーカープロセスのリスト
        """
        with self._process_lock:
            alive_processes = []
            for worker_process in self.worker_processes:
                if worker_process and worker_process.is_alive():
                    alive_processes.append(worker_process)
            return alive_processes
    
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
            logger.info("Shutting down worker processes...")

            # 終了シグナル送信
            self.queue_manager.send_shutdown_signal()

            # キューが肥大化していると SHUTDOWN 通知が末尾に埋もれて検出できない問題の対策として、
            # シャットダウン待機中は結果キューを非破壊的に掃き出しつつ、
            # ・WORKER_SHUTDOWN はカウント
            # ・通常結果はバッファに積む（DB書き込みは後段cleanupで実施）
            shutdown_success = await self._await_worker_shutdowns(timeout)

            # 完全なプロセス終了検証の実行（join/killベース）
            all_terminated = await self._verify_complete_process_termination()

            self.is_running = False
            # 実質的にはプロセスの完全終了を重視し、shutdown_success は参考値とする
            return all_terminated

        except Exception as e:
            logger.error(f"Error during worker shutdown: {e}")
            return False

    async def _dispatch_companies_to_workers(
        self, companies: list, client_data: Dict[str, Any], batch_stats: Dict[str, Any]
    ) -> tuple:
        """
        企業データをワーカーに分散送信
        
        Args:
            companies: 企業データリスト
            client_data: クライアントデータ
            batch_stats: バッチ統計
            
        Returns:
            tuple: (task_ids, error_stats)
        """
        task_ids = []
        queue_overflow_count = 0
        communication_error_count = 0

        for company in companies:
            try:
                # 企業データの検証を実行
                self.validate_company_data(company)
                
                # 事前営業禁止チェック（Form Analyzer準拠）
                prohibition_check_result = await self._check_company_prohibition(company)
                if prohibition_check_result['prohibition_detected']:
                    # 営業禁止が検出された場合は失敗として処理
                    logger.warning(f"営業禁止文言検出により企業を失敗扱い: record_id={company.get('id')}")
                    
                    # WorkerResultオブジェクトを生成（営業禁止検出専用）
                    prohibition_result = self._create_prohibition_detected_result(company, prohibition_check_result)
                    
                    # 結果をバッファに追加（通常のワーカー結果と同様に処理）
                    await self._buffer_worker_result(prohibition_result)
                    
                    # 営業禁止検出企業のDB更新（失敗ステータスとして記録）
                    await self._update_company_prohibition_status(company, prohibition_check_result)
                    
                    # 失敗統計に追加
                    batch_stats["failed_count"] += 1
                    batch_stats["prohibition_detected_failures"] = batch_stats.get("prohibition_detected_failures", 0) + 1
                    continue

                task_id = self.queue_manager.send_task(company, client_data, self.targeting_id)
                task_ids.append(task_id)
                batch_stats["companies_sent"] += 1
                self.orchestrator_stats["total_companies_sent"] += 1

            except QueueOverflowError as e:
                queue_overflow_count += 1
                logger.warning(f"Queue overflow for company {company.get('id')}: {e}")
                # キューオーバーフロー時は短時間待機してリトライ
                if queue_overflow_count <= 3:
                    await asyncio.sleep(1)
                    try:
                        # リトライ時も検証を実行
                        self.validate_company_data(company)
                        task_id = self.queue_manager.send_task(company, client_data, self.targeting_id)
                        task_ids.append(task_id)
                        batch_stats["companies_sent"] += 1
                        self.orchestrator_stats["total_companies_sent"] += 1
                        logger.info(f"Retry successful for company {company.get('id')} after queue overflow")
                    except Exception as retry_e:
                        logger.error(f"Retry failed for company {company.get('id')}: {retry_e}")
                        continue
                else:
                    logger.error(
                        f"Too many queue overflows ({queue_overflow_count}), skipping company {company.get('id')}"
                    )
                    continue

            except WorkerCommunicationError as e:
                communication_error_count += 1
                logger.error(f"Worker communication error for company {company.get('id')}: {e}")
                # 通信エラーが多い場合は処理を中断
                if communication_error_count > len(companies) * 0.3:  # 30%以上でエラー
                    logger.error("Too many worker communication errors, aborting batch")
                    break
                continue

            except ValueError as e:
                # データ検証エラー
                logger.warning(f"Company data validation failed for {company.get('id')}: {e}")
                batch_stats["validation_errors"] = batch_stats.get("validation_errors", 0) + 1
                continue

            except Exception as e:
                logger.error(f"Unexpected error sending task for company {company.get('id')}: {e}")
                continue
        
        error_stats = {
            "queue_overflow_count": queue_overflow_count,
            "communication_error_count": communication_error_count
        }
        
        return task_ids, error_stats

    async def _await_worker_shutdowns(self, timeout: float = 30) -> bool:
        """
        全ワーカーのシャットダウン通知を待機しつつ、結果キューを安全に掃き出す。

        - 大量の通常結果がキューに滞留していても、WORKER_SHUTDOWN を確実に検出する。
        - 非シャットダウンの結果はDB保存や背圧処理を行わず、buffer_lock 配下で直接バッファへ append のみ行う。
          （_buffer_worker_result は使用しない）
        - DB反映は cleanup フェーズでまとめて実行（またはバックグラウンドの非同期フラッシュ）。

        Args:
            timeout: 最大待機秒数

        Returns:
            bool: 全ワーカー分のシャットダウン通知を観測できたか
        """
        # 厳格なタイムアウトを wait_for で強制
        async def _process_with_timeout(expected_workers: int) -> bool:
            observed_shutdowns = set()
            start = time.time()
            buffered_count = 0
            buffer_errors = 0
            warn90 = False
            last_periodic_flush = start

            # shutdown中は即時保存・自動フラッシュ・背圧フラッシュを停止して純粋にバッファへ退避
            prev_immediate = getattr(self, 'immediate_save', False)
            self.immediate_save = False
            self._shutdown_draining = True

            # バックグラウンドフラッシュ制御
            flush_in_progress = False
            def _schedule_bg_flush(reason: str = "periodic"):
                nonlocal flush_in_progress, last_periodic_flush
                if flush_in_progress:
                    return
                flush_in_progress = True
                async def _bg_flush():
                    nonlocal flush_in_progress
                    try:
                        await self._flush_result_buffer()
                    except Exception as e:
                        logger.warning(f"Background flush error ({reason}): {e}")
                    finally:
                        flush_in_progress = False
                task = asyncio.create_task(_bg_flush())
                # 追跡＋例外監視
                self._shutdown_bg_tasks.add(task)
                def _done_cb(t: asyncio.Task):
                    try:
                        _ = t.result()
                    except Exception as e:
                        logger.warning(f"Background flush task failed ({reason}): {e}")
                    finally:
                        self._shutdown_bg_tasks.discard(t)
                task.add_done_callback(_done_cb)
                last_periodic_flush = time.time()

            try:
                while len(observed_shutdowns) < expected_workers:
                    # 90%到達警告
                    elapsed = time.time() - start
                    if not warn90 and elapsed >= timeout * 0.9:
                        warn90 = True
                        logger.warning(
                            f"Approaching shutdown timeout: {elapsed:.1f}s, observed={len(observed_shutdowns)}/{expected_workers}"
                        )

                    results = self.queue_manager.get_all_available_results()
                    if not results:
                        # 5秒ごとの非同期フラッシュ（awaitしない）
                        now = time.time()
                        try:
                            # バッファが空でなければperiodic flushをスケジュール
                            buffer_len = len(self.result_buffer) if hasattr(self, 'result_buffer') else 0
                            if buffer_len > 0 and (now - last_periodic_flush) >= 5.0:
                                _schedule_bg_flush("periodic")
                        except Exception as _pf:
                            logger.warning(f"Periodic flush scheduling failed: {_pf}")

                        await asyncio.sleep(0.2)
                        continue

                    for result in results:
                        try:
                            if result.status == ResultStatus.WORKER_SHUTDOWN:
                                observed_shutdowns.add(result.worker_id)
                                logger.info(
                                    f"Worker {result.worker_id} shutdown observed "
                                    f"({len(observed_shutdowns)}/{expected_workers})"
                                )
                                continue

                            # WORKER_READY等は無視。通常結果は最小限のロックでバッファへ退避（非同期でDB保存はしない）
                            if result.status in [
                                ResultStatus.SUCCESS,
                                ResultStatus.FAILED,
                                ResultStatus.ERROR,
                                ResultStatus.PROHIBITION_DETECTED,
                            ]:
                                with self.buffer_lock:
                                    self.result_buffer.append(result)
                                    current_buffer_size = len(self.result_buffer)
                                buffered_count += 1

                                # メモリセーフガード：肥大し過ぎたら非同期フラッシュ（awaitしない）
                                try:
                                    if current_buffer_size > int(self.MAX_BUFFER_SIZE) * 2:
                                        logger.warning(
                                            f"Buffer growing large during shutdown (size={current_buffer_size}), scheduling background flush"
                                        )
                                        _schedule_bg_flush("emergency")
                                except Exception as _ge:
                                    logger.warning(f"Background flush scheduling failed: {_ge}")
                        except Exception as e:
                            buffer_errors += 1
                            logger.warning(f"Error while handling result during shutdown wait: {e}")

                # 追加メトリクス
                try:
                    import psutil
                    rss = psutil.Process().memory_info().rss
                    rss_mb = rss / (1024 * 1024)
                    mem_msg = f", rss={rss_mb:.1f}MB"
                except Exception:
                    mem_msg = ""

                final_buffer_size = len(self.result_buffer) if hasattr(self, 'result_buffer') else 0
                total_elapsed = time.time() - start
                logger.info(
                    f"Shutdown wait summary: observed={len(observed_shutdowns)}/{expected_workers}, "
                    f"buffered={buffered_count}, buffer_errors={buffer_errors}, "
                    f"final_buffer={final_buffer_size}, elapsed={total_elapsed:.2f}s{mem_msg}"
                )
                return True
            finally:
                # 元の設定に戻す（cleanupで正式フラッシュ）
                self._shutdown_draining = False
                self.immediate_save = prev_immediate
                # 未観測ワーカーの詳細を警告
                if len(observed_shutdowns) < expected_workers:
                    missing_workers = sorted(list(set(range(expected_workers)) - observed_shutdowns))
                    logger.warning(f"Unobserved worker shutdowns: {missing_workers}")
                # バックグラウンドフラッシュのクリーンアップ
                try:
                    if self._shutdown_bg_tasks:
                        for t in list(self._shutdown_bg_tasks):
                            if not t.done():
                                t.cancel()
                        # タスクの完了を短時間待機
                        await asyncio.gather(*list(self._shutdown_bg_tasks), return_exceptions=True)
                except Exception as _ce:
                    logger.debug(f"Background flush cleanup error: {_ce}")

        try:
            expected_workers = len(self.worker_processes) if hasattr(self, 'worker_processes') else self.num_workers
            return await asyncio.wait_for(_process_with_timeout(expected_workers), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Shutdown wait timed out after {timeout}s")
            return False
        except Exception as e:
            logger.error(f"Error awaiting worker shutdowns: {e}")
            return False

    async def _verify_complete_process_termination(self) -> bool:
        """
        完全なプロセス終了検証（/proc/<pid>/status確認 + ゾンビ検出）
        
        Returns:
            bool: 全プロセスが完全終了した場合True
        """
        try:
            import signal
            import asyncio
            
            # 設定から検証設定を取得
            perf_config = get_performance_monitoring_config()
            termination_config = perf_config.process_management.termination_verification
            
            all_terminated = True
            zombie_pids = []
            
            for i, process in enumerate(self.worker_processes):
                if process.is_alive():
                    logger.warning(f"Worker {i} (PID: {process.pid}) still alive after shutdown signal")
                    
                    # 段階的終了プロセス
                    try:
                        process.terminate()  # SIGTERM
                        await asyncio.sleep(2)
                        
                        if process.is_alive():
                            process.kill()  # SIGKILL
                            await asyncio.sleep(1)
                            
                        process.join(timeout=5)
                        
                        # /proc/<pid>/statusでプロセス状態を完全確認
                        if termination_config.proc_status_verification:
                            is_zombie = self._check_process_status(process.pid)
                            if is_zombie:
                                zombie_pids.append(process.pid)
                                logger.warning(f"Zombie process detected: PID {process.pid}")
                                
                        if process.is_alive():
                            all_terminated = False
                            logger.error(f"Worker {i} (PID: {process.pid}) failed to terminate")
                        else:
                            logger.info(f"Worker {i} (PID: {process.pid}) terminated successfully")
                            
                    except Exception as e:
                        logger.error(f"Error terminating worker {i}: {e}")
                        all_terminated = False
                else:
                    logger.info(f"Worker {i} already terminated")
            
            # ゾンビプロセスのクリーンアップ
            if zombie_pids and termination_config.zombie_cleanup_enabled:
                await self._cleanup_zombie_processes(zombie_pids)
                
            # FD リーク検出
            if termination_config.fd_leak_detection:
                await self._detect_fd_leaks()
                
            return all_terminated
            
        except Exception as e:
            logger.error(f"Error during complete process termination verification: {e}")
            return False
    
    def _check_process_status(self, pid: int) -> bool:
        """
        /proc/<pid>/statusファイルからプロセス状態を確認
        
        Args:
            pid: プロセスID
            
        Returns:
            bool: ゾンビプロセスの場合True
        """
        try:
            status_file = Path(f"/proc/{pid}/status")
            if not status_file.exists():
                return False  # プロセスが完全に終了
                
            with open(status_file, 'r') as f:
                for line in f:
                    if line.startswith('State:'):
                        state = line.split()[1]
                        return state == 'Z'  # Z = Zombie
            return False
        except (FileNotFoundError, PermissionError, OSError):
            return False  # プロセス終了済み
    
    async def _cleanup_zombie_processes(self, zombie_pids: list):
        """ゾンビプロセスのクリーンアップ"""
        for pid in zombie_pids:
            try:
                os.waitpid(pid, 0)
                logger.info(f"Zombie process {pid} cleaned up")
            except (ChildProcessError, OSError) as e:
                logger.warning(f"Could not clean up zombie process {pid}: {e}")
    
    async def _detect_fd_leaks(self):
        """ファイルディスクリプタリークの検出と積極的対処"""
        try:
            import psutil
            current_process = psutil.Process()
            num_fds = current_process.num_fds() if hasattr(current_process, 'num_fds') else 0
            
            if num_fds > 1000:  # 閾値：1000を超える場合は積極的対処
                logger.warning(f"High number of file descriptors detected: {num_fds}")
                await self._handle_fd_leak(num_fds)
            elif num_fds > 800:  # 警告レベル
                logger.warning(f"Moderate FD usage detected: {num_fds} (approaching limit)")
            else:
                logger.info(f"File descriptor count: {num_fds}")
        except ImportError:
            logger.info("psutil not available, skipping FD leak detection")
        except Exception as e:
            logger.warning(f"Error detecting FD leaks: {e}")

    async def _handle_fd_leak(self, current_fds: int):
        """
        ファイルディスクリプタリークの積極的対処
        
        Args:
            current_fds: 現在のFD数
        """
        logger.warning(f"Initiating FD leak mitigation: {current_fds} descriptors")
        
        try:
            # Phase 1: 緊急リソースクリーンアップ
            await self._emergency_resource_cleanup()
            
            # Phase 2: 強制ガベージコレクション
            await self._force_gc_collection()
            
            # Phase 3: バッファ緊急フラッシュ
            if hasattr(self, 'result_buffer'):
                await self._flush_result_buffer()
                logger.info("Emergency buffer flush completed")
            
            # Phase 4: FD数再確認
            try:
                import psutil
                current_process = psutil.Process()
                new_fds = current_process.num_fds() if hasattr(current_process, 'num_fds') else 0
                reduction = current_fds - new_fds
                
                if reduction > 0:
                    logger.info(f"FD leak mitigation successful: reduced by {reduction} descriptors ({new_fds} remaining)")
                else:
                    logger.warning(f"FD leak mitigation had limited effect: {new_fds} descriptors remaining")
                    
            except Exception as e:
                logger.warning(f"Could not verify FD reduction: {e}")
                
        except Exception as e:
            logger.error(f"Error during FD leak mitigation: {e}")

    async def _emergency_resource_cleanup(self):
        """緊急リソースクリーンアップ"""
        try:
            # 古いキャッシュをクリア
            if hasattr(self, 'cache'):
                self.cache.clear()
            
            # 一時ファイルをクリーンアップ
            self.cleanup_temp_files()
            
            # 古いプロセス参照をクリーンアップ
            self.worker_processes = [p for p in self.worker_processes if p.is_alive()]
            
            logger.info("Emergency resource cleanup completed")
            
        except Exception as e:
            logger.warning(f"Error during emergency resource cleanup: {e}")

    async def _force_gc_collection(self):
        """強制ガベージコレクション"""
        try:
            import gc
            
            # 全世代のガベージコレクションを実行
            collected_objects = gc.collect()
            logger.info(f"Forced garbage collection: {collected_objects} objects collected")
            
            # 循環参照の確認
            if hasattr(gc, 'get_stats'):
                stats = gc.get_stats()
                logger.info(f"GC stats: {stats}")
                
        except Exception as e:
            logger.warning(f"Error during forced garbage collection: {e}")

    def get_processing_summary(self) -> Dict[str, Any]:
        """
        処理サマリーを取得（コントローラーと統合）

        Returns:
            Dict[str, Any]: 処理サマリー
        """
        # 既存のコントローラーサマリー
        controller_summary = self.controller.get_processing_summary()

        # オーケストレーター固有の統計
        orchestrator_elapsed = time.time() - self.orchestrator_stats["start_time"]

        # 統合サマリー作成
        integrated_summary = {
            **controller_summary,
            "processing_mode": "multi_process",
            "num_workers": self.num_workers,
            "orchestrator_stats": {
                "elapsed_time": orchestrator_elapsed,
                "batches_processed": self.orchestrator_stats["batches_processed"],
                "total_companies_sent": self.orchestrator_stats["total_companies_sent"],
                "total_results_received": self.orchestrator_stats["total_results_received"],
                "active_tasks": self.queue_manager.get_pending_task_count(),
            },
            "worker_health": self.check_worker_health(),
            "queue_stats": self.queue_manager.get_stats(),
        }

        return integrated_summary

    def cleanup_temp_files(self):
        """一時ファイルクリーンアップ（コントローラー経由）"""
        self.controller.cleanup_temp_files()

    async def cleanup(self):
        """リソース全体のクリーンアップ"""
        try:
            logger.info("Starting orchestrator cleanup...")

            # キューに残っている結果を先に回収・保存してからフラッシュ
            await self._drain_result_queue(max_wait_seconds=5.0, idle_sleep=0.2)

            # ワーカーシャットダウン
            await self.shutdown_workers(timeout=30)

            # シャットダウン中に届いた最後の結果も念のため回収
            await self._drain_result_queue(max_wait_seconds=2.0, idle_sleep=0.2)

            # キューマネージャークリーンアップ
            self.queue_manager.cleanup()

            # 最後に溢れ結果をDBへ再送
            try:
                await self._process_overflow_buffer()
            except Exception as _eof:
                logger.debug(f"Overflow buffer final reprocess failed: {_eof}")

            # 一時ファイルクリーンアップ
            self.cleanup_temp_files()

            logger.info("Orchestrator cleanup completed")

        except Exception as e:
            logger.error(f"Error during orchestrator cleanup: {e}")


class ConfigurableOrchestrator(MultiProcessOrchestrator):
    """設定ファイル対応オーケストレーター"""

    def __init__(self, targeting_id: int, headless: bool = None, test_batch_size: int = None):
        """
        設定ファイルからワーカー数を自動取得

        Args:
            targeting_id: ターゲティングID
            headless: ブラウザヘッドレスモード (None=環境自動判定, True=強制ヘッドレス, False=強制GUI)
            test_batch_size: テスト用バッチサイズ (None=デフォルト設定使用)
        """
        # テスト用バッチサイズが指定されている場合は1ワーカーに制限
        if test_batch_size is not None:
            num_workers = 1
            logger.info(f"Test mode detected (test_batch_size={test_batch_size}): forcing single worker for efficiency")
        else:
            # 通常の設定値取得処理（Form-Sender専用設定を優先。フォールバックでmulti_process）
            try:
                form_sender_config = get_form_sender_config()
                # 優先: form_sender_multi_process
                if form_sender_config:
                    base_workers = int(form_sender_config.get("num_workers", 2))
                    max_workers = int(form_sender_config.get("max_workers", base_workers))
                    if os.getenv("GITHUB_ACTIONS") == "true":
                        github_workers = int(form_sender_config.get("github_actions_workers", base_workers))
                        num_workers = min(github_workers, max_workers)
                        logger.info(f"GitHub Actions detected, using {num_workers} workers (form_sender_multi_process)")
                    else:
                        num_workers = min(base_workers, max_workers)
                        logger.info(f"Using form_sender_multi_process config: workers={num_workers}")
                else:
                    # フォールバック: multi_process（未定義の場合は2）
                    worker_config = get_worker_config()
                    multi_process_config = worker_config.get("multi_process", {})
                    num_workers = int(multi_process_config.get("num_workers", 2))
                    if os.getenv("GITHUB_ACTIONS") == "true":
                        github_workers = int(multi_process_config.get("github_actions_workers", num_workers))
                        num_workers = min(github_workers, 3)
                        logger.info(f"GitHub Actions detected, using {num_workers} workers (fallback multi_process)")
                    else:
                        logger.info("Using fallback multi_process config for form_sender")
            except Exception as e:
                logger.warning(f"Could not load worker config, using default (2 workers): {e}")
                num_workers = 2

        # 適切なワーカー数で親クラスを初期化
        super().__init__(targeting_id, num_workers, headless)
        
        # テスト用バッチサイズの適用
        self.test_batch_size = test_batch_size
        self.processed_count = 0  # 処理済みレコード数カウンタ
        if test_batch_size is not None:
            self.BATCH_SIZE = test_batch_size
            logger.info(f"Test batch size applied: {test_batch_size} (overrides default: {self.BATCH_SIZE})")
        
        headless_mode = "GUI" if headless == False else "headless" if headless == True else "auto"
        batch_info = f", test_batch_size: {test_batch_size}" if test_batch_size is not None else ""
        logger.info(f"ConfigurableOrchestrator initialized with {num_workers} workers (headless mode: {headless_mode}{batch_info})")

    async def _check_company_prohibition(self, company: Dict[str, Any]) -> Dict[str, Any]:
        """
        企業の営業禁止文言事前チェック（Form Analyzer準拠の高度検出）
        
        Args:
            company: 企業データ
            
        Returns:
            Dict[str, Any]: 営業禁止検出結果
            {
                'prohibition_detected': bool,
                'prohibition_phrases': List[str],
                'detection_method': str,
                'company_id': int,
                'form_url': str
            }
        """
        result = {
            'prohibition_detected': False,
            'prohibition_phrases': [],
            'detection_method': 'Form Analyzer準拠高度検出',
            'company_id': company.get('id'),
            'form_url': company.get('form_url', '')
        }
        
        try:
            self.prohibition_detection_stats['total_checked'] += 1
            
            form_url = company.get('form_url', '').strip()
            if not form_url:
                logger.debug(f"企業ID {company.get('id')}: フォームURLが空のため営業禁止チェックをスキップ")
                return result
            
            logger.debug(f"企業ID {company.get('id')}: 営業禁止文言の事前チェック開始")
            
            # フォームページのHTMLコンテンツを取得（軽量版）
            html_content = await self._fetch_form_page_for_prohibition_check(form_url)
            if not html_content:
                logger.warning(f"企業ID {company.get('id')}: HTMLコンテンツ取得に失敗、営業禁止チェックをスキップ")
                return result
            
            # ProhibitionDetectorによる高度検出実行
            detected, phrases = self.prohibition_detector.detect(html_content)
            
            if detected:
                result['prohibition_detected'] = True
                result['prohibition_phrases'] = phrases
                self.prohibition_detection_stats['prohibition_detected_count'] += 1
                self.prohibition_detection_stats['skipped_companies'].append({
                    'company_id': company.get('id'),
                    'form_url': form_url,
                    'phrases_count': len(phrases),
                    'detection_time': time.time()
                })
                
                logger.warning(f"企業ID {company.get('id')}: 営業禁止文言を検出 ({len(phrases)}件)")
                # セキュリティを考慮して禁止文言の詳細は最小限のログ出力
                for i, phrase in enumerate(phrases[:2]):  # 最初の2件のみ表示
                    logger.debug(f"禁止文言{i+1}: {phrase[:50]}...")
            else:
                logger.debug(f"企業ID {company.get('id')}: 営業禁止文言は検出されませんでした")
                
        except Exception as e:
            logger.error(f"企業ID {company.get('id')}: 営業禁止チェック中にエラー: {e}")
            # エラー時は安全側に倒して検出なしとする（処理を継続）
            result['prohibition_detected'] = False
            
        return result
    
    def _is_safe_url(self, url: str) -> bool:
        """
        URL安全性検証
        
        Args:
            url: 検証するURL
            
        Returns:
            bool: URL安全性（True: 安全, False: 危険）
        """
        import urllib.parse
        
        try:
            parsed = urllib.parse.urlparse(url)
            
            # プロトコル検証
            if parsed.scheme not in ['http', 'https']:
                logger.warning(f"Unsafe protocol detected: {parsed.scheme}")
                return False
                
            # ホスト名検証
            if not parsed.hostname:
                logger.warning("Empty hostname detected")
                return False
                
            # 拡張セキュリティ検証：localhost/プライベートIP/IDN攻撃防止
            hostname = parsed.hostname.lower()
            
            # 基本的なプライベートネットワークチェック
            private_networks = [
                'localhost', '127.0.0.1', '0.0.0.0', '::1',
                '192.168.', '10.', '172.16.', '172.17.', '172.18.',
                '172.19.', '172.20.', '172.21.', '172.22.', '172.23.',
                '172.24.', '172.25.', '172.26.', '172.27.', '172.28.',
                '172.29.', '172.30.', '172.31.'
            ]
            
            for private in private_networks:
                if hostname.startswith(private) or hostname == private:
                    logger.warning(f"Private/local network access blocked: {hostname}")
                    return False
            
            # IDN（国際化ドメイン名）攻撃対策
            try:
                # IDNを使った偽装ドメイン検出
                ascii_hostname = hostname.encode('ascii', 'ignore').decode('ascii')
                if ascii_hostname != hostname:
                    # 非ASCII文字が含まれる場合は追加検証
                    import unicodedata
                    normalized = unicodedata.normalize('NFKC', hostname)
                    if normalized != hostname:
                        logger.warning(f"Suspicious IDN domain detected: {hostname}")
                        return False
            except Exception as e:
                logger.warning(f"IDN validation error: {e}")
                return False
                
            # DNS rebinding攻撃対策：IP直接指定の阻止
            import re
            ip_pattern = r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
            if re.match(ip_pattern, hostname):
                logger.warning(f"Direct IP access blocked (DNS rebinding protection): {hostname}")
                return False
                
            # 基本的な長さ制限
            if len(url) > 2048:  # RFC推奨の最大URL長
                logger.warning(f"URL too long: {len(url)} characters")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"URL validation error: {e}")
            return False

    async def _fetch_form_page_for_prohibition_check(self, form_url: str) -> Optional[str]:
        """
        営業禁止チェック専用の軽量HTML取得（Basic認証・JavaScript不要）
        
        Args:
            form_url: フォームURL
            
        Returns:
            Optional[str]: HTMLコンテンツ（取得失敗時はNone）
        """
        # URL安全性検証
        if not self._is_safe_url(form_url):
            # URLは出さずに通知（CIポリシー準拠）
            logger.error("Unsafe URL blocked: ***URL_REDACTED***")
            return None
            
        try:
            import httpx
            import asyncio
            
            # 軽量なHTTPクライアント設定
            timeout = httpx.Timeout(10.0)  # 10秒タイムアウト
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            
            async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
                response = await client.get(form_url)
                
                if response.status_code == 200:
                    return response.text
                else:
                    logger.warning(f"HTTP {response.status_code} for URL: ***URL_REDACTED***")
                    return None
                    
        except httpx.TimeoutException:
            logger.warning("タイムアウト: ***URL_REDACTED***")
            return None
        except Exception as e:
            logger.debug(f"HTML取得エラー ***URL_REDACTED***: {e}")
            return None
    
    async def _update_company_prohibition_status(self, company: Dict[str, Any], prohibition_result: Dict[str, Any]):
        """
        営業禁止検出企業のデータベース更新（失敗ステータスとして統一処理）
        
        Args:
            company: 企業データ
            prohibition_result: 営業禁止検出結果
        """
        try:
            # 失敗ステータスとして統一的に記録
            update_data = {
                'status': 'failed',  # 失敗ステータスに統一
                'failure_reason': 'prohibition_detected',  # 失敗理由を明記
                'prohibition_detected': True,  # フィルタリング用のフラグは維持
                'prohibition_phrases_count': len(prohibition_result['prohibition_phrases']),
                'detection_method': prohibition_result['detection_method'],
                'error_message': f"営業禁止文言を検出 ({len(prohibition_result['prohibition_phrases'])}件)",
                'updated_at': time.time()
            }
            
            # 営業禁止検出を失敗として記録（record_idベース）
            success = await self.controller.update_company_status_async(
                company.get('id'), 
                update_data
            )
            
            if success:
                logger.info(f"企業ID {company.get('id')}: 営業禁止検出による失敗をDBに記録しました")
            else:
                logger.error(f"企業ID {company.get('id')}: 営業禁止検出による失敗のDB更新に失敗")
                
        except Exception as e:
            logger.error(f"企業ID {company.get('id')}: 営業禁止検出による失敗のDB更新エラー: {e}")
    
    def get_prohibition_detection_summary(self) -> Dict[str, Any]:
        """営業禁止検出統計の取得"""
        summary = dict(self.prohibition_detection_stats)
        if summary['total_checked'] > 0:
            summary['prohibition_detection_rate'] = summary['prohibition_detected_count'] / summary['total_checked']
        else:
            summary['prohibition_detection_rate'] = 0.0
        
        # スキップした企業の詳細は統計のみ返す（セキュリティ保護）
        summary['skipped_companies'] = len(summary['skipped_companies'])
        
        return summary
    
    def _create_prohibition_detected_result(self, company: Dict[str, Any], prohibition_result: Dict[str, Any]) -> 'WorkerResult':
        """
        営業禁止検出時のWorkResultオブジェクト生成
        
        Args:
            company: 企業データ
            prohibition_result: 営業禁止検出結果
            
        Returns:
            WorkerResult: 営業禁止検出結果のワーカー結果オブジェクト
        """
        from ..communication.queue_manager import WorkerResult, ResultStatus
        
        return WorkerResult(
            task_id=f"prohibition_check_{company.get('id')}_{int(time.time())}",
            worker_id=-1,  # 特別なワーカーID（オーケストレーター処理）
            record_id=company.get('id'),
            status=ResultStatus.PROHIBITION_DETECTED,
            error_message=f"営業禁止文言検出: {len(prohibition_result['prohibition_phrases'])}件の禁止文言",
            additional_data={
                'prohibition_detected': True,
                'prohibition_phrases_count': len(prohibition_result['prohibition_phrases']),
                'detection_method': prohibition_result['detection_method'],
                'form_url': prohibition_result['form_url'],
                'company_name': company.get('name', ''),
                'failure_reason': 'prohibition_detected',
                'processing_time': 0.0,  # 事前チェックなので処理時間は0
                'timestamp': time.time()
            },
            processing_time=0.0,
            timestamp=time.time()
        )
    
    async def _send_shutdown_tasks_to_all_workers(self):
        """
        全ワーカーにSHUTDOWNタスクを送信してテスト終了を指示
        
        test_batch_size到達時の即時停止メソッド（最高優先度）
        """
        try:
            logger.info("🛑 EMERGENCY SHUTDOWN: Sending SHUTDOWN tasks to all workers for test batch size limit")
            
            # QueueManagerを使用して高速送信
            if hasattr(self, 'queue_manager') and self.queue_manager:
                try:
                    # 全ワーカーに即座送信
                    self.queue_manager.send_shutdown_signal()
                    logger.info(f"✅ EMERGENCY SHUTDOWN signal successfully sent via QueueManager to all {self.num_workers} workers")
                    
                    # 追加セーフティ: 個別にSHUTDOWNタスクを緊急送信
                    for worker_id in range(self.num_workers):
                        emergency_shutdown_task = {
                            "task_type": TaskType.SHUTDOWN.value,
                            "worker_id": worker_id,
                            "reason": "test_batch_size_limit_emergency",
                            "priority": "IMMEDIATE"
                        }
                        
                        try:
                            # 緊急タスクとして送信（タイムアウト無し）
                            await asyncio.to_thread(
                                self.queue_manager.task_queue.put, 
                                emergency_shutdown_task
                            )
                            logger.debug(f"⚠️ Emergency SHUTDOWN task sent to worker {worker_id}")
                        except Exception as worker_error:
                            logger.error(f"Failed to send emergency SHUTDOWN to worker {worker_id}: {worker_error}")
                            
                except Exception as e:
                    logger.error(f"Failed to send SHUTDOWN signal via QueueManager: {e}")
            else:
                logger.error("QueueManager not available - CANNOT SEND SHUTDOWN TASKS!")
            
            logger.info(f"🛑 EMERGENCY SHUTDOWN tasks sent to {self.num_workers} workers")
            
        except Exception as e:
            logger.error(f"CRITICAL ERROR sending SHUTDOWN tasks to workers: {e}")
            # クリティカルエラーでも続行する（SHUTDOWNはベストエフォート）
