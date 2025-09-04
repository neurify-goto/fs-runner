"""
Form Finder Module

GitHub Actions向けのマルチプロセス・フォーム探索システムとSupabase書き込み機能を提供。
"""

# マルチプロセス・フォーム探索コンポーネント
from .worker import IsolatedFormFinderWorker, worker_process_main
from .orchestrator import ConfigurableFormFinderOrchestrator, FormFinderOrchestrator
from .utils import safe_log_info, safe_log_error

# Supabaseはオプショナルな依存関係として扱う
try:
    from .supabase_writer import SupabaseFormFinderWriter
    __all__ = [
        'IsolatedFormFinderWorker', 
        'worker_process_main',
        'ConfigurableFormFinderOrchestrator', 
        'FormFinderOrchestrator',
        'SupabaseFormFinderWriter', 
        'safe_log_info', 
        'safe_log_error'
    ]
except ImportError:
    __all__ = [
        'IsolatedFormFinderWorker', 
        'worker_process_main',
        'ConfigurableFormFinderOrchestrator', 
        'FormFinderOrchestrator',
        'safe_log_info', 
        'safe_log_error'
    ]