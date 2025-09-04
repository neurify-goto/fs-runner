"""
ヘルスモニタリングモジュール

システムヘルス監視、リソース管理、パフォーマンス監視を担当
"""

import asyncio
import gc
import logging
import os
import psutil
import resource
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from ..security.logger import SecurityLogger
from ..utils.secure_logger import get_secure_logger
from ..utils.performance_monitor import PerformanceMonitor

logger = get_secure_logger(__name__)
security_logger = SecurityLogger()


class HealthMonitor:
    """システムヘルス監視クラス"""

    def __init__(self):
        self.performance_monitor = PerformanceMonitor()
        self.start_time = time.time()
        self.processing_stats = {
            "total_processed": 0,
            "successful": 0,
            "failed": 0,
            "skipped": 0,
            "prohibition_detected": 0,
        }
        self.resource_alerts = []
        self.last_gc_time = time.time()

    async def monitor_system_health(self, check_interval: float = 30) -> None:
        """システムヘルスの継続的監視"""
        logger.info(f"システムヘルス監視を開始 (間隔: {check_interval}秒)")
        
        while True:
            try:
                await asyncio.sleep(check_interval)
                
                # リソース使用状況チェック
                resource_status = await self.check_resource_usage()
                
                # メモリ警告
                if resource_status["memory_percent"] > 80:
                    await self.handle_high_memory_usage(resource_status["memory_percent"])
                
                # CPU警告
                if resource_status["cpu_percent"] > 90:
                    logger.warning(f"高CPU使用率: {resource_status['cpu_percent']:.1f}%")
                
                # ファイルディスクリプタリーク検出
                if resource_status["open_files"] > 500:
                    await self.detect_fd_leaks(resource_status["open_files"])
                
                # パフォーマンスメトリクス記録
                self.performance_monitor.record_metric("memory_usage", resource_status["memory_percent"])
                self.performance_monitor.record_metric("cpu_usage", resource_status["cpu_percent"])
                
            except asyncio.CancelledError:
                logger.info("システムヘルス監視を終了します")
                break
            except Exception as e:
                logger.error(f"ヘルス監視中にエラー: {e}")

    async def check_resource_usage(self) -> Dict[str, Any]:
        """リソース使用状況をチェック"""
        try:
            process = psutil.Process()
            
            # メモリ使用量
            memory_info = process.memory_info()
            memory_percent = process.memory_percent()
            
            # CPU使用率（非ブロッキング測定）
            cpu_percent = process.cpu_percent(interval=0)
            
            # ファイルディスクリプタ
            open_files = len(process.open_files())
            
            # ディスク使用量
            disk_usage = psutil.disk_usage("/")
            
            return {
                "memory_mb": memory_info.rss / 1024 / 1024,
                "memory_percent": memory_percent,
                "cpu_percent": cpu_percent,
                "open_files": open_files,
                "disk_free_gb": disk_usage.free / 1024 / 1024 / 1024,
                "disk_percent": disk_usage.percent,
                "timestamp": time.time()
            }
            
        except Exception as e:
            logger.error(f"リソース使用状況チェック中にエラー: {e}")
            return {
                "memory_mb": 0,
                "memory_percent": 0,
                "cpu_percent": 0,
                "open_files": 0,
                "disk_free_gb": 0,
                "disk_percent": 0,
                "timestamp": time.time()
            }

    async def handle_high_memory_usage(self, memory_percent: float) -> None:
        """高メモリ使用時の処理"""
        logger.warning(f"高メモリ使用率: {memory_percent:.1f}%")
        
        # ガベージコレクションの強制実行
        await self.force_gc_collection()
        
        # メモリ使用量の詳細ログ
        try:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start()
            
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics("lineno")
            
            logger.info("メモリ使用量トップ5:")
            for stat in top_stats[:5]:
                logger.info(f"  {stat}")
                
        except Exception as e:
            logger.debug(f"メモリ詳細取得エラー: {e}")

    async def force_gc_collection(self) -> None:
        """ガベージコレクションを強制実行"""
        current_time = time.time()
        
        # 最後のGCから一定時間経過していれば実行
        if current_time - self.last_gc_time > 60:
            logger.info("ガベージコレクションを強制実行")
            
            # 実行前のメモリ使用量
            before = psutil.Process().memory_info().rss / 1024 / 1024
            
            # GC実行
            collected = gc.collect()
            
            # 実行後のメモリ使用量
            after = psutil.Process().memory_info().rss / 1024 / 1024
            
            freed = before - after
            logger.info(
                f"GC完了: {collected} オブジェクト回収, "
                f"{freed:.1f} MB解放 ({before:.1f} MB → {after:.1f} MB)"
            )
            
            self.last_gc_time = current_time

    async def detect_fd_leaks(self, current_fds: int) -> None:
        """ファイルディスクリプタリークの検出"""
        logger.warning(f"多数のファイルディスクリプタ使用: {current_fds}")
        
        try:
            process = psutil.Process()
            open_files = process.open_files()
            
            # ファイルタイプ別に集計
            file_types = {}
            for file in open_files:
                ext = os.path.splitext(file.path)[1] if file.path else "unknown"
                file_types[ext] = file_types.get(ext, 0) + 1
            
            logger.info(f"開いているファイルの種類: {file_types}")
            
            # リーク警告
            if current_fds > 1000:
                logger.critical(f"深刻なファイルディスクリプタリーク: {current_fds}")
                await self.emergency_resource_cleanup()
                
        except Exception as e:
            logger.error(f"FDリーク検出中にエラー: {e}")

    async def emergency_resource_cleanup(self) -> None:
        """緊急リソースクリーンアップ"""
        logger.warning("緊急リソースクリーンアップを実行")
        
        try:
            # 強制GC
            await self.force_gc_collection()
            
            # キャッシュクリア
            self._clear_caches()
            
            # 一時ファイルの削除
            self._cleanup_temp_files()
            
            logger.info("緊急クリーンアップ完了")
            
        except Exception as e:
            logger.error(f"緊急クリーンアップ中にエラー: {e}")

    def _clear_caches(self) -> None:
        """各種キャッシュのクリア"""
        try:
            # Pythonの内部キャッシュクリア
            if hasattr(gc, "clear_cache"):
                gc.clear_cache()
            
            # 正規表現キャッシュクリア
            import re
            if hasattr(re, "purge"):
                re.purge()
                
        except Exception as e:
            logger.debug(f"キャッシュクリア中にエラー: {e}")

    def _cleanup_temp_files(self) -> None:
        """一時ファイルのクリーンアップ"""
        try:
            import tempfile
            temp_dir = tempfile.gettempdir()
            
            # 古い一時ファイルを削除
            current_time = time.time()
            for filename in os.listdir(temp_dir):
                if filename.startswith("failed_result_") and filename.endswith(".pkl"):
                    filepath = os.path.join(temp_dir, filename)
                    try:
                        file_age = current_time - os.path.getmtime(filepath)
                        if file_age > 3600:  # 1時間以上古いファイル
                            os.remove(filepath)
                            logger.debug(f"古い一時ファイルを削除: {filename}")
                    except Exception:
                        pass
                        
        except Exception as e:
            logger.debug(f"一時ファイルクリーンアップ中にエラー: {e}")

    def update_processing_stats(self, status: str) -> None:
        """処理統計を更新"""
        self.processing_stats["total_processed"] += 1
        
        if status == "SUCCESS":
            self.processing_stats["successful"] += 1
        elif status == "ERROR":
            self.processing_stats["failed"] += 1
        elif status == "SKIPPED":
            self.processing_stats["skipped"] += 1
        elif status == "PROHIBITION_DETECTED":
            self.processing_stats["prohibition_detected"] += 1

    def get_processing_summary(self) -> Dict[str, Any]:
        """処理サマリーを取得"""
        current_time = time.time()
        elapsed_time = current_time - self.start_time
        
        # 処理速度の計算
        processing_rate = (
            self.processing_stats["total_processed"] / elapsed_time * 3600
            if elapsed_time > 0 else 0
        )
        
        # 成功率の計算
        success_rate = (
            self.processing_stats["successful"] / self.processing_stats["total_processed"] * 100
            if self.processing_stats["total_processed"] > 0 else 0
        )
        
        return {
            "start_time": datetime.fromtimestamp(
                self.start_time, tz=timezone(timedelta(hours=9))
            ).isoformat(),
            "elapsed_seconds": elapsed_time,
            "total_processed": self.processing_stats["total_processed"],
            "successful": self.processing_stats["successful"],
            "failed": self.processing_stats["failed"],
            "skipped": self.processing_stats["skipped"],
            "prohibition_detected": self.processing_stats["prohibition_detected"],
            "processing_rate_per_hour": round(processing_rate, 2),
            "success_rate_percent": round(success_rate, 2),
        }

    def get_system_metrics(self) -> Dict[str, Any]:
        """システムメトリクスを取得"""
        try:
            process = psutil.Process()
            
            return {
                "cpu_percent": process.cpu_percent(interval=0),
                "memory_mb": process.memory_info().rss / 1024 / 1024,
                "memory_percent": process.memory_percent(),
                "num_threads": process.num_threads(),
                "num_fds": len(process.open_files()),
                "io_counters": {
                    "read_bytes": process.io_counters().read_bytes,
                    "write_bytes": process.io_counters().write_bytes,
                } if hasattr(process, "io_counters") else {},
            }
        except Exception as e:
            logger.error(f"システムメトリクス取得エラー: {e}")
            return {}

    def log_performance_summary(self) -> None:
        """パフォーマンスサマリーをログ出力"""
        summary = self.get_processing_summary()
        metrics = self.get_system_metrics()
        
        logger.info("=== パフォーマンスサマリー ===")
        logger.info(f"処理件数: {summary['total_processed']} 件")
        logger.info(f"成功: {summary['successful']} 件 ({summary['success_rate_percent']:.1f}%)")
        logger.info(f"失敗: {summary['failed']} 件")
        logger.info(f"スキップ: {summary['skipped']} 件")
        logger.info(f"営業禁止検出: {summary['prohibition_detected']} 件")
        logger.info(f"処理速度: {summary['processing_rate_per_hour']:.1f} 件/時")
        logger.info(f"CPU使用率: {metrics.get('cpu_percent', 0):.1f}%")
        logger.info(f"メモリ使用量: {metrics.get('memory_mb', 0):.1f} MB")
        logger.info("============================")