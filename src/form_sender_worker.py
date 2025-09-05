#!/usr/bin/env python3
"""
Form Sender Worker (GitHub Actions版 - マルチプロセス対応エントリポイント)

RuleBasedAnalyzerによるリアルタイム解析に基づいて、
Webサイトのお問い合わせフォームへ指定データを入力送信する自動化システム（instruction_json依存は廃止）。

FORM_SENDER.md の仕様に基づく実装（マルチプロセス・アーキテクチャ）：
- マルチワーカープロセスによる並列処理
- オーケストレーターによる統括管理
- 5時間時間制限制御
- 営業時間・送信数制限のリアルタイムチェック
- 各企業処理完了時の即座DB書き込み
"""

import argparse
import asyncio
import json
import logging
import multiprocessing as mp
import queue
import signal
import sys
import threading
import time
import atexit
from pathlib import Path
from typing import Dict, Any, Optional

# マルチプロセス対応コンポーネントをインポート
from form_sender.orchestrator.manager import ConfigurableOrchestrator
from form_sender.utils.validation_config import get_validation_manager, validate_environment_variable
from form_sender.security.log_sanitizer import setup_sanitized_logging

# ロギング設定（サニタイゼーション機能付き + HTTPXログ無効化）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# HTTPXのHTTPリクエストログを無効化（URL情報削除のため）
logging.getLogger("httpx").setLevel(logging.WARNING)

# GitHub Actions環境での追加ログ抑制
import os
if os.getenv('GITHUB_ACTIONS', '').lower() == 'true':
    # CI/CD環境では企業情報保護のため更に厳格なログレベル設定
    logging.getLogger("playwright").setLevel(logging.ERROR)  # Playwrightのナビゲーション情報を完全抑制
    logging.getLogger("urllib3").setLevel(logging.ERROR)     # HTTP通信ログを完全抑制
    logging.getLogger("requests").setLevel(logging.ERROR)    # Requestsライブラリログを完全抑制
    logging.getLogger("supabase").setLevel(logging.WARNING)  # Supabaseクライアントログを抑制
    
    # ルートロガーレベルもGitHub Actions環境では警告以上のみ
    logging.getLogger().setLevel(logging.WARNING)
    
    print("INFO: GitHub Actions environment detected - Enhanced security logging enabled")

# サニタイゼーション機能を有効化
logger = setup_sanitized_logging(__name__)

# ルートロガーにもサニタイゼーション機能を適用
setup_sanitized_logging()

# グローバル停止制御（スレッドセーフ - 改良版）
_shutdown_event = threading.Event()
_orchestrator_instance = None
_orchestrator_lock = threading.Lock()  # オーケストレーター管理用ロック
_cleanup_lock = threading.Lock()
_cleanup_completed = threading.Event()
_signal_handlers_installed = False
_signal_install_lock = threading.Lock()  # シグナルハンドラーインストール用ロック


def get_orchestrator_instance():
    """スレッドセーフなオーケストレーターインスタンス取得"""
    global _orchestrator_instance
    with _orchestrator_lock:
        return _orchestrator_instance


def set_orchestrator_instance(instance):
    """スレッドセーフなオーケストレーターインスタンス設定"""
    global _orchestrator_instance
    with _orchestrator_lock:
        _orchestrator_instance = instance
        logger.info(f"Orchestrator instance set safely: {type(instance).__name__}")


async def main():
    """メイン処理（マルチプロセス・アーキテクチャ版）"""
    # 変数スコープ対応: finally ブロックで参照される変数を関数先頭で宣言
    monitoring_task = None
    
    parser = argparse.ArgumentParser(description='Form Sender Worker - Multi-Process Processing')
    parser.add_argument('--targeting-id', type=int, required=True, help='Targeting ID')
    parser.add_argument('--config-file', required=True, help='Config file path')
    parser.add_argument('--headless', choices=['true', 'false', 'auto'], default='auto', 
                       help='Browser headless mode (true=headless, false=GUI, auto=environment-based)')
    parser.add_argument('--test-batch-size', type=int, default=None,
                       help='Test mode batch size (overrides default batch size for testing)')
    parser.add_argument('--test-mode', action='store_true', help='DEPRECATED: test-mode is no longer supported')
    # ログモード: 既定でマッピング関連ログはQUIET（INFO/DEBUG抑制）。
    # --show-mapping-logs を指定すると抑制を解除（後方互換で --quiet-mapping-logs も受け付け）。
    parser.add_argument('--quiet-mapping-logs', action='store_true',
                       help='[Deprecated/Compat] Suppress mapping-related INFO/DEBUG logs')
    parser.add_argument('--show-mapping-logs', action='store_true',
                       help='Show mapping-related INFO/DEBUG logs (override default quiet)')
    # 追加入力リトライの詳細ログ（通常は抑制）
    parser.add_argument('--show-retry-logs', action='store_true',
                       help='Show detailed retry logs for missing-field autofill (default: suppressed)')
    
    args = parser.parse_args()
    
    # test-mode deprecation warning
    if args.test_mode:
        logger.warning("--test-mode is deprecated and no longer supported. All processing is now production-like.")
    
    # headlessモード設定の変換
    if args.headless == 'true':
        headless_mode = True
    elif args.headless == 'false':
        headless_mode = False
    else:  # 'auto'
        headless_mode = None
    
    logger.info(f"Browser headless mode: {args.headless} ({'forced headless' if headless_mode == True else 'forced GUI' if headless_mode == False else 'environment-based auto detection'})")

    # リトライ詳細ログの伝搬（サブプロセスへ環境変数で伝える）
    try:
        if args.show_retry_logs:
            os.environ['SHOW_RETRY_LOGS'] = '1'
        else:
            # 既に設定されている場合でも明示的にオフへ
            os.environ.pop('SHOW_RETRY_LOGS', None)
    except Exception:
        pass

    # マッピング関連ログの抑制（INFO/DEBUG のみ）。WARNING 以上は通す。
    try:
        from form_sender.security.log_filters import MappingLogFilter
        quiet_env = os.getenv('QUIET_MAPPING_LOGS', '')
        quiet_env_flag = True if quiet_env == '' else quiet_env.lower() in ['1', 'true', 'yes', 'on']
        # 既定: quiet（True）。--show-mapping-logs で解除。--quiet-mapping-logs は後方互換。
        quiet = (not args.show_mapping_logs) and (args.quiet_mapping_logs or quiet_env_flag)
        if quiet:
            _filter = MappingLogFilter()
            root_logger = logging.getLogger()
            root_logger.addFilter(_filter)
            for h in root_logger.handlers:
                h.addFilter(_filter)
            logger.info("Mapping logs set to QUIET by default (INFO/DEBUG suppressed)")
        else:
            logger.info("Mapping logs set to VERBOSE (INFO/DEBUG enabled)")
    except Exception as _log_filter_err:
        logger.warning(f"Failed to configure mapping-log filter: {_log_filter_err}")
    
    # test-batch-size設定
    if args.test_batch_size is not None:
        if args.test_batch_size < 1:
            logger.error(f"Invalid test-batch-size: {args.test_batch_size} (must be >= 1)")
            sys.exit(1)
        logger.info(f"Test batch size specified: {args.test_batch_size} (overrides default batch configuration)")
    
    # 改良版環境変数検証（厳格チェック）
    required_env_vars = {
        'SUPABASE_URL': {
            'required': True,
            'validation': lambda x: get_validation_manager().validate_supabase_url(x),
            'error_msg': get_validation_manager().get_supabase_url_validation()['error_msg']
        },
        'SUPABASE_SERVICE_ROLE_KEY': {
            'required': True,
            'validation': lambda x: get_validation_manager().validate_supabase_key(x),
            'error_msg': get_validation_manager().get_supabase_key_validation()['error_msg']
        },
        'GITHUB_ACTIONS': {
            'required': False,  # GitHub Actions環境でのみ必須
            'validation': lambda x: get_validation_manager().validate_github_actions_flag(x),
            'error_msg': get_validation_manager().get_github_actions_validation()['error_msg']
        },
        'TARGETING_ID': {
            'required': False,  # コマンドライン引数でも指定可能
            'validation': lambda x: x.isdigit() and 1 <= int(x) <= 999999,
            'error_msg': 'TARGETING_ID must be a valid integer between 1-999999'
        }
    }
    
    validation_errors = []
    missing_critical_vars = []
    
    for var_name, config in required_env_vars.items():
        env_value = os.environ.get(var_name)
        
        if not env_value:
            if config['required']:
                missing_critical_vars.append(var_name)
            continue
        
        # 値の検証
        try:
            if not config['validation'](env_value):
                validation_errors.append(f"{var_name}: {config['error_msg']}")
        except Exception as e:
            validation_errors.append(f"{var_name}: validation error - {e}")
    
    # エラーハンドリング
    if missing_critical_vars:
        logger.error(f"Critical environment variables missing: {missing_critical_vars}")
        
        # 厳格なフォールバック機構（限定的許可）
        development_mode = os.environ.get('DEVELOPMENT_MODE', '').lower()
        is_local_testing = development_mode == 'true' and os.path.exists('.env')
        
        if is_local_testing:
            logger.warning("DEVELOPMENT_MODE detected with .env file - allowing missing variables for local testing")
            logger.warning("This is only permitted for local development with .env configuration")
        else:
            logger.error("Production environment requires all critical environment variables")
            logger.error("Set DEVELOPMENT_MODE=true and ensure .env file exists for local testing")
            sys.exit(1)
    
    if validation_errors:
        logger.error("Environment variable validation failures:")
        for error in validation_errors:
            logger.error(f"  - {error}")
        
        # 検証エラーはフォールバックなしで終了
        if not os.environ.get('IGNORE_VALIDATION_ERRORS'):
            sys.exit(1)
        else:
            logger.warning("IGNORE_VALIDATION_ERRORS is set - continuing with validation errors")
    
    # 改良版設定ファイルパス検証（セキュリティ強化 - パストラバーサル対策）
    config_file_path = args.config_file
    
    # パストラバーサル攻撃対策: パス正規化による検証
    try:
        real_config_path = os.path.realpath(config_file_path)
    except OSError as e:
        logger.error(f"Failed to resolve config file path: {e}")
        sys.exit(1)
    
    # パスセキュリティチェック（正規化後のパスで検証）
    # 許可されるディレクトリ: /tmp/ または プロジェクトのtests/tmp/
    project_tests_tmp = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tests', 'tmp')
    project_tests_tmp_real = os.path.realpath(project_tests_tmp)
    
    allowed_paths = [
        '/tmp/',
        '/private/tmp/',  # macOS対応
        project_tests_tmp_real + '/'
    ]
    
    if not any(real_config_path.startswith(allowed_path) for allowed_path in allowed_paths):
        logger.error(f"Config file must be in allowed directories for security (resolved path): {real_config_path}")
        logger.error(f"Original path: {config_file_path}")
        logger.error(f"Allowed directories: {allowed_paths}")
        sys.exit(1)
    
    # 正規化されたパスを使用（シンボリックリンク攻撃対策）
    config_file_path = real_config_path
    
    if '*' in config_file_path:
        # ワイルドカードパターンの安全な解決
        import glob
        matching_files = glob.glob(config_file_path)
        
        # セキュリティフィルタリング
        safe_matching_files = []
        for file_path in matching_files:
            if (file_path.startswith('/tmp/client_config_') and 
                file_path.endswith('.json') and 
                os.path.isfile(file_path)):
                safe_matching_files.append(file_path)
            else:
                logger.warning(f"Filtered out unsafe config file: {file_path}")
        
        if not safe_matching_files:
            logger.error(f"No safe config files found matching pattern: {config_file_path}")
            logger.error("Expected pattern: /tmp/client_config_*.json")
            sys.exit(1)
        elif len(safe_matching_files) > 1:
            # 最新のファイルを選択（mtimeベース）
            safe_matching_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            logger.warning(f"Multiple config files found, using most recent: {safe_matching_files[0]}")
            logger.info(f"Available files: {safe_matching_files}")
        
        config_file_path = safe_matching_files[0]
        logger.info(f"Using config file: {config_file_path}")
    
    # ファイル存在と権限チェック
    if not os.path.exists(config_file_path):
        logger.error(f"Config file not found: {config_file_path}")
        sys.exit(1)
    
    # ファイル権限のセキュリティチェック（厳格版 - 600のみ許可）
    file_stat = os.stat(config_file_path)
    if (file_stat.st_mode & 0o777) != 0o600:
        logger.error(f"Config file must have exactly 600 permissions for security: {oct(file_stat.st_mode)}")
        # セキュリティ修正
        try:
            os.chmod(config_file_path, 0o600)
            logger.info("Fixed config file permissions to 600 (strict security)")
        except Exception as e:
            logger.error(f"Failed to fix file permissions: {e}")
            sys.exit(1)
    
    # ファイルサイズチェック
    file_size = os.path.getsize(config_file_path)
    if file_size < 50 or file_size > 10485760:  # 50 bytes - 10MB
        logger.error(f"Config file size suspicious: {file_size} bytes")
        sys.exit(1)
    
    # 改良版targeting_idの範囲チェック
    if not isinstance(args.targeting_id, int) or args.targeting_id <= 0 or args.targeting_id > 999999:
        logger.error(f"Invalid targeting_id: {args.targeting_id} (must be integer 1-999999)")
        sys.exit(1)
    
    # targeting_idの重複チェック（環境変数との整合性）
    env_targeting_id = os.environ.get('TARGETING_ID')
    if env_targeting_id:
        try:
            env_targeting_id_int = int(env_targeting_id)
            if env_targeting_id_int != args.targeting_id:
                logger.warning(
                    f"targeting_id mismatch: command line ({args.targeting_id}) vs environment ({env_targeting_id_int})"
                )
                logger.info(f"Using command line value: {args.targeting_id}")
        except ValueError:
            logger.warning(f"Invalid TARGETING_ID in environment: {env_targeting_id}")
    
    # マルチプロセス環境設定（競合回避版）
    try:
        current_method = mp.get_start_method(allow_none=True)
        if current_method is None:
            mp.set_start_method('spawn', force=False)  # forceフラグ削除で競合回避
            logger.info("Set multiprocessing start method to 'spawn'")
        else:
            logger.info(f"Using existing multiprocessing start method: '{current_method}'")
    except RuntimeError as e:
        logger.info(f"Multiprocessing start method already set: {e}")
        # 既存設定を維持
    
    logger.info(f"=== Form Sender Multi-Process Processing Started ===")
    # Targeting IDは非表示
    logger.info(f"Processing Mode: PRODUCTION - Multi-Process with Worker Pool")
    logger.info(f"Max Execution Time: 5 hours")
    
    # ワーカー数制限（システムリソースに基づく）
    max_system_workers = os.cpu_count() or 2
    logger.info(f"System CPU count: {max_system_workers}, max recommended workers: {min(max_system_workers, 8)}")
    
    # オーケストレーターを初期化（設定ファイルからワーカー数を自動取得）
    orchestrator = ConfigurableOrchestrator(args.targeting_id, headless_mode, args.test_batch_size)
    # 1件ごとの即時保存を明示化（冪等のため常にTrueを設定）
    try:
        orchestrator.immediate_save = True
        logger.info("Result saving mode set to: IMMEDIATE (per-record DB writes)")
    except Exception as _set_mode_err:
        logger.warning(f"Could not enforce immediate save mode explicitly: {_set_mode_err}")
    
    # スレッドセーフにオーケストレーターインスタンスを保存（クリーンアップ用）
    set_orchestrator_instance(orchestrator)
    
    # スレッドセーフなシグナルハンドラー設定（改良版）
    def signal_handler(signum, frame):
        """非同期安全なシグナルハンドラー"""
        # シグナルハンドラー内では最小限の処理のみ実行
        _shutdown_event.set()
        # 実際のクリーンアップはメインスレッドで実行される
    
    def install_signal_handlers():
        """シグナルハンドラーのインストール（競合状態対応版）"""
        global _signal_handlers_installed
        with _signal_install_lock:  # 競合状態対策: ロック保護
            if not _signal_handlers_installed:
                signal.signal(signal.SIGTERM, signal_handler)
                signal.signal(signal.SIGINT, signal_handler)
                _signal_handlers_installed = True
                logger.info("Signal handlers installed safely with lock protection")
    
    install_signal_handlers()
    
    try:
        # 初期化
        logger.info("Initializing orchestrator and database connection...")
        orchestrator.initialize_supabase()
        
        # 改良版クライアント設定データ取得（セキュリティ強化）
        try:
            with open(config_file_path, 'r', encoding='utf-8') as f:
                client_config = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            sys.exit(1)
        except UnicodeDecodeError as e:
            logger.error(f"Config file encoding error: {e}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Error reading config file: {e}")
            sys.exit(1)
        
        # 基本設定検証
        if not isinstance(client_config, dict):
            logger.error("Config file must contain a JSON object")
            sys.exit(1)
        
        if not client_config:
            logger.error("Config file is empty")
            sys.exit(1)
        
        # Client設定詳細は非表示（セキュリティ保護）
        config_fields_count = len(client_config)
        logger.info(f"Client configuration loaded successfully ({config_fields_count} fields)")
        
        # 改良版旧形式互換性データ生成（エラーハンドリング強化）
        try:
            client_data = _load_client_data_simple(config_file_path, args.targeting_id)
            if 'error' in client_data:
                logger.error(f"Client data loading error: {client_data['error']}")
                sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to load client data: {e}")
            sys.exit(1)
        
        # RuleBasedAnalyzerによるリアルタイム解析のみを使用
        
        # ワーカープロセス起動（エラーハンドリング強化）
        logger.info("Starting worker processes...")
        worker_start_success = await orchestrator.start_workers()
        
        if not worker_start_success:
            logger.error("Failed to start all worker processes")
            # 適切なクリーンアップ実行
            await _safe_cleanup_and_exit(orchestrator, 1)
        
        logger.info(f"All {orchestrator.num_workers} workers are ready!")
        
        # 改良版ワーカー監視タスクを開始（非同期安定化）
        try:
            # 監視タスクのシャットダウン対応強化
            monitoring_task = asyncio.create_task(
                _safe_worker_monitoring(orchestrator, check_interval=60)
            )
            logger.info("Enhanced worker monitoring task started")
        except Exception as monitoring_error:
            logger.error(f"Failed to start worker monitoring task: {monitoring_error}")
            # 監視タスクなしでも処理を継続（縮退運転）
        
        loop_count = 0
        consecutive_no_work_count = 0
        # テスト時は早期終了: test-batch-size 指定時は1サイクルで終了
        max_no_work_cycles = 0 if args.test_batch_size is not None else 3  # 0に変更: 1回のループで即座に終了
        
        # 連続処理ループ（マルチプロセス版）- 改良版停止制御
        while not orchestrator.should_stop and not _shutdown_event.is_set():
            # シグナル受信時の即座終了処理
            if _shutdown_event.is_set():
                logger.info("Shutdown signal detected, initiating graceful termination...")
                orchestrator.should_stop = True
                break
            loop_count += 1
            logger.info(f"--- Processing Loop {loop_count} ---")
            
            # 営業条件チェック（営業時間・時間制限・日次制限）
            can_continue, reason = orchestrator.check_business_conditions(client_config)
            if not can_continue:
                if reason == "営業時間外":
                    logger.info("Outside business hours - waiting 30 minutes before next check...")
                    await asyncio.sleep(1800)  # 30分待機（負荷軽減）
                    continue
                else:
                    logger.info(f"Processing stopped: {reason}")
                    break
            
            # ワーカーヘルスチェック
            worker_health = orchestrator.check_worker_health()
            unhealthy_workers = [w_id for w_id, status in worker_health.items() 
                               if status in ['dead', 'unresponsive']]
            
            if unhealthy_workers:
                logger.warning(f"Unhealthy workers detected: {unhealthy_workers}")
                # エラー統計の取得と出力
                error_stats = orchestrator.get_error_statistics()
                logger.warning(f"Error statistics: {error_stats}")
            
            # 改良版企業バッチ処理実行（非同期処理安定化）
            batch_start_time = time.time()
            batch_attempt = 1
            max_batch_retries = 3
            
            while batch_attempt <= max_batch_retries:
                try:
                    # シャットダウンシグナルチェック
                    if _shutdown_event.is_set() or orchestrator.should_stop:
                        logger.info("Shutdown detected, breaking from batch processing")
                        break
                    
                    # バッチ処理の段階的タイムアウト設定
                    base_timeout = 300  # 5分ベース
                    # リトライ時にタイムアウトを段階的に延長
                    batch_timeout = base_timeout * batch_attempt
                    
                    logger.info(f"Starting batch processing (attempt {batch_attempt}/{max_batch_retries}, timeout: {batch_timeout}s)")
                    
                    # バッチ処理の同期実行（RuleBasedAnalyzerリアルタイム解析、タイムアウト付き）
                    batch_stats = await asyncio.wait_for(
                        orchestrator.process_companies_batch(client_config, client_data),
                        timeout=batch_timeout
                    )
                    
                    # 成功時の処理
                    if batch_stats['companies_sent'] == 0:
                        consecutive_no_work_count += 1
                        logger.info(f"No companies to process (consecutive: {consecutive_no_work_count}/{max_no_work_cycles})")
                        
                        if consecutive_no_work_count >= max_no_work_cycles:
                            logger.info("No more companies available for processing")
                            return  # メインループを終了
                        
                        # テスト時は短縮待機で素早く終了
                        no_work_wait = 5 if args.test_batch_size is not None else 60
                        await asyncio.sleep(no_work_wait)
                        break  # バッチリトライループを抜ける
                    else:
                        consecutive_no_work_count = 0  # 処理があった場合はリセット
                    
                    # バッチ処理成功サマリー（営業禁止検出を失敗の内訳として含む）
                    batch_elapsed = time.time() - batch_start_time
                    prohibition_detected_failures = batch_stats.get('prohibition_detected_failures', 0)
                    
                    # ベースログ情報
                    log_msg = (f"Loop {loop_count} completed successfully: "
                               f"sent={batch_stats['companies_sent']}, "
                               f"success={batch_stats['success_count']}, "
                               f"failed={batch_stats['failed_count']}, "
                               f"errors={batch_stats['error_count']}, "
                               f"time={batch_elapsed:.2f}s")
                    
                    # 営業禁止検出による失敗の詳細
                    if prohibition_detected_failures > 0:
                        log_msg += f" (prohibition_failures={prohibition_detected_failures})"
                    
                    logger.info(log_msg)
                    
                    # テストモード時の即座終了制御
                    if args.test_batch_size is not None:
                        logger.info("Test batch size mode detected - forcing immediate termination after batch completion")
                        orchestrator.should_stop = True
                        _shutdown_event.set()
                    
                    # 成功時はリトライループを抜ける
                    break
                    
                except asyncio.TimeoutError:
                    batch_elapsed = time.time() - batch_start_time
                    logger.error(f"Batch processing timeout in loop {loop_count} (attempt {batch_attempt}/{max_batch_retries}, "
                                f"timeout: {batch_timeout}s, elapsed: {batch_elapsed:.2f}s)")
                    
                    # タイムアウト時の詳細ワーカーヘルスチェック
                    try:
                        worker_health = orchestrator.check_worker_health()
                        dead_workers = [w_id for w_id, status in worker_health.items() if status == 'dead']
                        unresponsive_workers = [w_id for w_id, status in worker_health.items() if status == 'unresponsive']
                        
                        logger.warning(f"Worker health after timeout: {worker_health}")
                        
                        if dead_workers:
                            logger.error(f"Dead workers detected: {dead_workers}")
                            # 緊急復旧を試行
                            recovery_success = await orchestrator.recover_failed_workers()
                            if recovery_success:
                                logger.info("Emergency worker recovery successful")
                            else:
                                logger.error("Emergency worker recovery failed")
                        
                        if unresponsive_workers:
                            logger.warning(f"Unresponsive workers detected: {unresponsive_workers}")
                    
                    except Exception as health_check_error:
                        logger.error(f"Failed to check worker health after timeout: {health_check_error}")
                    
                    # リトライ可能性をチェック
                    if batch_attempt < max_batch_retries:
                        backoff_time = 30 * batch_attempt  # 指数バックオフ
                        logger.info(f"Retrying batch processing in {backoff_time} seconds...")
                        await asyncio.sleep(backoff_time)
                        batch_attempt += 1
                    else:
                        logger.error(f"Max batch retries reached ({max_batch_retries}), skipping this batch")
                        break
                        
                except asyncio.CancelledError:
                    logger.info("Batch processing cancelled")
                    raise  # キャンセル例外は再発生
                    
                except Exception as batch_error:
                    batch_elapsed = time.time() - batch_start_time
                    logger.error(f"Batch processing error in loop {loop_count} (attempt {batch_attempt}/{max_batch_retries}, "
                                f"elapsed: {batch_elapsed:.2f}s): {batch_error}")
                    
                    # エラー後のワーカー状態確認と復旧
                    try:
                        worker_health = orchestrator.check_worker_health()
                        unhealthy_count = sum(1 for status in worker_health.values() if status in ['dead', 'unresponsive'])
                        
                        if unhealthy_count > 0:
                            logger.warning(f"Detected {unhealthy_count} unhealthy workers after error")
                            # エラー後の自動復旧
                            recovery_attempted = await orchestrator.recover_failed_workers()
                            if recovery_attempted:
                                logger.info("Worker recovery attempted after error")
                            else:
                                logger.error("Worker recovery failed after error")
                        
                        logger.info(f"Worker health: {worker_health}")
                        
                    except Exception as health_check_error:
                        logger.error(f"Failed to check worker health after error: {health_check_error}")
                    
                    # リトライ可能性をチェック
                    if batch_attempt < max_batch_retries:
                        backoff_time = 10 * batch_attempt  # 短いバックオフ
                        logger.info(f"Retrying batch processing in {backoff_time} seconds...")
                        await asyncio.sleep(backoff_time)
                        batch_attempt += 1
                    else:
                        logger.error(f"Max batch retries reached ({max_batch_retries}), skipping this batch")
                        break
            
            # バッチ間の待機時間（Supabase負荷軽減・シグナルチェック付き）
            base_sleep_time = 5
            for i in range(base_sleep_time):
                if _shutdown_event.is_set() or orchestrator.should_stop:
                    logger.info("Shutdown detected during sleep, breaking immediately")
                    return
                await asyncio.sleep(1)
        
        # 最終サマリー
        final_summary = orchestrator.get_processing_summary()
        logger.info(f"=== Form Sender Multi-Process Processing Completed ===")
        logger.info(f"Total Loops: {loop_count}")
        logger.info(f"Total Companies Processed: {final_summary['processed_count']}")
        logger.info(f"Successful: {final_summary['success_count']}")
        logger.info(f"Failed: {final_summary['failed_count']}")
        logger.info(f"Total Execution Time: {final_summary['elapsed_time']:.2f} seconds")
        logger.info(f"Workers Used: {final_summary['num_workers']}")
        logger.info(f"Batches Processed: {final_summary['orchestrator_stats']['batches_processed']}")
        
        # 営業禁止検出統計を失敗分析として表示（Form Analyzer準拠）
        try:
            prohibition_summary = orchestrator.get_prohibition_detection_summary()
            logger.info(f"=== Advanced Prohibition Detection Summary (Failure Analysis) ===")
            logger.info(f"Companies Checked for Prohibition: {prohibition_summary['total_checked']}")
            logger.info(f"Prohibition-based Failures: {prohibition_summary['prohibition_detected_count']}")
            logger.info(f"Prohibition Detection Rate: {prohibition_summary['prohibition_detection_rate']:.2%}")
            logger.info(f"Failed Companies due to Prohibition: {prohibition_summary['skipped_companies']}")
        except Exception as e:
            logger.warning(f"Could not display prohibition detection summary: {e}")
        
        termination_reason = reason if not can_continue else 'No more companies or stopped by signal'
        logger.info(f"Termination Reason: {termination_reason}")
        
    except Exception as e:
        logger.error(f"Main process error: {e}")
        # 適切なクリーンアップ実行
        await _safe_cleanup_and_exit(orchestrator, 1)
        
    finally:
        # 改良版リソースクリーンアップ（非同期安定化）
        logger.info("Starting comprehensive resource cleanup...")
        
        # 監視タスクの安全なキャンセル（段階的タイムアウト）
        if monitoring_task and not monitoring_task.done():
            logger.info("Cancelling worker monitoring task...")
            monitoring_task.cancel()
            
            try:
                # Phase 1: 5秒でグレースフルキャンセルを試行
                await asyncio.wait_for(monitoring_task, timeout=5.0)
                logger.info("Worker monitoring task cancelled gracefully")
            except asyncio.CancelledError:
                logger.info("Worker monitoring task cancelled")
            except asyncio.TimeoutError:
                logger.warning("Worker monitoring task cancellation timeout (5s)")
                # Phase 2: 強制キャンセルの再試行
                try:
                    monitoring_task.cancel()
                    await asyncio.wait_for(monitoring_task, timeout=2.0)
                    logger.info("Worker monitoring task force cancelled")
                except Exception:
                    logger.error("Failed to cancel monitoring task, continuing cleanup...")
            except Exception as cancel_error:
                logger.error(f"Error cancelling monitoring task: {cancel_error}")
        
        # オーケストレーターの安全なクリーンアップ（タイムアウト付き）
        try:
            cleanup_task = asyncio.create_task(orchestrator.cleanup())
            await asyncio.wait_for(cleanup_task, timeout=30.0)
            logger.info("Orchestrator cleanup completed")
        except asyncio.TimeoutError:
            logger.error("Orchestrator cleanup timeout (30s)")
            # タイムアウト時の強制クリーンアップ
            await _force_terminate_remaining_processes(orchestrator)
        except Exception as cleanup_error:
            logger.error(f"Error during orchestrator cleanup: {cleanup_error}")


async def _force_terminate_remaining_processes(orchestrator):
    """
    残存プロセスの強制終了（ゾンビプロセス防止）
    
    Args:
        orchestrator: オーケストレーターインスタンス
    """
    try:
        # 生存しているワーカーを確認
        alive_workers = orchestrator.safe_check_alive_workers()
        
        if alive_workers:
            logger.warning(f"Force terminating {len(alive_workers)} remaining worker processes")
            
            for i, worker_process in enumerate(orchestrator.worker_processes):
                if worker_process in alive_workers:
                    try:
                        logger.info(f"Force killing worker process {i} (PID: {worker_process.pid})")
                        worker_process.kill()  # SIGKILLで強制終了
                        worker_process.join(timeout=5)  # プロセス終了を待機
                    except Exception as e:
                        logger.error(f"Failed to force kill worker {i}: {e}")
            
            # 段階的終了確認とゾンビプロセス防止
            termination_verified = await _verify_complete_termination(orchestrator, alive_workers)
            
            if termination_verified:
                logger.info("All processes successfully terminated with complete verification")
            else:
                logger.error("Process termination verification failed - potential zombie processes may remain")
        else:
            logger.info("No remaining processes to terminate")
            
    except Exception as e:
        logger.error(f"Error during force termination: {e}")


async def _verify_complete_termination(orchestrator, original_alive_workers) -> bool:
    """
    プロセス終了の完全性検証
    
    Args:
        orchestrator: オーケストレーターインスタンス
        original_alive_workers: 終了対象だった生存プロセスリスト
        
    Returns:
        bool: 完全終了が確認できた場合True
    """
    import os
    import psutil
    import time
    
    try:
        logger.info("Starting comprehensive process termination verification")
        
        # Step 1: オーケストレーター経由での生存確認
        final_check = orchestrator.safe_check_alive_workers()
        if final_check:
            logger.warning(f"Orchestrator still reports {len(final_check)} alive workers")
            
        # Step 2: PIDベースの直接確認
        zombie_processes = []
        for worker_process in original_alive_workers:
            if worker_process:
                try:
                    pid = worker_process.pid
                    
                    # /proc/<pid>/status確認（Linuxの場合）
                    if os.path.exists(f"/proc/{pid}"):
                        try:
                            with open(f"/proc/{pid}/status", 'r') as f:
                                status_content = f.read()
                                if 'State:' in status_content:
                                    state_line = [line for line in status_content.split('\n') if line.startswith('State:')][0]
                                    if 'Z (zombie)' in state_line:
                                        zombie_processes.append(pid)
                                        logger.warning(f"Zombie process detected: PID {pid}")
                                    elif 'T (stopped)' not in state_line and 'X (dead)' not in state_line:
                                        logger.warning(f"Process still active: PID {pid}, State: {state_line}")
                        except (FileNotFoundError, IndexError, IOError):
                            # プロセスが既に終了している（正常）
                            pass
                    
                    # psutilでの二重確認
                    try:
                        proc = psutil.Process(pid)
                        if proc.is_running():
                            status = proc.status()
                            if status == psutil.STATUS_ZOMBIE:
                                zombie_processes.append(pid)
                                logger.warning(f"psutil confirms zombie process: PID {pid}")
                            else:
                                logger.warning(f"psutil reports active process: PID {pid}, status {status}")
                                return False
                    except psutil.NoSuchProcess:
                        # プロセス終了済み（正常）
                        logger.debug(f"Process {pid} confirmed terminated via psutil")
                    except psutil.AccessDenied:
                        # アクセス権限なし（プロセスは存在している可能性）
                        logger.warning(f"Access denied for process {pid} - may still be running")
                        
                except Exception as e:
                    logger.error(f"Error checking process {worker_process}: {e}")
                    
        # Step 3: ゾンビプロセスのクリーンアップ
        if zombie_processes:
            logger.warning(f"Attempting to clean up {len(zombie_processes)} zombie processes")
            await _cleanup_zombie_processes(zombie_processes)
            
        # Step 4: ファイルディスクリプタリーク確認
        try:
            current_process = psutil.Process(os.getpid())
            fd_count = current_process.num_fds()
            logger.info(f"Current process file descriptors: {fd_count}")
            
            # 異常に多いFDは潜在的なリークを示す
            if fd_count > 1000:  # 閾値は調整可能
                logger.warning(f"High file descriptor count detected: {fd_count} - potential resource leak")
                
        except Exception as fd_error:
            logger.warning(f"Could not check file descriptors: {fd_error}")
            
        # Step 5: 最終判定
        if not final_check and not zombie_processes:
            logger.info("Complete termination verification: SUCCESS")
            return True
        else:
            logger.error(f"Complete termination verification: FAILED - {len(final_check)} alive, {len(zombie_processes)} zombies")
            return False
            
    except Exception as e:
        logger.error(f"Process termination verification error: {e}")
        return False


async def _cleanup_zombie_processes(zombie_pids: list) -> None:
    """
    ゾンビプロセスのクリーンアップ
    
    Args:
        zombie_pids: ゾンビプロセスのPIDリスト
    """
    import signal
    import time
    
    try:
        logger.info(f"Attempting to clean up {len(zombie_pids)} zombie processes")
        
        for pid in zombie_pids:
            try:
                # 親プロセスにSIGCHLDを送信してゾンビ回収を促す
                try:
                    proc = psutil.Process(pid)
                    parent_pid = proc.ppid()
                    if parent_pid and parent_pid != 1:  # init以外の親がいる場合
                        os.kill(parent_pid, signal.SIGCHLD)
                        logger.info(f"Sent SIGCHLD to parent {parent_pid} for zombie cleanup")
                        time.sleep(0.1)  # 短時間待機
                except (psutil.NoSuchProcess, ProcessLookupError):
                    # ゾンビが既に回収された（正常）
                    continue
                    
                # 再確認
                try:
                    proc = psutil.Process(pid)
                    if proc.status() != psutil.STATUS_ZOMBIE:
                        logger.info(f"Zombie process {pid} successfully cleaned up")
                    else:
                        logger.warning(f"Zombie process {pid} still remains - may require system-level intervention")
                except psutil.NoSuchProcess:
                    logger.info(f"Zombie process {pid} has been reaped")
                    
            except Exception as cleanup_error:
                logger.error(f"Failed to cleanup zombie process {pid}: {cleanup_error}")
                
    except Exception as e:
        logger.error(f"Zombie process cleanup error: {e}")


async def _safe_cleanup_and_exit(orchestrator, exit_code: int = 0):
    """
    改良版安全なクリーンアップとプロセス終了
    
    Args:
        orchestrator: オーケストレーターインスタンス
        exit_code: 終了コード
    """
    global _cleanup_lock, _cleanup_completed
    
    # 既にクリーンアップが完了している場合は即座に終了
    if _cleanup_completed.is_set():
        logger.info("Cleanup already completed, exiting immediately")
        sys.exit(exit_code)
    
    # アトミックなクリーンアップ実行
    cleanup_acquired = False
    try:
        cleanup_acquired = _cleanup_lock.acquire(timeout=5.0)
        if not cleanup_acquired:
            logger.error("Failed to acquire cleanup lock, forcing exit")
            sys.exit(exit_code)
        
        # 重複クリーンアップ防止
        if _cleanup_completed.is_set():
            logger.info("Cleanup already completed by another thread")
            return
        
        logger.info("Starting comprehensive cleanup process...")
        
        # シャットダウン状態を設定
        _shutdown_event.set()
        
        if orchestrator:
            # オーケストレーターの停止要求
            orchestrator.should_stop = True
            
            # 子プロセスの段階的終了（改良版）
            if hasattr(orchestrator, 'worker_processes') and orchestrator.worker_processes:
                logger.info(f"Terminating {len(orchestrator.worker_processes)} worker processes...")
                
                # Phase 1: Graceful termination request
                for i, worker_process in enumerate(orchestrator.worker_processes):
                    if worker_process and worker_process.is_alive():
                        logger.info(f"Requesting graceful termination of worker {i} (PID: {worker_process.pid})")
                        try:
                            worker_process.terminate()
                        except Exception as e:
                            logger.warning(f"Error terminating worker {i}: {e}")
                
                # Phase 2: Wait for graceful termination (10 seconds)
                logger.info("Waiting for graceful worker termination...")
                graceful_wait_start = time.time()
                while time.time() - graceful_wait_start < 10:
                    # スレッドセーフなワーカー生存チェック（デッドロック防止）
                    alive_processes = orchestrator.safe_check_alive_workers()
                    
                    if not alive_processes:
                        logger.info("All workers terminated gracefully")
                        break
                            
                    await asyncio.sleep(0.2)
                
                # Phase 3: Force termination for remaining processes
                alive_workers = orchestrator.safe_check_alive_workers()
                remaining_workers = []
                for i, worker_process in enumerate(orchestrator.worker_processes):
                    if worker_process in alive_workers:
                        remaining_workers.append((i, worker_process))
                
                if remaining_workers:
                    logger.warning(f"Force terminating {len(remaining_workers)} remaining workers")
                    for i, worker_process in remaining_workers:
                        try:
                            logger.warning(f"Force killing worker {i} (PID: {worker_process.pid})")
                            worker_process.kill()
                            worker_process.join(timeout=2)
                        except Exception as e:
                            logger.error(f"Error force-killing worker {i}: {e}")
            
            # オーケストレーターのクリーンアップ（タイムアウト付き）
            try:
                cleanup_task = asyncio.create_task(orchestrator.cleanup())
                await asyncio.wait_for(cleanup_task, timeout=15.0)
                logger.info("Orchestrator cleanup completed")
            except asyncio.TimeoutError:
                logger.error("Orchestrator cleanup timed out")
            except Exception as cleanup_error:
                logger.error(f"Error during orchestrator cleanup: {cleanup_error}")
        
        # クリーンアップ完了マーク
        _cleanup_completed.set()
        logger.info("Comprehensive cleanup completed successfully")
        
    except Exception as e:
        logger.error(f"Critical error during cleanup: {e}")
        _cleanup_completed.set()  # エラー時もマークして無限ループを防ぐ
    finally:
        if cleanup_acquired:
            _cleanup_lock.release()
        
        # 最終プロセス終了
        logger.info(f"Exiting with code {exit_code}")
        sys.exit(exit_code)


# instruction_json関連の関数は削除 - RuleBasedAnalyzerのリアルタイム解析のみを使用


def _load_client_data_simple(config_file: str, targeting_id: int) -> dict:
    """
    改良版2シート構造対応クライアントデータ読み込み（構造整合性確保・セキュリティ強化版）
    
    Args:
        config_file: 設定ファイルパス
        targeting_id: ターゲティングID
    
    Returns:
        dict: 2シート構造のクライアントデータ
    
    Raises:
        Exception: 重大なエラー時
    """
    try:
        # セキュリティチェック（本番用/tmp/とテスト用tests/tmp/を許可）
        project_tests_tmp = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tests', 'tmp')
        project_tests_tmp_real = os.path.realpath(project_tests_tmp)
        allowed_patterns = [
            '/tmp/client_config_',
            '/private/tmp/client_config_',
            os.path.join(project_tests_tmp_real, 'client_config_')
        ]
        
        is_allowed = any(config_file.startswith(pattern) for pattern in allowed_patterns)
        if not is_allowed:
            raise ValueError(f"Unsafe config file path: {config_file}")
        
        with open(config_file, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        
        # 基本データ型チェック
        if not isinstance(config_data, dict):
            raise ValueError("Config data must be a dictionary")
        
        if not config_data:
            raise ValueError("Config data is empty")
        
        # 2シート構造の厳密な整合性チェック
        required_sections = ['client', 'targeting']
        missing_sections = [section for section in required_sections if section not in config_data]
        
        if missing_sections:
            error_msg = f"2シート構造の必須セクションが不足: {missing_sections}"
            logger.error(error_msg)
            return {
                'targeting_id': targeting_id,
                'client_id': config_data.get('client_id', 0),
                'error': error_msg
            }
        
        # 各セクションの型チェック
        for section in required_sections:
            if not isinstance(config_data[section], dict):
                error_msg = f"Section '{section}' must be a dictionary, got {type(config_data[section])}"
                logger.error(error_msg)
                return {
                    'targeting_id': targeting_id,
                    'client_id': config_data.get('client_id', 0),
                    'error': error_msg
                }
            
            if not config_data[section]:
                error_msg = f"Section '{section}' is empty"
                logger.error(error_msg)
                return {
                    'targeting_id': targeting_id,
                    'client_id': config_data.get('client_id', 0),
                    'error': error_msg
                }
        
        # 基本管理情報の検証
        client_id = config_data.get('client_id')
        if client_id is not None and (not isinstance(client_id, int) or client_id < 0):
            logger.warning(f"Invalid client_id: {client_id}, using 0")
            client_id = 0
        
        # 2シート構造をそのまま保持（セキュリティコピー）
        client_data = {
            'targeting_id': targeting_id,
            'client_id': client_id or 0,
            # 2シート構造をネスト構造で保持（ディープコピー）
            'client': dict(config_data['client']),
            'targeting': dict(config_data['targeting']),
            # 基本管理情報
            'active': bool(config_data.get('active', True)),
            'description': str(config_data.get('description', ''))
        }
        
        logger.info(f"2シート構造クライアントデータ読み込み完了: targeting_id={targeting_id}, client_id={client_data['client_id']}")
        logger.info(f"Client fields: {len(client_data['client'])}, Targeting fields: {len(client_data['targeting'])}")
        
        return client_data
        
    except json.JSONDecodeError as e:
        error_msg = f"JSON解析エラー: {e}"
        logger.error(error_msg)
        return {
            'targeting_id': targeting_id,
            'error': error_msg
        }
    except (IOError, OSError) as e:
        error_msg = f"ファイルI/Oエラー: {e}"
        logger.error(error_msg)
        return {
            'targeting_id': targeting_id,
            'error': error_msg
        }
    except ValueError as e:
        error_msg = f"データ検証エラー: {e}"
        logger.error(error_msg)
        return {
            'targeting_id': targeting_id,
            'error': error_msg
        }
    except Exception as e:
        error_msg = f"予期しないエラー: {e}"
        logger.error(error_msg)
        return {
            'targeting_id': targeting_id,
            'error': error_msg
        }


async def _safe_worker_monitoring(orchestrator, check_interval: float = 60) -> None:
    """
    改良版ワーカー監視タスク（シャットダウン対応強化）
    
    Args:
        orchestrator: オーケストレーターインスタンス
        check_interval: チェック間隔（秒）
    """
    logger.info(f"Starting safe worker monitoring task (interval: {check_interval}s)")
    
    try:
        while orchestrator.is_running and not orchestrator.should_stop and not _shutdown_event.is_set():
            try:
                # シャットダウンシグナルの频繁チェック
                if _shutdown_event.is_set():
                    logger.info("Shutdown signal detected in monitoring task")
                    break
                
                # ヘルスチェック
                health_status = orchestrator.check_worker_health()
                unhealthy_workers = [
                    w_id for w_id, status in health_status.items() if status in ["dead", "unresponsive"]
                ]
                
                if unhealthy_workers:
                    logger.warning(f"Unhealthy workers detected in monitoring: {unhealthy_workers}")
                    
                    # 復旧処理実行（タイムアウト付き）
                    try:
                        recovery_task = asyncio.create_task(orchestrator.recover_failed_workers())
                        recovery_success = await asyncio.wait_for(recovery_task, timeout=30.0)
                        
                        if recovery_success:
                            logger.info("Worker recovery completed successfully in monitoring")
                        else:
                            logger.error("Worker recovery failed or incomplete in monitoring")
                    except asyncio.TimeoutError:
                        logger.error("Worker recovery timeout in monitoring (30s)")
                    except Exception as recovery_error:
                        logger.error(f"Worker recovery error in monitoring: {recovery_error}")
                
                # キュー統計の監視
                try:
                    pending_count = orchestrator.queue_manager.get_pending_task_count()
                    if pending_count > 100:  # 闾値を上げて警告頻度を減らす
                        logger.warning(f"High number of pending tasks in monitoring: {pending_count}")
                except Exception as queue_error:
                    logger.error(f"Error checking queue status in monitoring: {queue_error}")
                
                # シャットダウンシグナル対応の細かい待機
                sleep_start = time.time()
                while (time.time() - sleep_start) < check_interval:
                    if _shutdown_event.is_set() or orchestrator.should_stop:
                        logger.info("Shutdown detected during monitoring sleep")
                        return
                    await asyncio.sleep(1)  # 1秒ごとにシャットダウンチェック
                
            except asyncio.CancelledError:
                logger.info("Worker monitoring task cancelled")
                raise
            except Exception as monitor_error:
                logger.error(f"Error in worker monitoring cycle: {monitor_error}")
                # エラー後の短い待機
                try:
                    await asyncio.sleep(min(check_interval, 30))
                except asyncio.CancelledError:
                    logger.info("Worker monitoring task cancelled during error recovery")
                    raise
    
    except asyncio.CancelledError:
        logger.info("Worker monitoring task cancelled gracefully")
        raise
    except Exception as e:
        logger.error(f"Critical error in worker monitoring task: {e}")
    finally:
        logger.info("Safe worker monitoring task stopped")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, initiating shutdown...")
        _shutdown_event.set()
    except Exception as e:
        logger.error(f"Critical error in main: {e}")
        sys.exit(1)
