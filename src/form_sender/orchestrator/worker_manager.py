"""
ワーカー管理モジュール

ワーカープロセスの起動、監視、終了を管理
"""

import asyncio
import logging
import multiprocessing as mp
import os
import psutil
import signal
import threading
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta

from ..security.logger import SecurityLogger
from ..utils.secure_logger import get_secure_logger

logger = get_secure_logger(__name__)
security_logger = SecurityLogger()


class WorkerManager:
    """ワーカープロセス管理クラス"""

    def __init__(self, num_workers: int = 2):
        self.num_workers = num_workers
        self.workers = {}
        self.worker_processes = {}
        self.worker_status = {}
        self.worker_start_times = {}
        self.status_lock = threading.Lock()
        self.recovery_manager = None

    async def start_workers(self, task_queue, result_queue, config_file_path: str, headless: bool = True) -> bool:
        """ワーカープロセスを起動"""
        try:
            logger.info(f"ワーカー {self.num_workers} 個の起動を開始")
            
            for worker_id in range(self.num_workers):
                success = await self._start_single_worker(
                    worker_id, task_queue, result_queue, config_file_path, headless
                )
                if not success:
                    logger.error(f"ワーカー {worker_id} の起動に失敗")
                    return False
                
                await asyncio.sleep(1)  # ワーカー起動間隔
            
            logger.info(f"全 {self.num_workers} ワーカーの起動完了")
            return True
            
        except Exception as e:
            logger.error(f"ワーカー起動中にエラー: {e}")
            return False

    async def _start_single_worker(
        self, worker_id: int, task_queue, result_queue, 
        config_file_path: str, headless: bool
    ) -> bool:
        """単一ワーカーを起動"""
        try:
            from ..worker.isolated_worker import worker_process_main
            
            process = mp.Process(
                target=worker_process_main,
                args=(worker_id, task_queue, result_queue, headless)
            )
            
            process.start()
            self.workers[worker_id] = process
            self.worker_processes[worker_id] = process
            self.worker_status[worker_id] = "RUNNING"
            self.worker_start_times[worker_id] = time.time()
            
            logger.info(f"ワーカー {worker_id} を起動しました (PID: {process.pid})")
            return True
            
        except Exception as e:
            logger.error(f"ワーカー {worker_id} の起動に失敗: {e}")
            return False

    def update_worker_status_atomic(self, worker_id: int, new_status: str) -> None:
        """ワーカーステータスを原子的に更新"""
        with self.status_lock:
            old_status = self.worker_status.get(worker_id, "UNKNOWN")
            self.worker_status[worker_id] = new_status
            logger.debug(f"ワーカー {worker_id} のステータス更新: {old_status} → {new_status}")

    def check_worker_health(self) -> Dict[int, str]:
        """全ワーカーのヘルスチェック"""
        health_status = {}
        
        with self.status_lock:
            for worker_id, process in self.worker_processes.items():
                if process and process.is_alive():
                    health_status[worker_id] = "HEALTHY"
                else:
                    health_status[worker_id] = "DEAD"
                    self.worker_status[worker_id] = "DEAD"
        
        return health_status

    def safe_check_alive_workers(self) -> List[int]:
        """生存しているワーカーのIDリストを安全に取得"""
        alive = []
        with self.status_lock:
            for worker_id, process in self.worker_processes.items():
                try:
                    if process and process.is_alive():
                        alive.append(worker_id)
                except Exception as e:
                    logger.debug(f"ワーカー {worker_id} の生存確認中にエラー: {e}")
        return alive

    async def restart_single_worker(
        self, worker_id: int, task_queue, result_queue, 
        config_file_path: str, headless: bool
    ) -> bool:
        """単一ワーカーを再起動"""
        try:
            logger.info(f"ワーカー {worker_id} の再起動を開始")
            
            # 既存のワーカーを終了
            if worker_id in self.worker_processes:
                old_process = self.worker_processes[worker_id]
                if old_process and old_process.is_alive():
                    old_process.terminate()
                    old_process.join(timeout=5)
                    if old_process.is_alive():
                        old_process.kill()
                        old_process.join(timeout=2)
            
            # 新しいワーカーを起動
            await asyncio.sleep(2)  # クリーンアップ待機
            
            return await self._start_single_worker(
                worker_id, task_queue, result_queue, config_file_path, headless
            )
            
        except Exception as e:
            logger.error(f"ワーカー {worker_id} の再起動中にエラー: {e}")
            return False

    async def shutdown_workers(self, timeout: float = 30) -> bool:
        """全ワーカーを安全にシャットダウン"""
        try:
            logger.info("全ワーカーのシャットダウンを開始")
            start_time = time.time()
            
            # 段階的終了: まずSIGTERM
            for worker_id, process in self.worker_processes.items():
                if process and process.is_alive():
                    try:
                        logger.debug(f"ワーカー {worker_id} にSIGTERMを送信")
                        process.terminate()
                    except Exception as e:
                        logger.debug(f"ワーカー {worker_id} の終了中にエラー: {e}")
            
            # タイムアウトまで待機
            while time.time() - start_time < timeout:
                all_terminated = True
                for process in self.worker_processes.values():
                    if process and process.is_alive():
                        all_terminated = False
                        break
                
                if all_terminated:
                    logger.info("全ワーカーが正常に終了しました")
                    return True
                
                await asyncio.sleep(0.5)
            
            # タイムアウト後はSIGKILL
            for worker_id, process in self.worker_processes.items():
                if process and process.is_alive():
                    logger.warning(f"ワーカー {worker_id} を強制終了します")
                    process.kill()
                    process.join(timeout=2)
            
            # プロセステーブルのクリーンアップ
            await self._cleanup_zombie_processes()
            
            return True
            
        except Exception as e:
            logger.error(f"ワーカーシャットダウン中にエラー: {e}")
            return False

    async def _cleanup_zombie_processes(self):
        """ゾンビプロセスのクリーンアップ"""
        try:
            zombie_pids = []
            for process in self.worker_processes.values():
                if process and process.pid:
                    try:
                        ps_process = psutil.Process(process.pid)
                        if ps_process.status() == psutil.STATUS_ZOMBIE:
                            zombie_pids.append(process.pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            
            if zombie_pids:
                logger.warning(f"ゾンビプロセスを検出: {zombie_pids}")
                for pid in zombie_pids:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                        logger.debug(f"ゾンビプロセス {pid} をクリーンアップ")
                    except Exception as e:
                        logger.debug(f"ゾンビプロセス {pid} のクリーンアップ失敗: {e}")
                        
        except Exception as e:
            logger.debug(f"ゾンビプロセスクリーンアップ中にエラー: {e}")

    async def monitor_and_recover_workers(
        self, task_queue, result_queue, config_file_path: str, 
        headless: bool, check_interval: float = 30
    ) -> None:
        """ワーカーの監視と自動復旧"""
        logger.info(f"ワーカー監視を開始 (チェック間隔: {check_interval}秒)")
        
        while True:
            try:
                await asyncio.sleep(check_interval)
                
                # ヘルスチェック
                health_status = self.check_worker_health()
                dead_workers = [
                    worker_id for worker_id, status in health_status.items()
                    if status == "DEAD"
                ]
                
                # 死亡したワーカーの復旧
                if dead_workers:
                    logger.warning(f"死亡したワーカーを検出: {dead_workers}")
                    for worker_id in dead_workers:
                        success = await self.restart_single_worker(
                            worker_id, task_queue, result_queue, config_file_path, headless
                        )
                        if not success:
                            logger.error(f"ワーカー {worker_id} の復旧に失敗")
                
            except asyncio.CancelledError:
                logger.info("ワーカー監視を終了します")
                break
            except Exception as e:
                logger.error(f"ワーカー監視中にエラー: {e}")
                await asyncio.sleep(10)

    def get_worker_statistics(self) -> Dict[str, Any]:
        """ワーカー統計情報を取得"""
        stats = {
            "total_workers": self.num_workers,
            "alive_workers": len(self.safe_check_alive_workers()),
            "worker_status": dict(self.worker_status),
            "worker_uptimes": {}
        }
        
        current_time = time.time()
        for worker_id, start_time in self.worker_start_times.items():
            stats["worker_uptimes"][worker_id] = current_time - start_time
        
        return stats

    async def verify_complete_termination(self) -> bool:
        """プロセスの完全終了を確認"""
        try:
            max_wait = 10
            check_interval = 0.5
            start_time = time.time()
            
            while time.time() - start_time < max_wait:
                all_terminated = True
                
                for worker_id, process in self.worker_processes.items():
                    if process and process.pid:
                        if self._check_process_status(process.pid):
                            all_terminated = False
                            logger.debug(f"ワーカー {worker_id} (PID: {process.pid}) はまだ実行中")
                
                if all_terminated:
                    logger.info("全プロセスの完全終了を確認")
                    return True
                
                await asyncio.sleep(check_interval)
            
            logger.warning(f"{max_wait}秒後もプロセスが残存しています")
            return False
            
        except Exception as e:
            logger.error(f"プロセス終了確認中にエラー: {e}")
            return False

    def _check_process_status(self, pid: int) -> bool:
        """プロセスの状態をチェック"""
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False