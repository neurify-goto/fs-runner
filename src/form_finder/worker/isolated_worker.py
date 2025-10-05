"""
独立型Form Finderワーカープロセス

マルチプロセス環境でフォーム探索処理を実行する独立型ワーカー
"""

import asyncio
import logging
import multiprocessing as mp
import queue
import signal
import time
from datetime import datetime
from typing import Dict, Any, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Browser, Page

# form_finder関連モジュール
from ..form_explorer.form_explorer import FormExplorer
from ..utils import safe_log_info, safe_log_error

# 通信管理
from form_sender.communication.queue_manager import QueueManager, WorkerResult, WorkerTask, ResultStatus, TaskType
from form_sender.security.log_sanitizer import sanitize_for_log

# 設定
from config.manager import get_form_explorer_config
from utils.env import is_github_actions

logger = logging.getLogger(__name__)


class IsolatedFormFinderWorker:
    """独立型フォーム探索ワーカー（プロセス分離版）"""

    def __init__(self, worker_id: int):
        """
        初期化

        Args:
            worker_id: ワーカープロセスID
        """
        self.worker_id = worker_id
        self.is_running = False
        self.should_stop = False

        # Playwright関連（プロセス独立）
        self.playwright = None
        self.browser: Optional[Browser] = None

        # フォーム探索エンジン
        self.form_explorer = FormExplorer()

        # 設定読み込み（非同期対応後は初期化でのみ実行）
        self.max_pages = 10
        self.timeout = 60
        self.min_score = 100
        
        # 設定読み込みフラグ
        self._config_loaded = False

        # 統計情報
        self.stats = {
            "processed": 0,
            "success": 0,
            "failed": 0,
            "errors": 0,
            "start_time": time.time()
        }

        logger.info(f"IsolatedFormFinderWorker {worker_id} initialized")

    async def _load_config_async(self):
        """非同期で設定を読み込み"""
        if self._config_loaded:
            return
            
        try:
            # 設定ファイル読み込みを非同期で実行（I/Oブロッキング回避）
            import asyncio
            loop = asyncio.get_event_loop()
            explorer_config = await loop.run_in_executor(None, get_form_explorer_config)
            
            self.max_pages = explorer_config["max_pages_per_site"]
            self.timeout = explorer_config["site_timeout"] 
            self.min_score = explorer_config["min_link_score"]
            self._config_loaded = True
            
            logger.debug(f"Worker {self.worker_id}: Config loaded - max_pages={self.max_pages}, timeout={self.timeout}, min_score={self.min_score}")
            
        except Exception as e:
            logger.warning(f"Worker {self.worker_id}: 設定読み込み失敗、デフォルト値使用: {e}")
            self._config_loaded = True  # エラーでもフラグを立ててリトライを防ぐ

    async def initialize(self) -> bool:
        """Playwrightブラウザの初期化"""
        try:
            logger.info(f"Worker {self.worker_id}: Initializing Playwright browser for form finding")

            # GitHub Actions環境検知
            github_actions_env = is_github_actions()

            # Playwright初期化
            self.playwright = await async_playwright().start()

            # ブラウザ起動設定
            browser_args = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-extensions',
                '--disable-gpu',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
            ]

            # GitHub Actions環境でのメモリ最適化
            if github_actions_env:
                browser_args.extend([
                    '--memory-pressure-off',
                    '--disable-background-networking',
                    '--disable-default-apps',
                ])

            # ブラウザ起動
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=browser_args
            )

            # フォーム探索エンジン初期化
            await self.form_explorer.initialize()
            
            # 非同期設定読み込み（初期化時に実行）
            await self._load_config_async()

            self.is_running = True
            logger.info(f"Worker {self.worker_id}: Browser and form explorer initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Failed to initialize: {e}")
            await self.cleanup()
            return False

    async def process_company(self, company_data: Dict[str, Any]) -> Dict[str, Any]:
        """単一企業のフォーム探索処理"""
        record_id = company_data.get('record_id')
        company_url = company_data.get('company_url')
        
        result = {
            'record_id': record_id,
            'form_urls': [],
            'form_found': False,
            'status': 'pending',
            'business_status': 'pending',
            'error_message': None,
            'processed_at': datetime.utcnow().isoformat(),
            'exploration_details': {}
        }

        try:
            # 入力値検証
            if not company_url:
                result['status'] = 'failed'
                result['error_message'] = '企業URLが設定されていません'
                safe_log_error(str(record_id), "企業URL未設定")
                return result

            from ..utils import validate_company_url
            if not validate_company_url(company_url):
                result['status'] = 'failed'
                result['error_message'] = '企業URLの形式が不正です'
                safe_log_error(str(record_id), "企業URL形式不正")
                return result

            # 3ステップフォーム探索実行
            safe_log_info(str(record_id), "フォーム探索開始")
            
            form_url, pages_visited, success_step = await self.form_explorer.explore_site_for_forms(
                browser=self.browser,
                company_url=company_url,
                record_id=record_id,
                max_pages=self.max_pages,
                timeout=self.timeout,
                min_score=self.min_score
            )

            # 結果を整形 - 有効なform_urlのみを対象とする
            form_urls = []
            
            # 統一化されたform_url妥当性チェックと詳細ログ
            from ..utils import is_valid_form_url
            url_status = {
                'original_form_url': form_url,
                'url_valid': is_valid_form_url(form_url) if form_url else False,
                'url_type': type(form_url).__name__ if form_url else 'None',
                'url_length': len(str(form_url)) if form_url else 0
            }
            
            if form_url and is_valid_form_url(form_url):
                form_urls = [form_url]
                safe_log_info(str(record_id), f"✅ 有効フォームURL検出: {form_url[:50]}... (len={url_status['url_length']})")
            else:
                if form_url:
                    # 無効URL詳細分析
                    invalid_reason = []
                    if not isinstance(form_url, str):
                        invalid_reason.append(f"type={type(form_url).__name__}")
                    elif not form_url.strip():
                        invalid_reason.append("empty")
                    elif form_url.startswith('about:'):
                        invalid_reason.append("about:")
                    elif not form_url.startswith(('http://', 'https://')):
                        invalid_reason.append("no-http")
                    elif len(form_url) > 2048:
                        invalid_reason.append("too-long")
                    
                    reason_str = ', '.join(invalid_reason) if invalid_reason else 'unknown'
                    safe_log_info(str(record_id), f"❌ 無効フォームURL除外: {repr(form_url)[:30]}... (reason: {reason_str})")
                else:
                    safe_log_info(str(record_id), f"❓ フォームURL未取得 (steps_completed={success_step}, pages_visited={pages_visited})")
            
            # form_foundは有効なform_urlが存在する場合のみtrue
            result['form_urls'] = form_urls
            result['form_found'] = len(form_urls) > 0
            
            # URL取得詳細をlogging用に記録（デバッグモード時）
            if logger.isEnabledFor(logging.DEBUG):
                result['url_acquisition_debug'] = url_status
            result['status'] = 'success'  # 技術的成功
            result['business_status'] = 'success' if len(form_urls) > 0 else 'failed'  # ビジネス成功・失敗
            result['exploration_details'] = {
                'pages_visited': pages_visited,
                'success_step': success_step,
                'exploration_method': '3-step-advanced'
            }

            safe_log_info(str(record_id), f"フォーム探索完了: フォーム発見数={len(form_urls)}個, 訪問ページ={pages_visited}個")

        except Exception as e:
            safe_log_error(str(record_id), f"フォーム探索エラー: {str(e)}")
            result['status'] = 'failed'          # 技術的失敗
            result['business_status'] = 'failed'  # ビジネスも失敗
            result['error_message'] = str(e)

        return result


    async def run_worker_loop(self, task_queue: mp.Queue, result_queue: mp.Queue):
        """ワーカーメインループ"""
        try:
            logger.info(f"Worker {self.worker_id}: Starting worker loop")

            # 初期化完了通知
            ready_result = WorkerResult(
                task_id=f"worker_{self.worker_id}_ready",
                worker_id=self.worker_id,
                record_id=None,
                status=ResultStatus.WORKER_READY,
                additional_data=None
            )
            result_queue.put(ready_result)
            logger.info(f"Worker {self.worker_id}: Ready signal sent")

            # メインループ
            while not self.should_stop:
                try:
                    # タスク受信（最適化されたタイムアウト）
                    try:
                        task_data = task_queue.get(timeout=0.1)  # 100msに短縮して応答性向上
                    except queue.Empty:
                        continue

                    # シャットダウンシグナルチェック
                    if task_data is None:
                        logger.info(f"Worker {self.worker_id}: Shutdown signal received (None)")
                        break
                    
                    # 辞書形式のシャットダウンシグナルもチェック
                    if isinstance(task_data, dict) and task_data.get('task_type') == 'shutdown':
                        logger.info(f"Worker {self.worker_id}: Shutdown signal received (dict)")
                        break

                    # タスクデータ解析
                    logger.debug(f"Worker {self.worker_id}: Raw task data type: {type(task_data)}")
                    
                    if isinstance(task_data, dict):
                        # 辞書形式のタスクデータをWorkerTaskオブジェクトに変換
                        try:
                            task = WorkerTask.from_dict(task_data)
                            company_data = task.company_data
                            task_id = task.task_id
                            logger.info(f"Worker {self.worker_id}: WorkerTask from dict conversion successful")
                            logger.info(f"Worker {self.worker_id}: Task ID: {task_id}")
                            logger.info(f"Worker {self.worker_id}: Task type: {task.task_type}")
                            logger.info(f"Worker {self.worker_id}: Company data type: {type(company_data)}")
                            if isinstance(company_data, dict):
                                logger.info(f"Worker {self.worker_id}: Company data keys: {list(company_data.keys())}")
                            logger.info(f"Worker {self.worker_id}: Client data: {task.client_data}")
                            logger.info(f"Worker {self.worker_id}: Targeting ID: {task.targeting_id}")
                        except Exception as e:
                            logger.error(f"Worker {self.worker_id}: Failed to convert dict to WorkerTask: {e}")
                            # フォールバック：辞書を直接company_dataとして使用
                            company_data = task_data
                            task_id = f"task_{task_data.get('record_id', 'unknown')}"
                            logger.warning(f"Worker {self.worker_id}: Using fallback - treating dict as company_data directly")
                    elif isinstance(task_data, WorkerTask):
                        # WorkerTaskオブジェクト直接受信（念のため）
                        task = task_data
                        company_data = task.company_data
                        task_id = task.task_id
                        logger.info(f"Worker {self.worker_id}: Direct WorkerTask object received")
                    else:
                        # 旧形式対応
                        company_data = task_data
                        task_id = f"task_{task_data.get('record_id', 'unknown')}"
                        logger.warning(f"Worker {self.worker_id}: Unknown task data format, using as company_data directly")

                    logger.info(f"Worker {self.worker_id}: Processing company {company_data.get('record_id')}")
                    
                    # デバッグ: 企業データの内容を確認（安全なログ）
                    if company_data is None:
                        logger.error(f"Worker {self.worker_id}: company_data is None!")
                    elif isinstance(company_data, dict):
                        logger.info(f"Worker {self.worker_id}: Company data keys: {list(company_data.keys())}")
                        logger.info(f"Worker {self.worker_id}: Has record_id: {'record_id' in company_data}")
                        logger.info(f"Worker {self.worker_id}: Has company_url: {'company_url' in company_data}")
                        logger.info(f"Worker {self.worker_id}: Record ID value: {company_data.get('record_id', 'MISSING')}")
                        logger.info(f"Worker {self.worker_id}: Company URL length: {len(str(company_data.get('company_url', '')))}")
                        if not company_data.get('company_url'):
                            logger.error(f"Worker {self.worker_id}: company_url is empty or None: {repr(company_data.get('company_url'))}")
                        else:
                            logger.info(f"Worker {self.worker_id}: Company URL starts with http: {str(company_data.get('company_url', '')).startswith(('http://', 'https://'))}")
                    else:
                        logger.error(f"Worker {self.worker_id}: company_data is not dict: {type(company_data)} = {repr(company_data)}")

                    # フォーム探索処理実行
                    start_time = time.time()
                    processing_result = await self.process_company(company_data)
                    processing_time = time.time() - start_time

                    # 成功判定を厳格化: form_urlsが空または無効な場合は失敗扱い
                    form_urls = processing_result.get('form_urls', [])
                    has_valid_form_urls = False
                    
                    if processing_result['status'] == 'success' and form_urls:
                        # form_urlsの各URLを検証
                        from ..utils import is_valid_form_url
                        valid_urls = []
                        for url in form_urls:
                            if url and is_valid_form_url(str(url).strip()):
                                valid_urls.append(url)
                        
                        if valid_urls:
                            has_valid_form_urls = True
                            form_urls = valid_urls  # 有効なURLのみを保持

                    # 最終的な成功判定
                    if processing_result['status'] == 'success' and has_valid_form_urls:
                        status = ResultStatus.SUCCESS
                        self.stats["success"] += 1
                        logger.debug(f"record_id={processing_result['record_id']}: Worker段階で真の成功として判定")
                    else:
                        status = ResultStatus.FAILED
                        self.stats["failed"] += 1
                        if processing_result['status'] == 'success':
                            logger.warning(f"record_id={processing_result['record_id']}: form_urls無効によりWorker段階で失敗として再分類")

                    # form_finder固有データをadditional_dataに格納
                    additional_data = {
                        'form_urls': form_urls,
                        'form_found': has_valid_form_urls,  # 有効なURLの存在に基づく
                        'exploration_details': processing_result.get('exploration_details', {}),
                        'business_status': 'success' if has_valid_form_urls else 'failed'
                    }

                    worker_result = WorkerResult(
                        task_id=task_id,
                        worker_id=self.worker_id,
                        record_id=processing_result['record_id'],
                        status=status,
                        error_message=processing_result.get('error_message'),
                        processing_time=processing_time,
                        additional_data=additional_data
                    )

                    result_queue.put(worker_result)
                    self.stats["processed"] += 1

                    logger.info(f"Worker {self.worker_id}: Completed company {company_data.get('record_id')} "
                               f"in {processing_time:.2f}s - Status: {status.value}")

                except Exception as e:
                    logger.error(f"Worker {self.worker_id}: Error processing task: {e}")
                    self.stats["errors"] += 1
                    
                    # エラー結果を送信
                    error_result = WorkerResult(
                        task_id=task_id if 'task_id' in locals() else "unknown",
                        worker_id=self.worker_id,
                        record_id=company_data.get('record_id') if 'company_data' in locals() else None,
                        status=ResultStatus.ERROR,
                        error_message=str(e),
                        additional_data=None
                    )
                    result_queue.put(error_result)

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Critical error in worker loop: {e}")
        finally:
            logger.info(f"Worker {self.worker_id}: Worker loop ended")

    async def cleanup(self):
        """リソースクリーンアップ"""
        try:
            logger.info(f"Worker {self.worker_id}: Starting cleanup")

            if self.browser:
                await self.browser.close()
                self.browser = None

            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

            self.is_running = False
            logger.info(f"Worker {self.worker_id}: Cleanup completed")

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Error during cleanup: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """統計情報を取得"""
        elapsed_time = time.time() - self.stats["start_time"]
        return {
            **self.stats,
            "elapsed_time": elapsed_time,
            "processing_rate": self.stats["processed"] / max(elapsed_time, 1),
        }


def worker_process_main(worker_id: int, task_queue: mp.Queue, result_queue: mp.Queue):
    """ワーカープロセスのメインエントリーポイント"""
    # プロセス名設定（monitoring用）
    try:
        import setproctitle
        setproctitle.setproctitle(f"form-finder-worker-{worker_id}")
    except ImportError:
        pass

    # ログ設定
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s - Worker-{worker_id} - %(name)s - %(levelname)s - %(message)s'
    )
    
    logger.info(f"Starting form finder worker process {worker_id}")

    # シグナルハンドラー設定
    worker = None
    
    def signal_handler(signum, frame):
        logger.info(f"Worker {worker_id}: Received signal {signum}")
        if worker:
            worker.should_stop = True

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    async def run_worker():
        nonlocal worker
        try:
            # ワーカー初期化
            worker = IsolatedFormFinderWorker(worker_id)
            
            # ブラウザ初期化
            if not await worker.initialize():
                logger.error(f"Worker {worker_id}: Failed to initialize")
                return

            # ワーカーループ実行
            await worker.run_worker_loop(task_queue, result_queue)

        except Exception as e:
            logger.error(f"Worker {worker_id}: Critical error: {e}")
        finally:
            if worker:
                await worker.cleanup()
                stats = worker.get_stats()
                logger.info(f"Worker {worker_id}: Final stats: {stats}")

    try:
        # asyncioループ実行
        asyncio.run(run_worker())
    except Exception as e:
        logger.error(f"Worker {worker_id}: Failed to run asyncio loop: {e}")
    finally:
        logger.info(f"Worker {worker_id}: Process ended")
