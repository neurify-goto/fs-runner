#!/usr/bin/env python3
"""
Detail Worker (GitHub Actions版)

企業詳細ページから企業情報を収集する高度なワーカー。
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import aiohttp
import requests
from bs4 import BeautifulSoup

# fetch_detail モジュールをインポートするためのパス追加
sys.path.append(str(Path(__file__).parent / 'fetch_detail'))

from browser_manager import BrowserManager
from detail_extractor import DetailExtractor
from config.manager import get_retry_config_for

# ロギング設定（サニタイズ付き）
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
try:
    from form_sender.security.log_sanitizer import setup_sanitized_logging
    setup_sanitized_logging()  # ルートロガーに適用
except Exception:
    pass
logger = logging.getLogger(__name__)


class TimeoutManager:
    """GitHub Actions タイムアウト管理クラス"""
    
    def __init__(self, max_execution_minutes: int = 55):
        """
        Args:
            max_execution_minutes: 最大実行時間（分）デフォルト55分
        """
        self.start_time = time.time()
        self.max_execution_seconds = max_execution_minutes * 60
        self.warning_threshold = max_execution_minutes - 5  # 5分前に警告
        self.force_stop_threshold = max_execution_minutes - 3  # 3分前に強制停止
        
        self.warning_issued = False
        self.force_stop_requested = False
        self.should_save_and_exit = False
        
        logger.info(f"TimeoutManager初期化: 最大実行時間={max_execution_minutes}分")
    
    def get_elapsed_time(self) -> float:
        """経過時間を秒単位で取得"""
        return time.time() - self.start_time
    
    def get_remaining_time(self) -> float:
        """残り時間を秒単位で取得"""
        return max(0, self.max_execution_seconds - self.get_elapsed_time())
    
    def get_elapsed_minutes(self) -> float:
        """経過時間を分単位で取得"""
        return self.get_elapsed_time() / 60
    
    def get_remaining_minutes(self) -> float:
        """残り時間を分単位で取得"""
        return self.get_remaining_time() / 60
    
    def check_timeout_status(self) -> tuple[bool, str]:
        """タイムアウト状況をチェック
        
        Returns:
            (継続可否, メッセージ)
        """
        elapsed_minutes = self.get_elapsed_minutes()
        remaining_minutes = self.get_remaining_minutes()
        
        # 強制停止フェーズ
        if elapsed_minutes >= self.force_stop_threshold:
            if not self.force_stop_requested:
                self.force_stop_requested = True
                self.should_save_and_exit = True
                message = f"強制停止フェーズに到達（経過時間: {elapsed_minutes:.1f}分）。処理を中断して結果を保存します。"
                logger.error(message)
                return False, message
            return False, "強制停止フェーズ継続中"
        
        # 警告フェーズ
        elif elapsed_minutes >= self.warning_threshold:
            if not self.warning_issued:
                self.warning_issued = True
                message = f"タイムアウト警告（経過時間: {elapsed_minutes:.1f}分、残り時間: {remaining_minutes:.1f}分）。新規タスクの開始を停止します。"
                logger.warning(message)
            return True, f"警告フェーズ（残り時間: {remaining_minutes:.1f}分）"
        
        # 通常フェーズ
        else:
            return True, f"正常実行中（残り時間: {remaining_minutes:.1f}分）"
    
    def should_continue_new_task(self) -> bool:
        """新しいタスクを開始すべきかどうかを判定"""
        can_continue, _ = self.check_timeout_status()
        return can_continue and not self.warning_issued
    
    def should_force_exit(self) -> bool:
        """強制終了すべきかどうかを判定"""
        return self.should_save_and_exit
    
    def get_timeout_summary(self) -> Dict[str, Any]:
        """タイムアウト情報のサマリを取得"""
        return {
            'timeout_triggered': self.force_stop_requested or self.should_save_and_exit,
            'warning_issued': self.warning_issued,
            'elapsed_minutes': round(self.get_elapsed_minutes(), 2),
            'remaining_minutes': round(self.get_remaining_minutes(), 2),
            'max_execution_minutes': self.max_execution_seconds / 60,
            'termination_reason': 'GitHub Actions timeout prevention' if self.should_save_and_exit else None
        }


class DetailWorker:
    """企業詳細情報収集ワーカー（GitHub Actions版・完全版）"""
    
    def __init__(self):
        self.browser_manager = BrowserManager()
        self.detail_extractor = DetailExtractor()
        self.session = None
        self.results = []
        self.errors = []
        
        # タイムアウト管理
        self.timeout_manager = TimeoutManager(max_execution_minutes=55)
        self.shutdown_requested = False
        
        # 連続エラー監視用
        self.consecutive_failures = 0
        self.total_fatal_errors = 0
        self.error_patterns = {}
        
        # 早期終了設定（標準化エラーハンドリング）
        from form_sender.utils.error_handler import load_config_safe
        from form_sender.utils.config_loader import get_performance_monitoring_config
        
        # デフォルト設定
        default_config = type('DefaultConfig', (), {
            'max_consecutive_failures': 5,
            'max_fatal_error_ratio': 0.8,
            'retry_delay_seconds': 2.0,
            'max_retries': 3
        })()
        
        def load_resilience_config():
            perf_config = get_performance_monitoring_config()
            return perf_config.worker_resilience.fetch_detail
        
        resilience_config = load_config_safe(
            loader_func=load_resilience_config,
            fallback_value=default_config,
            config_name="worker_resilience.fetch_detail",
            critical=False
        )
        
        self.max_consecutive_failures = resilience_config.max_consecutive_failures
        self.max_fatal_error_ratio = resilience_config.max_fatal_error_ratio
        self.retry_delay_seconds = resilience_config.retry_delay_seconds
        self.max_retries = resilience_config.max_retries
        
        logger.info(f"Worker resilience config: max_failures={self.max_consecutive_failures}, "
                   f"fatal_ratio={self.max_fatal_error_ratio}, retry_delay={self.retry_delay_seconds}s")
        
        # シグナルハンドラ設定
        self._setup_signal_handlers()
    
    def is_fatal_error_pattern(self, error: Exception) -> bool:
        """致命的なエラーパターンかどうかを判定"""
        error_str = str(error).lower()
        
        # 致命的エラーパターンの定義
        fatal_patterns = [
            'timeout',  # タイムアウト系
            'net::err_connection_refused',  # 接続拒否
            'net::err_name_not_resolved',  # DNS解決失敗
            'net::err_internet_disconnected',  # インターネット接続なし
            'browser has been closed',  # ブラウザクローズ
            'context has been closed',  # コンテキストクローズ
        ]
        
        return any(pattern in error_str for pattern in fatal_patterns)
    
    def update_error_statistics(self, error: Exception, is_success: bool):
        """エラー統計を更新"""
        if is_success:
            self.consecutive_failures = 0
            return
        
        # 連続失敗カウンタを増加
        self.consecutive_failures += 1
        
        # エラーが存在する場合のみエラーパターンを記録
        if error:
            # エラーパターンを記録
            error_type = type(error).__name__
            error_message = str(error)
            
            if error_type not in self.error_patterns:
                self.error_patterns[error_type] = []
            self.error_patterns[error_type].append(error_message)
            
            # 致命的エラーカウント
            if self.is_fatal_error_pattern(error):
                self.total_fatal_errors += 1
                logger.warning(f"致命的エラーパターン検出: {error_type} - {error_message}")
        else:
            # エラー情報が無い場合（一般的な失敗）
            logger.debug("エラー詳細なしでの失敗を記録")
    
    def _setup_signal_handlers(self):
        """シグナルハンドラを設定"""
        def signal_handler(signum, frame):
            logger.warning(f"シグナル {signum} を受信しました。グレースフルシャットダウンを開始します。")
            self.shutdown_requested = True
            self.timeout_manager.should_save_and_exit = True
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        logger.info("シグナルハンドラ設定完了")
    
    def should_terminate_batch(self, processed_count: int) -> tuple[bool, str]:
        """バッチ処理を早期終了すべきかどうかを判定"""
        # シグナルによる終了要求
        if self.shutdown_requested:
            return True, "シグナルによる終了要求"
        
        # タイムアウトによる強制終了
        if self.timeout_manager.should_force_exit():
            return True, "GitHub Actions タイムアウト防止のための強制終了"
        
        # 連続失敗による早期終了
        if self.consecutive_failures >= self.max_consecutive_failures:
            reason = f"連続失敗数が上限({self.max_consecutive_failures})に達したため処理を終了"
            return True, reason
        
        # 致命的エラー比率による早期終了（5件以上処理済みの場合のみ）
        if processed_count >= 5:
            error_ratio = self.total_fatal_errors / processed_count
            if error_ratio >= self.max_fatal_error_ratio:
                reason = f"致命的エラー比率({error_ratio:.2f})が上限({self.max_fatal_error_ratio})を超えたため処理を終了"
                return True, reason
        
        return False, ""
    
    async def __aenter__(self):
        """非同期コンテキストマネージャー開始"""
        await self.browser_manager.__aenter__()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """非同期コンテキストマネージャー終了"""
        await self.browser_manager.__aexit__(exc_type, exc_val, exc_tb)
    
    def get_github_event_data(self) -> Dict[str, Any]:
        """GitHub Actions イベントデータを取得"""
        event_path = os.environ.get('GITHUB_EVENT_PATH')
        if not event_path or not os.path.exists(event_path):
            raise ValueError("GitHub Actions イベントファイルが見つかりません")
        
        with open(event_path, 'r', encoding='utf-8') as f:
            event_data = json.load(f)
        
        client_payload = event_data.get('client_payload', {})
        if not client_payload:
            raise ValueError("client_payload が見つかりません")
        
        return client_payload
    
    async def fetch_company_detail(self, record_id_unused, detail_url: str, company_name: str = "") -> Dict[str, Any]:
        """企業詳細情報を取得（リファクタリング版）"""
        start_time = time.time()
        browser_restart_attempted = False
        
        # リトライ設定を設定ファイルから取得
        try:
            retry_config = get_retry_config_for("network_operations")
            max_retries = retry_config["max_retries"]
            base_delay = retry_config["base_delay"]
        except Exception as e:
            logger.warning(f"リトライ設定の読み込みに失敗、デフォルト値を使用: {e}")
            max_retries = 3
            base_delay = 1.0
        
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.info(f"企業詳細収集リトライ {attempt}/{max_retries} - {delay}秒待機後")
                    await asyncio.sleep(delay)
                    
                    if attempt >= 1 and not browser_restart_attempted:  # 2回目のエラーで再起動
                        logger.info("深刻なエラーのためブラウザを再起動")
                        await self.browser_manager.restart_browser()
                        browser_restart_attempted = True
                    else:
                        await self.browser_manager.reset_page_state()
                else:
                    logger.info("企業詳細収集開始")
                
                # ページが初期化されていなければ作成
                if not self.browser_manager.page:
                    await self.browser_manager.create_clean_page()
                
                # 段階的タイムアウト設定
                timeout_settings = [10000, 15000, 30000]  # 初回10秒、リトライ時15秒、最終30秒
                current_timeout = timeout_settings[min(attempt, len(timeout_settings) - 1)]
                
                logger.info(f"ページアクセス開始 (試行{attempt + 1}, タイムアウト: {current_timeout}ms)")
                await self.browser_manager.page.goto(detail_url, wait_until='networkidle', timeout=current_timeout)
                html = await self.browser_manager.page.content()
                
                # 基本データ構造を初期化
                company_data = {
                    'company_name': company_name,
                    'detail_url': detail_url,
                    'company_url': None,
                    'representative': None,
                    'capital': None,
                    'employee_count': None,
                    'postal_code': None,
                    'tel': None,
                    'established_year': None,
                    'established_month': None,
                    'closing_month': None,
                    'average_age': None,
                    'average_salary': None,
                    'national_id': None,
                    'status': 'success',
                    'execution_time': 0,
                    'timestamp': datetime.now().isoformat()
                }
                
                # 詳細情報抽出
                extracted_detail_data = self.detail_extractor.extract_company_details(html, detail_url)
                
                # 抽出したデータを統合
                for key, value in extracted_detail_data.items():
                    if value is not None:
                        company_data[key] = value
                
                # 実行時間の更新
                company_data['execution_time'] = time.time() - start_time
                
                # 結果の要約
                filled_fields = [k for k, v in company_data.items() 
                               if v is not None and k not in ['national_id', 'company_name', 'detail_url', 'status', 'execution_time', 'timestamp']]
                
                logger.info(f"企業詳細収集完了 - {len(filled_fields)}フィールド取得済み")
                logger.info(f"取得フィールド: {filled_fields}")
                
                return company_data
                
            except Exception as e:
                # リトライ可能エラーかどうかを判定
                if attempt < max_retries and self.browser_manager.is_retryable_error(e):
                    logger.warning(f"企業詳細収集エラー（リトライ {attempt+1}/{max_retries}）: {e}")
                    continue
                else:
                    if attempt >= max_retries:
                        logger.error(f"企業詳細収集失敗（最大リトライ回数到達）: {e}")
                    else:
                        logger.error(f"企業詳細収集失敗（リトライ不可エラー）: {e}")
                    
                    error_data = {
                        'company_name': company_name,
                        'detail_url': detail_url,
                        'national_id': None,
                        'status': 'failed',
                        'error': str(e),
                        'execution_time': time.time() - start_time,
                        'timestamp': datetime.now().isoformat()
                    }
                    return error_data
    
    
    async def save_intermediate_results(self, results: List[Dict[str, Any]], batch_data: List[Dict[str, Any]]) -> bool:
        """中間結果を即座に保存する"""
        try:
            if not results:
                logger.warning("保存する中間結果がありません")
                return False
            
            logger.info(f"中間結果保存開始: {len(results)}件")
            
            # タイムアウト情報を含む結果データを作成
            timeout_summary = self.timeout_manager.get_timeout_summary()
            
            # バッチ全体のrecord_idリストを作成
            all_batch_record_ids = [task.get('record_id') for task in batch_data]
            all_batch_record_ids = [rid for rid in all_batch_record_ids if isinstance(rid, int)]
            
            # 統計情報計算
            successful = [r for r in results if r['status'] == 'success']
            failed = [r for r in results if r['status'] == 'failed']
            
            intermediate_summary = {
                'total_processed': len(results),
                'total_successful': len(successful),
                'total_failed': len(failed),
                'results': results,
                'execution_time': sum(r.get('execution_time', 0) for r in results),
                'timestamp': datetime.now().isoformat(),
                # タイムアウト関連情報
                'timeout_triggered': timeout_summary['timeout_triggered'],
                'terminated_early': True,
                'termination_reason': timeout_summary.get('termination_reason', 'timeout prevention'),
                'remaining_tasks': len(batch_data) - len(results),
                'timeout_info': timeout_summary,
                # エラー統計情報
                'consecutive_failures': self.consecutive_failures,
                'total_fatal_errors': self.total_fatal_errors,
                'error_patterns': {k: len(v) for k, v in self.error_patterns.items()},
                'fatal_error_ratio': self.total_fatal_errors / len(results) if results else 0,
                # バッチ情報（Supabaseクリーンアップ用）
                'all_batch_record_ids': all_batch_record_ids,
                'early_termination_cleanup_needed': True
            }
            
            # artifactsディレクトリ作成
            artifacts_dir = Path("artifacts")
            artifacts_dir.mkdir(exist_ok=True)
            
            # 結果を保存
            results_file = artifacts_dir / "processing_results.json"
            with open(results_file, 'w', encoding='utf-8') as f:
                json.dump(intermediate_summary, f, ensure_ascii=False, indent=2)
            
            logger.info(f"中間結果保存完了: {results_file}")
            logger.info(f"保存内容: 成功={len(successful)}, 失敗={len(failed)}, タイムアウト={timeout_summary['timeout_triggered']}")
            
            return True
            
        except Exception as e:
            logger.error(f"中間結果保存エラー: {e}")
            return False
    
    async def process_batch(self, batch_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """バッチ処理実行（リファクタリング版）"""
        logger.info(f"バッチ処理開始: {len(batch_data)}件")
        logger.info(f"タイムアウト管理: 最大実行時間={self.timeout_manager.max_execution_seconds/60:.1f}分")
        
        # バッチ開始時にブラウザを初期化
        try:
            await self.browser_manager._initialize_browser()
            logger.info("バッチ用ブラウザ初期化完了")
        except Exception as e:
            logger.error(f"ブラウザ初期化エラー: {e}")
            # ブラウザ初期化に失敗した場合は全て失敗として扱う
            failed_results = []
            for task in batch_data:
                error_result = {
                    'record_id': task.get('record_id'),
                    'company_name': task.get('company_name', ''),
                    'detail_url': task.get('detail_page', ''),
                    'status': 'failed',
                    'error': f'ブラウザ初期化エラー: {e}',
                    'execution_time': 0,
                    'timestamp': datetime.now().isoformat()
                }
                failed_results.append(error_result)
            
            return {
                'total_processed': len(batch_data),
                'total_successful': 0,
                'total_failed': len(batch_data),
                'results': failed_results,
                'execution_time': 0,
                'timestamp': datetime.now().isoformat()
            }
        
        results = []
        terminated_early = False
        termination_reason = ""
        
        for i, task in enumerate(batch_data):
            record_id = task.get('record_id')
            logger.info(f"企業処理 {i+1}/{len(batch_data)} - record_id: {record_id}")
            
            # タイムアウト状況チェック
            can_continue, timeout_message = self.timeout_manager.check_timeout_status()
            if i % 10 == 0:  # 10件ごとにタイムアウト情報をログ出力
                logger.info(f"タイムアウト状況: {timeout_message}")
            
            # 新しいタスクの開始可否判定
            if not self.timeout_manager.should_continue_new_task():
                logger.warning(f"タイムアウト警告により新規タスク開始を停止。残り{len(batch_data) - i}件をスキップします。")
                terminated_early = True
                termination_reason = "GitHub Actions タイムアウト防止のため"
                break
            
            detail_url = task['detail_page']
            company_name = task.get('company_name', '')
            is_success = False
            processing_error = None
            
            try:
                # 各企業処理前にクリーンなページを作成
                await self.browser_manager.create_clean_page()
                
                # 企業詳細情報を取得
                result = await self.fetch_company_detail(None, detail_url, company_name)
                # record_idを結果に含める（データベース更新用）
                result['record_id'] = record_id
                
                # 成功判定
                is_success = result.get('status') == 'success'
                if not is_success:
                    processing_error = Exception(result.get('error', 'Unknown error'))
                
                results.append(result)
                
                # 各企業処理後にページをクリーンアップ
                await self.browser_manager.cleanup_page()
                
                logger.info(f"企業処理完了 {i+1}/{len(batch_data)} - record_id: {record_id} - {result.get('status', 'unknown')}")
                
            except Exception as e:
                logger.error(f"企業処理中エラー {i+1}/{len(batch_data)} - record_id: {record_id}: {e}")
                processing_error = e
                is_success = False
                
                # エラー時のページクリーンアップ
                try:
                    await self.browser_manager.cleanup_page()
                except:
                    pass
                
                # エラー結果を作成
                error_result = {
                    'record_id': record_id,
                    'company_name': company_name,
                    'detail_url': detail_url,
                    'status': 'failed',
                    'error': f'処理エラー: {e}',
                    'execution_time': 0,
                    'timestamp': datetime.now().isoformat()
                }
                results.append(error_result)
            
            # エラー統計を更新
            if processing_error:
                self.update_error_statistics(processing_error, is_success)
            else:
                self.update_error_statistics(None, is_success)
            
            # 早期終了判定
            should_terminate, reason = self.should_terminate_batch(i + 1)
            if should_terminate:
                terminated_early = True
                termination_reason = reason
                logger.warning(f"バッチ処理早期終了: {reason}")
                logger.info(f"処理済み: {i + 1}/{len(batch_data)}, 残り: {len(batch_data) - i - 1}")
                
                # タイムアウトによる終了の場合は中間結果を保存
                if self.timeout_manager.should_force_exit() and results:
                    logger.info("タイムアウト対応として中間結果を保存中...")
                    await self.save_intermediate_results(results, batch_data)
                
                break
        
        # 統計情報計算
        successful = [r for r in results if r['status'] == 'success']
        failed = [r for r in results if r['status'] == 'failed']
        
        # バッチ全体のrecord_idリストを作成
        all_batch_record_ids = [task.get('record_id') for task in batch_data]
        all_batch_record_ids = [rid for rid in all_batch_record_ids if isinstance(rid, int)]
        
        # タイムアウト情報を取得
        timeout_summary = self.timeout_manager.get_timeout_summary()
        
        summary = {
            'total_processed': len(results),
            'total_successful': len(successful),
            'total_failed': len(failed),
            'results': results,
            'execution_time': sum(r['execution_time'] for r in results),
            'timestamp': datetime.now().isoformat(),
            # 早期終了情報
            'terminated_early': terminated_early,
            'termination_reason': termination_reason,
            'remaining_tasks': len(batch_data) - len(results) if terminated_early else 0,
            # タイムアウト関連情報
            'timeout_triggered': timeout_summary['timeout_triggered'],
            'timeout_info': timeout_summary,
            # エラー統計情報
            'consecutive_failures': self.consecutive_failures,
            'total_fatal_errors': self.total_fatal_errors,
            'error_patterns': {k: len(v) for k, v in self.error_patterns.items()},
            'fatal_error_ratio': self.total_fatal_errors / len(results) if results else 0,
            # バッチ情報（Supabaseクリーンアップ用）
            'all_batch_record_ids': all_batch_record_ids,
            'early_termination_cleanup_needed': terminated_early
        }
        
        # ログ出力の改善
        if terminated_early:
            if timeout_summary['timeout_triggered']:
                logger.warning(f"タイムアウト対応による早期終了: 成功={len(successful)}, 失敗={len(failed)}, 経過時間={timeout_summary['elapsed_minutes']:.1f}分")
            else:
                logger.warning(f"バッチ処理早期終了: 成功={len(successful)}, 失敗={len(failed)}, 理由: {termination_reason}")
        else:
            logger.info(f"バッチ処理完了: 成功={len(successful)}, 失敗={len(failed)}")
        
        # タイムアウト情報のログ
        if timeout_summary['warning_issued']:
            logger.info(f"タイムアウト警告発生: 経過時間={timeout_summary['elapsed_minutes']:.1f}分, 残り時間={timeout_summary['remaining_minutes']:.1f}分")
        
        # エラー統計の詳細ログ
        if self.error_patterns:
            logger.info(f"エラーパターン統計: {dict(summary['error_patterns'])}")
            logger.info(f"致命的エラー率: {summary['fatal_error_ratio']:.2%}")
            
        return summary
    
    async def run(self):
        """メイン処理実行"""
        try:
            # GitHub Actions環境情報出力
            logger.info("=== GitHub Actions Worker 開始 ===")
            logger.info(f"GITHUB_EVENT_PATH: {os.getenv('GITHUB_EVENT_PATH', 'NOT_SET')}")
            logger.info(f"Working directory: {os.getcwd()}")
            
            # イベントデータ取得
            event_data = self.get_github_event_data()
            batch_id = event_data.get('batch_id')
            batch_data = event_data.get('batch_data', [])
            
            logger.info(f"タスク数: {len(batch_data)}")
            
            if not batch_data:
                raise ValueError("処理対象タスクが空です")
            
            # artifactsディレクトリ作成
            artifacts_dir = Path("artifacts")
            artifacts_dir.mkdir(exist_ok=True)
            
            # バッチ処理実行
            async with self:
                results = await self.process_batch(batch_data)
            
            # 結果をファイルに保存
            results_file = artifacts_dir / "processing_results.json"
            with open(results_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            
            logger.info(f"処理結果を保存しました: {results_file}")
            # タイムアウト情報の最終ログ
            timeout_summary = self.timeout_manager.get_timeout_summary()
            logger.info(f"実行時間情報: 総経過時間={timeout_summary['elapsed_minutes']:.1f}分, タイムアウト発生={timeout_summary['timeout_triggered']}")
            logger.info("=== GitHub Actions Worker 完了 ===")
            
        except Exception as e:
            logger.error(f"Worker実行エラー: {e}", exc_info=True)
            
            # エラーレポート作成
            artifacts_dir = Path("artifacts")
            artifacts_dir.mkdir(exist_ok=True)
            
            # タイムアウト情報も含めたエラーレポート
            timeout_summary = self.timeout_manager.get_timeout_summary()
            
            error_report = {
                'error': str(e),
                'timestamp': datetime.now().isoformat(),
                'batch_id': locals().get('batch_id', 'unknown'),
                'status': 'failed',
                'timeout_info': timeout_summary
            }
            
            error_file = artifacts_dir / "error_report.json"
            with open(error_file, 'w', encoding='utf-8') as f:
                json.dump(error_report, f, ensure_ascii=False, indent=2)
            
            logger.info(f"エラーレポートを保存しました: {error_file}")
            raise


async def main():
    """メイン実行関数"""
    worker = DetailWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
