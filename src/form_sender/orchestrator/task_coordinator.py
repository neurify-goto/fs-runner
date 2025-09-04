"""
タスク調整モジュール

タスクの配分、結果の収集、バッファ管理を担当
"""

import asyncio
import json
import logging
import os
import pickle
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
import threading

from supabase import Client
from config.manager import get_form_sender_config
from ..security.logger import SecurityLogger
from ..utils.secure_logger import get_secure_logger
from ..communication.queue_manager import WorkerResult, WorkerTask, TaskType

logger = get_secure_logger(__name__)
security_logger = SecurityLogger()


class TaskCoordinator:
    """タスク調整とバッファ管理クラス"""

    def __init__(self, supabase_client: Client, targeting_id: int):
        self.supabase = supabase_client
        self.targeting_id = targeting_id
        
        # バッファ管理
        self.result_buffer = []
        self.buffer_lock = threading.Lock()
        self.last_buffer_flush = time.time()
        
        # オーバーフローバッファ
        self.overflow_buffer = deque(maxlen=500)
        self.overflow_lock = threading.Lock()
        
        # エラー統計
        self.error_counts = {}
        self.error_lock = threading.Lock()
        
        # 設定の読み込み
        self._load_buffer_config()

    def _load_buffer_config(self):
        """バッファ設定を読み込み"""
        try:
            # デフォルト値
            self.BATCH_SIZE = 20
            self.BUFFER_TIMEOUT = 30
            self.MAX_BUFFER_SIZE = 100
            self.RETRY_BACKOFF_BASE = 2
            self.RETRY_BACKOFF_MAX = 60
            self.MAX_PARALLEL_DB_WRITES = 5
            
            # 設定ファイルから読み込み
            db_batch_config = get_form_sender_config("db_batch_writing")
            
            if db_batch_config:
                self.BATCH_SIZE = db_batch_config.get("batch_size", 20)
                self.BUFFER_TIMEOUT = db_batch_config.get("buffer_timeout_seconds", 30)
                self.MAX_BUFFER_SIZE = db_batch_config.get("max_buffer_size", 100)
                self.RETRY_BACKOFF_BASE = db_batch_config.get("retry_backoff_base", 2)
                self.RETRY_BACKOFF_MAX = db_batch_config.get("retry_backoff_max_seconds", 60)
                self.MAX_PARALLEL_DB_WRITES = db_batch_config.get("max_parallel_writes", 5)
                
        except Exception as e:
            logger.warning(f"バッファ設定の読み込みに失敗、デフォルト値を使用: {e}")

    async def dispatch_companies_to_workers(
        self, companies: List[Dict[str, Any]], 
        task_queue: asyncio.Queue, num_workers: int
    ) -> int:
        """企業データをワーカーに配分"""
        dispatched = 0
        
        try:
            for company in companies:
                if not self.validate_company_data(company):
                    logger.warning(f"無効な企業データをスキップ: ID {company.get('id')}")
                    continue
                
                await task_queue.put(company)
                dispatched += 1
                
                # 適度な配分速度
                if dispatched % 10 == 0:
                    await asyncio.sleep(0.1)
            
            # 終了シグナルを送信
            for worker_id in range(num_workers):
                shutdown_task = WorkerTask(
                    task_id=f"shutdown_{worker_id}",
                    task_type=TaskType.SHUTDOWN
                )
                await task_queue.put(shutdown_task.to_dict())
            
            logger.info(f"{dispatched}件の企業データをワーカーに配分しました")
            return dispatched
            
        except Exception as e:
            logger.error(f"タスク配分中にエラー: {e}")
            return dispatched

    def validate_company_data(self, company_data: Dict[str, Any]) -> bool:
        """企業データの妥当性検証"""
        required_fields = ["id", "form_url"]
        
        for field in required_fields:
            if field not in company_data or not company_data[field]:
                logger.debug(f"必須フィールド '{field}' が欠落または空")
                return False
        
        # URL形式の基本チェック
        form_url = company_data.get("form_url", "")
        if not form_url.startswith(("http://", "https://")):
            logger.debug(f"無効なURL形式: {form_url}")
            return False
        
        return True

    async def buffer_worker_result(self, result: WorkerResult):
        """ワーカー結果をバッファに追加"""
        try:
            # バッファへの追加
            with self.buffer_lock:
                self.result_buffer.append(result)
                buffer_size = len(self.result_buffer)
            
            # エラー統計の更新
            if result.error_message:
                self._update_error_statistics(result.error_type, result.error_message)
            
            # バッファフラッシュの判定
            should_flush = False
            current_time = time.time()
            
            with self.buffer_lock:
                if buffer_size >= self.BATCH_SIZE:
                    should_flush = True
                    logger.debug(f"バッファサイズ {buffer_size} がバッチサイズ {self.BATCH_SIZE} に到達")
                elif current_time - self.last_buffer_flush > self.BUFFER_TIMEOUT:
                    should_flush = True
                    logger.debug(f"バッファタイムアウト {self.BUFFER_TIMEOUT}秒 経過")
                elif buffer_size >= self.MAX_BUFFER_SIZE:
                    should_flush = True
                    logger.warning(f"バッファが最大サイズ {self.MAX_BUFFER_SIZE} に到達")
            
            # フラッシュ実行
            if should_flush:
                await self.flush_result_buffer()
                
        except Exception as e:
            logger.error(f"結果バッファリング中にエラー: {e}")
            # 緊急時は直接保存を試みる
            await self._emergency_save_result(result)

    async def flush_result_buffer(self):
        """バッファ内の結果をデータベースに保存"""
        with self.buffer_lock:
            if not self.result_buffer:
                return
            
            results_to_save = self.result_buffer[:]
            self.result_buffer.clear()
            self.last_buffer_flush = time.time()
        
        if not results_to_save:
            return
        
        logger.info(f"バッファから {len(results_to_save)} 件の結果を保存開始")
        
        # 並列保存の実行
        save_tasks = []
        semaphore = asyncio.Semaphore(self.MAX_PARALLEL_DB_WRITES)
        
        async def save_with_semaphore(result):
            async with semaphore:
                await self._save_single_result(result)
        
        for result in results_to_save:
            save_tasks.append(save_with_semaphore(result))
        
        # タスクの実行と失敗の処理
        results = await asyncio.gather(*save_tasks, return_exceptions=True)
        
        failed_results = []
        for i, exc in enumerate(results):
            if isinstance(exc, Exception):
                logger.error(f"結果保存失敗: {exc}")
                failed_results.append(results_to_save[i])
        
        # 失敗した結果のリトライ
        if failed_results:
            await self._retry_failed_results(failed_results)

    async def _save_single_result(self, result: WorkerResult):
        """単一の結果をデータベースに保存"""
        try:
            update_data = {
                "status": result.status,
                "submission_result": result.submission_result,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "processed_at": result.processed_at.isoformat() if result.processed_at else None,
            }
            
            # 営業禁止検出結果の追加
            if result.prohibition_detected is not None:
                update_data["prohibition_detected"] = result.prohibition_detected
            if result.prohibition_keywords:
                update_data["prohibition_keywords"] = json.dumps(
                    result.prohibition_keywords, ensure_ascii=False
                )
            
            # データベース更新
            response = self.supabase.table("form_submissions").update(update_data).eq(
                "id", result.record_id
            ).execute()
            
            if response.data:
                logger.debug(f"記録ID {result.record_id} の結果を保存しました")
            else:
                raise Exception("データベース更新に失敗")
                
        except Exception as e:
            logger.error(f"結果保存エラー (記録ID: {result.record_id}): {e}")
            raise

    async def _retry_failed_results(self, failed_results: List[WorkerResult]):
        """失敗した結果の再試行"""
        if not failed_results:
            return
        
        logger.warning(f"{len(failed_results)} 件の失敗した結果を再試行します")
        
        for attempt in range(3):
            if not failed_results:
                break
            
            # バックオフ待機
            wait_time = min(self.RETRY_BACKOFF_BASE ** attempt, self.RETRY_BACKOFF_MAX)
            await asyncio.sleep(wait_time)
            
            # 再試行
            still_failed = []
            for result in failed_results:
                try:
                    await self._save_single_result(result)
                except Exception:
                    still_failed.append(result)
            
            failed_results = still_failed
        
        # それでも失敗した場合はオーバーフローバッファへ
        if failed_results:
            for result in failed_results:
                await self._save_to_overflow_buffer(result)

    async def _save_to_overflow_buffer(self, result: WorkerResult) -> bool:
        """オーバーフローバッファに保存"""
        try:
            with self.overflow_lock:
                self.overflow_buffer.append(result)
            
            logger.warning(f"結果をオーバーフローバッファに保存: 記録ID {result.record_id}")
            
            # 一定数溜まったら処理
            if len(self.overflow_buffer) >= 10:
                await self._process_overflow_buffer()
            
            return True
            
        except Exception as e:
            logger.error(f"オーバーフローバッファ保存エラー: {e}")
            # 最終手段としてファイルに保存
            await self._save_to_temp_file(result)
            return False

    async def _save_to_temp_file(self, result: WorkerResult) -> None:
        """一時ファイルに保存（最終手段）"""
        try:
            import tempfile
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"failed_result_{result.record_id}_{timestamp}.pkl"
            
            temp_dir = tempfile.gettempdir()
            filepath = os.path.join(temp_dir, filename)
            
            with open(filepath, "wb") as f:
                pickle.dump(result.to_dict(), f)
            
            logger.error(f"結果を一時ファイルに保存: {filepath}")
            
        except Exception as e:
            logger.critical(f"一時ファイル保存も失敗: {e}")

    async def _process_overflow_buffer(self) -> None:
        """オーバーフローバッファを処理"""
        with self.overflow_lock:
            if not self.overflow_buffer:
                return
            
            results_to_process = list(self.overflow_buffer)
            self.overflow_buffer.clear()
        
        logger.info(f"オーバーフローバッファから {len(results_to_process)} 件を処理")
        
        for result in results_to_process:
            try:
                await self._save_single_result(result)
            except Exception as e:
                logger.error(f"オーバーフロー結果の保存失敗: {e}")

    async def _emergency_save_result(self, result: WorkerResult):
        """緊急時の結果保存"""
        try:
            await self._save_single_result(result)
        except Exception:
            await self._save_to_overflow_buffer(result)

    def _update_error_statistics(self, error_type: str, error_message: str):
        """エラー統計を更新"""
        with self.error_lock:
            if error_type not in self.error_counts:
                self.error_counts[error_type] = {
                    "count": 0,
                    "last_error": None,
                    "sample_messages": []
                }
            
            self.error_counts[error_type]["count"] += 1
            self.error_counts[error_type]["last_error"] = time.time()
            
            # サンプルメッセージを保存（最大5件）
            if len(self.error_counts[error_type]["sample_messages"]) < 5:
                self.error_counts[error_type]["sample_messages"].append(
                    error_message[:200] if error_message else ""
                )

    def get_error_statistics(self) -> Dict[str, Any]:
        """エラー統計を取得"""
        with self.error_lock:
            total_errors = sum(stats["count"] for stats in self.error_counts.values())
            
            return {
                "total_errors": total_errors,
                "error_types": dict(self.error_counts),
                "most_common_error": max(
                    self.error_counts.items(),
                    key=lambda x: x[1]["count"],
                    default=(None, {"count": 0})
                )[0] if self.error_counts else None
            }

    async def drain_result_queue(self, result_queue: asyncio.Queue, max_wait: float = 5.0) -> int:
        """結果キューを排出"""
        drained_count = 0
        start_time = time.time()
        
        while time.time() - start_time < max_wait:
            try:
                result = await asyncio.wait_for(result_queue.get(), timeout=0.5)
                
                if isinstance(result, WorkerResult):
                    await self.buffer_worker_result(result)
                    drained_count += 1
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"結果キュー排出中にエラー: {e}")
        
        # 最終フラッシュ
        if self.result_buffer:
            await self.flush_result_buffer()
        
        return drained_count

    def get_buffer_statistics(self) -> Dict[str, Any]:
        """バッファ統計を取得"""
        with self.buffer_lock:
            buffer_size = len(self.result_buffer)
        
        with self.overflow_lock:
            overflow_size = len(self.overflow_buffer)
        
        return {
            "buffer_size": buffer_size,
            "overflow_buffer_size": overflow_size,
            "last_flush_time": self.last_buffer_flush,
            "time_since_last_flush": time.time() - self.last_buffer_flush
        }