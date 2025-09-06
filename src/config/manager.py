"""設定ファイル読み込みと管理を行うユーティリティモジュール"""

import json
import os
from typing import Dict, Any, Optional
from pathlib import Path


class ConfigManager:
    """設定ファイルの読み込みと管理を行うクラス"""
    
    def __init__(self):
        self.config_dir = Path(__file__).parent.parent.parent / "config"
        self._worker_config: Optional[Dict[str, Any]] = None
        self._retry_config: Optional[Dict[str, Any]] = None
    
    def get_worker_config(self) -> Dict[str, Any]:
        """Worker設定を取得"""
        if self._worker_config is None:
            self._worker_config = self._load_config("worker_config.json")
        return self._worker_config
    
    def get_retry_config(self) -> Dict[str, Any]:
        """リトライ設定を取得"""
        if self._retry_config is None:
            self._retry_config = self._load_config("retry_config.json")
        return self._retry_config
    
    def get_retry_setting(self, operation_type: str) -> Dict[str, Any]:
        """特定の操作タイプのリトライ設定を取得
        
        Args:
            operation_type: 操作タイプ (network_operations, api_operations, 
                          database_operations, form_analysis)
        
        Returns:
            指定されたタイプのリトライ設定辞書
        """
        retry_config = self.get_retry_config()
        if operation_type in retry_config["retry_configurations"]:
            return retry_config["retry_configurations"][operation_type]
        
        # デフォルトはnetwork_operationsの設定を使用
        return retry_config["retry_configurations"]["network_operations"]
    
    def get_timeout_setting(self, setting_name: str) -> int:
        """タイムアウト設定を取得
        
        Args:
            setting_name: 設定名 (form_page_load, groq_api_request, 
                         supabase_operation, browser_initialization)
        
        Returns:
            タイムアウト値（ミリ秒）
        """
        retry_config = self.get_retry_config()
        timeout_settings = retry_config["timeout_settings"]
        
        if setting_name in timeout_settings:
            return timeout_settings[setting_name]
        
        # デフォルトタイムアウト
        return 30000
    
    def get_circuit_breaker_config(self) -> Dict[str, Any]:
        """サーキットブレーカー設定を取得"""
        retry_config = self.get_retry_config()
        return retry_config["circuit_breaker"]
    
    def get_form_sender_config(self) -> Dict[str, Any]:
        """フォーム送信設定を取得"""
        worker_config = self.get_worker_config()
        return worker_config["form_sender"]

    def get_cookie_consent_config(self) -> Dict[str, Any]:
        """Cookie同意処理の設定を取得"""
        return self._load_config("cookie_consent.json")
    
    def get_privacy_consent_config(self) -> Dict[str, Any]:
        """プライバシー同意チェック処理の設定を取得"""
        return self._load_config("consent_agreement.json")

    def get_prefectures(self) -> Dict[str, Any]:
        """都道府県名リストを取得"""
        return self._load_config("prefectures.json")
    
    def get_form_explorer_config(self) -> Dict[str, Any]:
        """フォーム探索設定を取得"""
        worker_config = self.get_worker_config()
        return worker_config["form_explorer"]
    
    def get_database_config(self) -> Dict[str, Any]:
        """データベース設定を取得"""
        worker_config = self.get_worker_config()
        return worker_config["database"]
    
    def get_groq_config(self) -> Dict[str, Any]:
        """Groq API設定を取得"""
        worker_config = self.get_worker_config()
        return worker_config["groq"]
    
    def _load_config(self, filename: str) -> Dict[str, Any]:
        """設定ファイルを読み込み"""
        config_path = self.config_dir / filename
        
        if not config_path.exists():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {config_path}")
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"設定ファイルの形式が不正です ({filename}): {e}")
        except Exception as e:
            raise RuntimeError(f"設定ファイルの読み込みに失敗しました ({filename}): {e}")


# グローバルな設定マネージャーインスタンス
config_manager = ConfigManager()


def get_worker_config() -> Dict[str, Any]:
    """Worker設定を取得する便利関数"""
    return config_manager.get_worker_config()


def get_retry_config_for(operation_type: str) -> Dict[str, Any]:
    """特定操作のリトライ設定を取得する便利関数"""
    return config_manager.get_retry_setting(operation_type)


def get_timeout_for(setting_name: str) -> int:
    """タイムアウト設定を取得する便利関数"""
    return config_manager.get_timeout_setting(setting_name)


def get_circuit_breaker_config() -> Dict[str, Any]:
    """サーキットブレーカー設定を取得する便利関数"""
    return config_manager.get_circuit_breaker_config()


def get_form_sender_config() -> Dict[str, Any]:
    """フォーム送信設定を取得する便利関数"""
    return config_manager.get_form_sender_config()


def get_form_explorer_config() -> Dict[str, Any]:
    """フォーム探索設定を取得する便利関数"""
    return config_manager.get_form_explorer_config()


def get_database_config() -> Dict[str, Any]:
    """データベース設定を取得する便利関数"""
    return config_manager.get_database_config()


def get_groq_config() -> Dict[str, Any]:
    """Groq API設定を取得する便利関数"""
    return config_manager.get_groq_config()

def get_cookie_consent_config() -> Dict[str, Any]:
    """Cookie同意処理設定を取得する便利関数"""
    return config_manager.get_cookie_consent_config()

def get_privacy_consent_config() -> Dict[str, Any]:
    """プライバシー同意チェック処理設定を取得する便利関数"""
    return config_manager.get_privacy_consent_config()

def get_prefectures() -> Dict[str, Any]:
    """都道府県名リストを取得する便利関数"""
    return config_manager.get_prefectures()
