"""
エラー分類ユーティリティ

指示書問題と外部要因を正確に区別するための共通分類ロジック
性能最適化版：パターンマッチング効率化、メソッド複雑度削減、例外チェーン保持
"""

import re
import logging
from typing import Dict, Any, List, Pattern, Optional, Tuple


class ErrorClassifier:
    """エラー分類用ユーティリティクラス（性能最適化版）

    目的:
    - 送信失敗時に「未入力/入力してください」などの画面上の検証メッセージを検知し、
      送信ボタン未検出系に紐づけず MAPPING/VALIDATION 系へ正しく分類する。
    - 既存の軽量パターン分類に加えて、ページコンテンツも加味した詳細分類を提供。
    """
    
    # 最適化：パターンとエラータイプの統合管理
    # (patterns, error_type, priority) のタプル形式で管理
    ERROR_PATTERN_RULES = [
        # 最高優先度: 外部要因パターン（最適化済み）
        ([
            re.compile(r'network[\s\w]*timeout', re.IGNORECASE),
            re.compile(r'server[\s\w]*error', re.IGNORECASE),
            re.compile(r'connection[\s\w]*refused', re.IGNORECASE),
            re.compile(r'site[\s\w]*maintenance', re.IGNORECASE),
            re.compile(r'cloudflare[\s\w]*protection', re.IGNORECASE),
            re.compile(r'access[\s\w]*denied', re.IGNORECASE),
            re.compile(r'page\s+load[\s\w]*timeout', re.IGNORECASE)
        ], 'EXTERNAL', 1),
        
        # 指示書構造問題パターン（最適化済み）
        ([
            re.compile(r'instruction_json[\s\w]*invalid', re.IGNORECASE),
            re.compile(r'json[\s\w]*decode[\s\w]*error', re.IGNORECASE),
            re.compile(r'placeholder[\s\w]*not[\s\w]*found', re.IGNORECASE),
            re.compile(r'missing[\s\w]*instruction', re.IGNORECASE),
            re.compile(r'invalid[\s\w]*json', re.IGNORECASE)
        ], 'INSTRUCTION', 2),
        
        # 送信ボタン関連エラーパターン（最適化済み）
        ([
            re.compile(r'submit\s*button[\s\w]*not\s*found', re.IGNORECASE),
            re.compile(r'no\s*submit\s*button[\s\w]*selector', re.IGNORECASE),
            re.compile(r'submit[\s\w]*selector[\s\w]*not[\s\w]*provided', re.IGNORECASE),
            re.compile(r'button[\s\w]*type[\s\w]*submit[\s\w]*not[\s\w]*found', re.IGNORECASE)
        ], 'SUBMIT_BUTTON', 3),
        
        # 成功判定関連エラーパターン（最適化済み）
        ([
            re.compile(r'cannot\s*determine\s*success', re.IGNORECASE),
            re.compile(r'no[\s\w]*success[\s\w]*indicators', re.IGNORECASE),
            re.compile(r'success[\s\w]*determination[\s\w]*failed', re.IGNORECASE),
            re.compile(r'no[\s\w]*clear[\s\w]*success[\s\w]*error[\s\w]*indicators', re.IGNORECASE)
        ], 'SUCCESS_DETERMINATION_FAILED', 4),
        
        # コンテンツ分析関連エラーパターン（最適化済み）
        ([
            re.compile(r'error[\s\w]*indicators[\s\w]*found[\s\w]*in[\s\w]*content', re.IGNORECASE),
            re.compile(r'no[\s\w]*url[\s\w]*change[\s\w]*detected', re.IGNORECASE),
            re.compile(r'content[\s\w]*analysis[\s\w]*failed', re.IGNORECASE),
            re.compile(r'error[\s\w]*analyzing[\s\w]*page[\s\w]*content', re.IGNORECASE)
        ], 'CONTENT_ANALYSIS', 5),
        
        # フィールド要素関連エラーパターン（最適化済み）
        ([
            re.compile(r'element[\s\w]*not[\s\w]*found[\s\w]*for', re.IGNORECASE),
            re.compile(r'selector[\s\w]*not[\s\w]*found', re.IGNORECASE),
            re.compile(r'element[\s\w]*timeout', re.IGNORECASE),
            re.compile(r'locator[\s\w]*not[\s\w]*found', re.IGNORECASE)
        ], 'ELEMENT_NOT_FOUND', 6),
        
        # 入力タイプ不一致エラーパターン（最適化済み）
        ([
            re.compile(r'cannot\s*type[\s\w]*into\s*input[\s\w]*type', re.IGNORECASE),
            re.compile(r'input[\s\w]*type[\s\w]*mismatch', re.IGNORECASE),
            re.compile(r'cannot[\s\w]*fill[\s\w]*field[\s\w]*type', re.IGNORECASE),
            re.compile(r'error[\s\w]*filling[\s\w]*field', re.IGNORECASE)
        ], 'INPUT_TYPE_MISMATCH', 7),
        
        # フォーム検証エラーパターン（最適化済み）
        ([
            re.compile(r'validation[\s\w]*error', re.IGNORECASE),
            re.compile(r'required[\s\w]*field[\s\w]*failed', re.IGNORECASE),
            re.compile(r'form[\s\w]*validation[\s\w]*failed', re.IGNORECASE),
            re.compile(r'invalid[\s\w]*input[\s\w]*value', re.IGNORECASE)
        ], 'FORM_VALIDATION_ERROR', 8)
    ]
    
    # 最適化：キーワードをコンパイル済み正規表現に変更
    BOT_PATTERN = re.compile(r'\b(?:recaptcha|cloudflare|bot)\b', re.IGNORECASE)
    INSTRUCTION_KEYWORD_PATTERN = re.compile(r'\b(?:parse|decode|invalid|missing)\b', re.IGNORECASE)
    ELEMENT_KEYWORD_PATTERN = re.compile(r'\b(?:element|selector|locator)\b', re.IGNORECASE)
    INSTRUCTION_JSON_PATTERN = re.compile(r'\b(?:instruction|json)\b', re.IGNORECASE)

    # 追加: 日本語・英語の入力必須/未入力バリエーション（ページテキスト用）
    REQUIRED_TEXT_PATTERNS: List[Pattern] = [
        re.compile(p) for p in [
            r"未入力",
            r"入力\s*してください",
            r"入力されていません",
            r"必須\s*項目",
            r"必須です",
            r"選択\s*してください",
            r"チェック\s*してください",
            r"空白|空欄",
            r"\bfield\s+is\s+required\b",
            r"\brequired\s+field\b",
            r"\bplease\s+(enter|select|fill)\b",
            r"\b(cannot\s+be\s+blank|must\s+not\s+be\s+empty)\b",
        ]
    ]

    # 追加: フォーマット不正バリエーション
    FORMAT_TEXT_PATTERNS: List[Pattern] = [
        re.compile(p, re.IGNORECASE) for p in [
            r"形式が正しくありません",
            r"正しく入力してください",
            r"invalid\s+format",
            r"invalid\s+(email|phone|url)",
            r"メール.*(形式|正しく|無効)",
            r"phone.*(invalid|format)",
        ]
    ]

    # 追加: その他代表的エラー
    CAPTCHA_TEXT_PATTERNS: List[Pattern] = [re.compile(r, re.IGNORECASE) for r in [r"captcha", r"recaptcha", r"私はロボットではありません"]]
    # CSRF は誤判定防止のため「token」単独では判定しない。エラー語と近接している場合のみ検出。
    CSRF_NEAR_ERROR_PATTERNS: List[Pattern] = [
        # 英語: CSRF/XSRF/forgery/authenticity + エラー語（invalid/mismatch/expired/missing/failedなど）が近接
        re.compile(r"(csrf|xsrf|forgery|authenticity)[^\n<]{0,80}(invalid|mismatch|expired|missing|required|failed|error)", re.IGNORECASE),
        # 日本語: (CSRF|ワンタイム(キー|トークン)|トークン) + エラー語（無効/不一致/期限/切れ/エラー）
        re.compile(r"(csrf|ワンタイム(?:キー|トークン)|トークン)[^\n<]{0,80}(無効|不一致|期限|切れ|エラー)")
    ]
    DUPLICATE_TEXT_PATTERNS: List[Pattern] = [re.compile(r, re.IGNORECASE) for r in [r"重複", r"既に(送信|登録)", r"duplicate", r"already\s+submitted"]]

    @classmethod
    def classify_error_type(cls, error_context: Dict[str, Any]) -> str:
        """
        エラータイプの分類（性能最適化版）
        
        フォーム処理の各段階で発生するエラーを詳細に分類
        
        Args:
            error_context: エラーコンテキスト情報
            
        Returns:
            str: エラータイプ
        """
        error_message = error_context.get('error_message', '').lower()
        is_bot_detected = error_context.get('is_bot_detected', False)
        is_timeout = error_context.get('is_timeout', False)
        
        try:
            # 1. 特別なケースを先に処理
            special_case = cls._classify_special_cases(error_message, is_bot_detected, is_timeout)
            if special_case:
                return special_case
            
            # 2. パターンベースの分類（最適化済み）
            pattern_result = cls._classify_by_patterns(error_message)
            if pattern_result:
                return cls._refine_pattern_result(pattern_result, error_message)
            
            # 3. フォールバック分類
            return cls._classify_fallback(error_message)
            
        except Exception as e:
            # 例外チェーンを保持して再抜出
            raise RuntimeError(f"Error classification failed: {e}") from e
    
    @classmethod
    def _classify_special_cases(cls, error_message: str, is_bot_detected: bool, is_timeout: bool) -> Optional[str]:
        """特別なケースの分類（Bot検知、タイムアウトなど）"""
        # Bot検知（確実）
        if is_bot_detected or cls.BOT_PATTERN.search(error_message):
            return 'BOT_DETECTED'
        
        # タイムアウト判定
        if is_timeout or 'timeout' in error_message:
            return 'TIMEOUT'
            
        return None
    
    @classmethod
    def _classify_by_patterns(cls, error_message: str) -> Optional[str]:
        """パターンベースの最適化された分類"""
        # 優先度順でパターンをチェック（一回のループで全パターンを効率的に処理）
        for patterns, error_type, priority in cls.ERROR_PATTERN_RULES:
            for pattern in patterns:
                if pattern.search(error_message):
                    return error_type
        return None
    
    @classmethod
    def _refine_pattern_result(cls, pattern_result: str, error_message: str) -> str:
        """パターン結果の細かい分類"""
        if pattern_result == 'EXTERNAL':
            return 'TIMEOUT' if 'timeout' in error_message else 'ACCESS'
        elif pattern_result == 'SUBMIT_BUTTON':
            return cls._classify_submit_button_error(error_message)
        elif pattern_result == 'CONTENT_ANALYSIS':
            return cls._classify_content_analysis_error(error_message)
        else:
            return pattern_result
    
    @classmethod
    def _classify_submit_button_error(cls, error_message: str) -> str:
        """送信ボタンエラーの細かい分類"""
        if 'not found' in error_message or 'selector' not in error_message:
            return 'SUBMIT_BUTTON_NOT_FOUND'
        elif 'selector' in error_message and ('not provided' in error_message or 'missing' in error_message):
            return 'SUBMIT_BUTTON_SELECTOR_MISSING'
        else:
            return 'SUBMIT_BUTTON_ERROR'
    
    @classmethod
    def _classify_content_analysis_error(cls, error_message: str) -> str:
        """コンテンツ分析エラーの細かい分類"""
        if 'error indicators found' in error_message:
            return 'FORM_VALIDATION_ERROR'
        else:
            return 'CONTENT_ANALYSIS_FAILED'
    
    @classmethod
    def _classify_fallback(cls, error_message: str) -> str:
        """フォールバック分類（従来ロジック）"""
        if cls.INSTRUCTION_KEYWORD_PATTERN.search(error_message):
            # より厳密に指示書問題かチェック
            if cls.INSTRUCTION_JSON_PATTERN.search(error_message):
                return 'INSTRUCTION'
            else:
                return 'SYSTEM'  # 曖昧な場合はSYSTEMに分類
        elif cls.ELEMENT_KEYWORD_PATTERN.search(error_message):
            # サイト変更の可能性が高い
            return 'ELEMENT_EXTERNAL'  # 外部要因による要素問題
        elif 'input' in error_message:
            # 入力制限の可能性が高い
            return 'INPUT_EXTERNAL'  # 外部要因による入力問題
        elif 'submit' in error_message:
            return 'SUBMIT'
        elif 'access' in error_message:
            return 'ACCESS'
        else:
            return 'SYSTEM'
    
    @classmethod
    def should_update_instruction_valid(cls, error_type: str) -> bool:
        """
        instruction_valid を更新すべきかどうかの判定（廃止済み）
        
        RuleBasedAnalyzerのリアルタイム解析ではinstruction_validフラグを使用しないため、
        常にFalseを返す
        
        Args:
            error_type: エラータイプ
            
        Returns:
            bool: 常にFalse（instruction_valid更新は不要）
        """
        # RuleBasedAnalyzerリアルタイム解析ではDBのinstruction_validフラグを更新しない
        return False
    
    @classmethod
    def is_recoverable_error(cls, error_type: str, error_message: str) -> bool:
        """
        復旧可能なエラーかどうかの判定（拡張版）
        
        Args:
            error_type: エラータイプ
            error_message: エラーメッセージ
            
        Returns:
            bool: 復旧可能な場合 True
        """
        # 復旧可能なエラータイプ（従来＋新規）
        recoverable_types = [
            'TIMEOUT', 'ACCESS', 'ELEMENT_EXTERNAL', 
            'INPUT_EXTERNAL', 'SYSTEM',
            # 新規追加：外部要因の可能性があるエラー
            'ELEMENT_NOT_FOUND',  # サイト変更の可能性
            'CONTENT_ANALYSIS_FAILED',  # 一時的な問題の可能性
            'SUBMIT_BUTTON_NOT_FOUND'  # ページ変更の可能性
        ]
        
        # 復旧不可能なエラータイプ（構造的問題）
        non_recoverable_types = [
            'INSTRUCTION', 'SUBMIT_BUTTON_SELECTOR_MISSING',
            'SUCCESS_DETERMINATION_FAILED', 'INPUT_TYPE_MISMATCH',
            'FORM_VALIDATION_ERROR', 'BOT_DETECTED',
            # 追加: マッピング/検証起因は自動復旧不可
            'MAPPING', 'VALIDATION_FORMAT', 'CSRF_ERROR', 'DUPLICATE_SUBMISSION'
        ]
        
        if error_type in non_recoverable_types:
            return False
        
        if error_type not in recoverable_types:
            return False
        
        # 特定のエラーメッセージパターンは復旧不可能
        non_recoverable_patterns = [
            'instruction_valid', 'placeholder', 'json decode',
            'invalid selector', 'malformed', 'selector missing',
            'not provided', 'type mismatch', 'validation error'
        ]
        
        if any(pattern in error_message.lower() for pattern in non_recoverable_patterns):
            return False
        
        return True
    
    # フォーム処理段階に特化した分類メソッド（最適化版）
    
    @classmethod
    def classify_form_submission_error(cls, error_message: str, has_url_change: bool = False, 
                                     page_content: str = "", submit_selector: str = "") -> str:
        """
        フォーム送信段階のエラーを詳細分類（最適化版）
        
        Args:
            error_message: エラーメッセージ
            has_url_change: URL変更があったかどうか
            page_content: ページコンテンツ（オプション）
            submit_selector: 送信ボタンセレクタ（オプション）
            
        Returns:
            str: 詳細なエラータイプ
        """
        try:
            # 送信ボタン関連の事前チェック
            if not submit_selector or submit_selector.strip() == "":
                return 'SUBMIT_BUTTON_SELECTOR_MISSING'
            
            # 最適化されたパターンマッチング
            error_message_lower = error_message.lower()
            pattern_result = cls._classify_by_patterns(error_message_lower)
            
            if pattern_result:
                if pattern_result == 'SUBMIT_BUTTON':
                    return 'SUBMIT_BUTTON_NOT_FOUND'
                elif pattern_result == 'CONTENT_ANALYSIS':
                    return cls._classify_content_analysis_error(error_message_lower)
                elif pattern_result in ['SUCCESS_DETERMINATION_FAILED', 'FORM_VALIDATION_ERROR']:
                    return pattern_result

            # 従来の分類にフォールバック
            error_context = {
                'error_message': error_message,
                'error_location': 'form_submission',
                'has_url_change': has_url_change,
                'page_content': page_content,
                'submit_selector': submit_selector
            }
            refined = cls._classify_from_page_content(error_context)
            if refined:
                return refined
            return cls.classify_error_type(error_context)
            
        except Exception as e:
            raise RuntimeError(f"Form submission error classification failed: {e}") from e

    # 追加: ページテキスト/HTMLからの詳細分類（必須/フォーマット/ボット/CSRF/重複など）
    @classmethod
    def _classify_from_page_content(cls, context: Dict[str, Any]) -> Optional[str]:
        try:
            content = (context.get('page_content') or '').lower()
            if not content:
                return None

            # 必須未入力 → MAPPING
            for p in cls.REQUIRED_TEXT_PATTERNS:
                if p.search(content):
                    return 'MAPPING'

            # 形式不正 → VALIDATION_FORMAT
            for p in cls.FORMAT_TEXT_PATTERNS:
                if p.search(content):
                    return 'VALIDATION_FORMAT'

            # reCAPTCHA/Cloudflare → BOT_DETECTED（概要語のみだが相対的に誤判定リスクは低い）
            for p in cls.CAPTCHA_TEXT_PATTERNS:
                if p.search(content):
                    return 'BOT_DETECTED'

            # CSRF/トークン → CSRF_ERROR（近接条件を満たす場合のみ）
            for p in cls.CSRF_NEAR_ERROR_PATTERNS:
                if p.search(content):
                    return 'CSRF_ERROR'

            # 重複送信 → DUPLICATE_SUBMISSION
            for p in cls.DUPLICATE_TEXT_PATTERNS:
                if p.search(content):
                    return 'DUPLICATE_SUBMISSION'

            # HTML的手掛かり
            if 'aria-invalid="true"' in content or 'required' in content:
                return 'FORM_VALIDATION_ERROR'

            return None
        except Exception as e:
            logging.getLogger(__name__).debug(f"Page content classification error: {e}")
            return None
    
    @classmethod
    def classify_form_input_error(cls, error_message: str, field_name: str = "",
                                field_type: str = "", selector: str = "") -> str:
        """
        フィールド入力段階のエラーを詳細分類（最適化版）
        
        Args:
            error_message: エラーメッセージ
            field_name: フィールド名（オプション）
            field_type: フィールドタイプ（オプション）
            selector: セレクタ（オプション）
            
        Returns:
            str: 詳細なエラータイプ
        """
        try:
            error_message_lower = error_message.lower()
            
            # 最適化されたパターンマッチング
            pattern_result = cls._classify_by_patterns(error_message_lower)
            
            if pattern_result in ['ELEMENT_NOT_FOUND', 'INPUT_TYPE_MISMATCH', 'FORM_VALIDATION_ERROR']:
                return pattern_result
            
            # 'not found' の特別チェック
            if 'not found' in error_message_lower:
                return 'ELEMENT_NOT_FOUND'
            
            # 従来の分類にフォールバック
            error_context = {
                'error_message': error_message,
                'error_location': 'form_input',
                'field_name': field_name,
                'field_type': field_type,
                'selector': selector
            }
            return cls.classify_error_type(error_context)
            
        except Exception as e:
            raise RuntimeError(f"Form input error classification failed: {e}") from e
