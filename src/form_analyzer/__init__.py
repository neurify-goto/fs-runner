"""
Form Analyzer Module

企業フォームページを解析し、営業禁止検出とプロンプト生成を行うモジュール。
"""

from .worker import FormAnalyzerWorker
from .form_extractor import FormExtractor
from .groq_client import GroqClient
from .prohibition_detector import ProhibitionDetector
from .prompt_generator import PromptGenerator

# Supabaseはオプショナルな依存関係として扱う
try:
    from .supabase_writer import SupabaseFormAnalyzerWriter
    __all__ = [
        'FormAnalyzerWorker',
        'FormExtractor',
        'GroqClient',
        'ProhibitionDetector',
        'PromptGenerator',
        'SupabaseFormAnalyzerWriter'
    ]
except ImportError:
    __all__ = [
        'FormAnalyzerWorker',
        'FormExtractor',
        'GroqClient',
        'ProhibitionDetector',
        'PromptGenerator'
    ]