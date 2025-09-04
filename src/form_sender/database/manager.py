"""
データベース操作マネージャー

再試行機能付きのデータベース操作を提供
"""

import asyncio
import logging
import time
import random
from supabase import Client

from config.manager import get_database_config

logger = logging.getLogger(__name__)


class DatabaseManager:
    """データベース操作の再試行機能付きマネージャー"""
    
    def __init__(self, supabase_client: Client, max_retries: int = None):
        self.supabase = supabase_client
        
        # 設定ファイルから設定を読み込み
        try:
            db_config = get_database_config()
            self.max_retries = max_retries or db_config["max_retries"]
            self.retry_delay = db_config["retry_delay"]
        except Exception as e:
            logger.warning(f"データベース設定の読み込みに失敗、デフォルト値を使用: {e}")
            self.max_retries = max_retries or 3
            self.retry_delay = 1
    
    def _is_fatal_error(self, error_str: str) -> bool:
        """致命的（リトライ不要）エラー判定"""
        fatal_markers = [
            "authentication failed", "invalid credentials", "permission denied",
            "not found", "does not exist", "malformed request", "syntax error"
        ]
        return any(m in error_str for m in fatal_markers)

    def _should_retry(self, error_str: str) -> bool:
        """リトライ対象エラー判定（429/5xx/ネットワーク系）」"""
        retry_markers = [
            "timeout", "timed out", "temporarily unavailable", "temporary failure",
            "too many requests", "429", "rate limit", "rate-limited",
            "server error", "5xx", "internal server error", "bad gateway", "gateway timeout",
            "connection reset", "connection aborted", "connection refused", "ecoonreset",
            "dns", "name resolution", "ssl", "tls"
        ]
        return any(m in error_str for m in retry_markers)

    def _compute_backoff(self, base_delay: float, attempt: int, max_delay: float = 30.0) -> float:
        """指数バックオフ + ジッター（±20%）"""
        delay = min(base_delay * (2 ** attempt), max_delay)
        jitter = delay * random.uniform(-0.2, 0.2)
        return max(0.05, delay + jitter)

    async def execute_with_retry(self, operation: str, func, *args, **kwargs):
        """再試行機能付きでデータベース操作を実行"""
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                result = func(*args, **kwargs)
                if attempt > 0:
                    logger.info(f"Database operation '{operation}' succeeded on attempt {attempt + 1}")
                return result
                
            except Exception as e:
                last_exception = e
                
                # エラーの種類に応じてリトライの必要性を判断
                error_str = str(e).lower()
                if self._is_fatal_error(error_str):
                    logger.error(f"Fatal error detected for operation '{operation}', skipping retries: {e}")
                    break
                
                # リトライ対象の場合
                if self._should_retry(error_str) or attempt < self.max_retries - 1:
                    wait_time = self._compute_backoff(self.retry_delay, attempt, max_delay=30.0)
                    logger.warning(
                        f"Database operation '{operation}' failed on attempt {attempt + 1}/{self.max_retries}: {e}"
                    )
                    if attempt < self.max_retries - 1:
                        logger.info(f"Retrying in {wait_time:.2f} seconds...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Database operation '{operation}' failed after {self.max_retries} attempts")
                else:
                    break
        
        raise last_exception
    
    def execute_with_retry_sync(self, operation: str, func, *args, **kwargs):
        """同期版：再試行機能付きでデータベース操作を実行"""
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                result = func(*args, **kwargs)
                if attempt > 0:
                    logger.info(f"Database operation '{operation}' succeeded on attempt {attempt + 1}")
                return result
                
            except Exception as e:
                last_exception = e
                
                # エラーの種類に応じてリトライの必要性を判断
                error_str = str(e).lower()
                if self._is_fatal_error(error_str):
                    logger.error(f"Fatal error detected for operation '{operation}', skipping retries: {e}")
                    break
                
                # リトライ対象の場合
                if self._should_retry(error_str) or attempt < self.max_retries - 1:
                    wait_time = self._compute_backoff(self.retry_delay, attempt, max_delay=30.0)
                    logger.warning(
                        f"Database operation '{operation}' failed on attempt {attempt + 1}/{self.max_retries}: {e}"
                    )
                    if attempt < self.max_retries - 1:
                        logger.info(f"Retrying in {wait_time:.2f} seconds...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Database operation '{operation}' failed after {self.max_retries} attempts")
                else:
                    break

        raise last_exception
