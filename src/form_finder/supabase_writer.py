"""
Supabase Writer for Form Finder (GitHub Actions)
Form Finder処理結果をSupabaseに書き込むためのユーティリティ
"""

import json
import logging
import os
# datetime 未使用：不要インポートを削除
from typing import Dict, List, Any

from supabase import create_client, Client

logger = logging.getLogger(__name__)


class SupabaseFormFinderWriter:
    """Supabase書き込み管理クラス（Form Finder用）"""
    
    def _sanitize_error_message(self, error_msg: str) -> str:
        """エラーメッセージから機密情報を除去（セキュリティ対策）"""
        import re
        # URLやキー、パスワードらしき文字列を除去
        sanitized = re.sub(r'https?://[^\s]+', '[URL_REMOVED]', error_msg)
        sanitized = re.sub(r'key[_-]?[a-zA-Z0-9]{20,}', '[KEY_REMOVED]', sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r'token[_-]?[a-zA-Z0-9]{20,}', '[TOKEN_REMOVED]', sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r'/[a-zA-Z0-9_/-]{20,}', '[PATH_REMOVED]', sanitized)
        return sanitized

    
    def __init__(self, supabase_url: str, supabase_key: str, target_table: str = 'companies'):
        """
        Supabaseクライアント初期化
        
        Args:
            supabase_url: SupabaseプロジェクトURL
            supabase_key: Supabaseサービスロールキー
        """
        try:
            # シンプルな初期化（余計なオプションを避ける）
            logger.info("Supabase初期化開始")
            self.supabase: Client = create_client(supabase_url, supabase_key)
            self.target_table: str = (target_table or 'companies').strip()
            if self.target_table not in ('companies', 'companies_extra'):
                logger.warning(f"Unknown target_table '{self.target_table}', fallback to 'companies'")
                self.target_table = 'companies'
            logger.info("Supabaseクライアント初期化完了")
        except Exception as e:
            # セキュリティ: エラーメッセージから機密情報を除去
            sanitized_error = self._sanitize_error_message(str(e))
            logger.error(f"Supabaseクライアント初期化エラー: {sanitized_error}")
            logger.error(f"エラータイプ: {type(e).__name__}")
            logger.error("URLまたはキーの設定エラー")
            raise
    
    def save_form_finder_results(self, batch_id: str, results_data: List[Dict[str, Any]], status: str) -> bool:
        """
        Form Finder処理結果をSupabaseに効率的なバッチ処理で保存
        
        Args:
            batch_id: バッチID
            results_data: 処理結果データ
            status: 処理ステータス ('success', 'failure')
            
        Returns:
            保存成功可否
        """
        try:
            # 成功判定を厳格化: form_urlsが空または無効な場合は失敗として扱う
            truly_successful_results = []
            failed_results = []
            
            for result in results_data:
                if not result.get('record_id'):
                    continue
                    
                # まずstatusをチェック
                if result.get('status') == 'failed':
                    failed_results.append(result)
                    continue
                
                # statusが'success'でもform_urlsが有効でない場合は失敗扱い
                if result.get('status') == 'success':
                    form_urls = result.get('form_urls', [])
                    form_url = form_urls[0] if form_urls else None
                    
                    # URL妥当性をチェック
                    has_valid_url = False
                    if form_url:
                        from .utils import is_valid_form_url
                        form_url_str = str(form_url).strip()
                        if is_valid_form_url(form_url_str) and len(form_url_str) <= 2048:
                            has_valid_url = True
                    
                    if has_valid_url:
                        truly_successful_results.append(result)
                        logger.debug(f"record_id={result.get('record_id')}: 真の成功として分類")
                    else:
                        # form_urlが無効なので失敗として再分類
                        failed_result = result.copy()
                        failed_result['status'] = 'failed'
                        failed_results.append(failed_result)
                        logger.warning(f"record_id={result.get('record_id')}: form_url無効により失敗として再分類")
            
            successful_results = truly_successful_results
            
            logger.info(f"バッチ処理開始: 成功={len(successful_results)}件, 失敗={len(failed_results)}件")
            
            # 効率的なバッチ処理: 成功と失敗を分けて処理
            success_updated = 0
            failure_updated = 0
            
            # 成功結果の一括更新
            if successful_results:
                success_updated = self._batch_update_success_results(successful_results)
                
            # 失敗結果の一括更新  
            if failed_results:
                failure_updated = self._batch_update_failure_results(failed_results)
            
            total_updated = success_updated + failure_updated
            logger.info(f"バッチ更新完了: 合計{total_updated}件更新 (成功={success_updated}, 失敗={failure_updated})")
            
            return total_updated > 0
            
        except Exception as e:
            logger.error(f"Form Finder結果保存エラー: {e}")
            return False
    
    def _batch_update_success_results(self, successful_results: List[Dict[str, Any]]) -> int:
        """成功結果の効率的なバッチ更新（整合性強化版）"""
        try:
            # companies以外（例: companies_extra）はRPC未対応のためフォールバックを使用
            if self.target_table != 'companies':
                logger.info(f"RPC未対応テーブルのためフォールバック更新を使用 (table={self.target_table})")
                return self._fallback_update_success_results(successful_results)
            # レコードIDリストを構築（整数のみを許可）
            record_ids = []
            form_url_mapping = {}
            
            for result in successful_results:
                record_id = result.get('record_id')
                # セキュリティ: record_idの型検証
                if not isinstance(record_id, int) or record_id <= 0:
                    logger.warning(f"不正なrecord_id: {record_id}")
                    continue
                
                # フォームURLの安全な処理
                form_urls = result.get('form_urls', [])
                form_url = form_urls[0] if form_urls else None
                
                # 整合性チェック: 有効なform_urlが存在するかを厳密に検証
                has_valid_form_url = False
                if form_url:
                    form_url_str = str(form_url).strip()
                    
                    # 統一化されたURL妥当性検証
                    from .utils import is_valid_form_url
                    if is_valid_form_url(form_url_str):
                        # SQLインジェクション対策: 単一引用符をエスケープ
                        form_url_sanitized = form_url_str.replace("'", "''")
                        # 長さ制限（URLとして妥当な範囲）
                        if len(form_url_sanitized) <= 2048:
                            form_url_mapping[record_id] = form_url_sanitized
                            has_valid_form_url = True
                        else:
                            logger.warning(f"form_urlが長すぎます (record_id={record_id}): {len(form_url_sanitized)}文字")
                    else:
                        logger.warning(f"無効なform_urlを検出 (record_id={record_id}): {form_url_str}")
                
                # 整合性保証: 有効なform_urlが存在する場合のみ成功として処理
                if has_valid_form_url:
                    record_ids.append(record_id)
                    logger.debug(f"record_id={record_id}: 有効なform_urlで成功処理")
                # 注意: 無効なform_urlの場合は成功処理リストに追加しない
                # これらのレコードは failed_results として処理される必要がある
            
            if not record_ids:
                logger.warning("有効なrecord_idが見つかりません")
                return 0
            
            # RPC関数で効率的な一括更新
            result = self.supabase.rpc('bulk_update_form_finder_success', {
                'record_ids': record_ids,
                'form_url_mapping': form_url_mapping
            }).execute()
            
            # 改善されたエラーハンドリング
            if result.data is not None:
                # より堅牢な戻り値処理
                updated_count = result.data if isinstance(result.data, int) else (
                    len(result.data) if isinstance(result.data, list) else 0
                )
                logger.info(f"成功結果バッチ更新完了: {updated_count}件")
                return updated_count
            else:
                logger.warning("成功結果バッチ更新: レスポンスデータが空")
                return 0
                
        except Exception as e:
            logger.warning(f"RPC関数による成功結果バッチ更新失敗、フォールバック処理実行: {e}")
            return self._fallback_update_success_results(successful_results)
    
    def _batch_update_failure_results(self, failed_results: List[Dict[str, Any]]) -> int:
        """失敗結果の効率的なバッチ更新"""
        try:
            if self.target_table != 'companies':
                logger.info(f"RPC未対応テーブルのためフォールバック更新を使用 (table={self.target_table})")
                return self._fallback_update_failure_results(failed_results)
            # レコードIDリスト作成（整数のみを許可）
            record_ids = []
            
            for result in failed_results:
                record_id = result.get('record_id')
                # セキュリティ: record_idの型検証
                if not isinstance(record_id, int) or record_id <= 0:
                    logger.warning(f"不正なrecord_id: {record_id}")
                    continue
                record_ids.append(record_id)
            
            if not record_ids:
                logger.warning("有効なrecord_idが見つかりません")
                return 0
            
            # RPC関数で効率的な一括更新
            result = self.supabase.rpc('bulk_update_form_finder_failure', {
                'record_ids': record_ids
            }).execute()
            
            # 改善されたエラーハンドリング
            if result.data is not None:
                # より堅牢な戻り値処理
                updated_count = result.data if isinstance(result.data, int) else (
                    len(result.data) if isinstance(result.data, list) else 0
                )
                logger.info(f"失敗結果バッチ更新完了: {updated_count}件")
                return updated_count
            else:
                logger.warning("失敗結果バッチ更新: レスポンスデータが空")
                return 0
                
        except Exception as e:
            logger.warning(f"RPC関数による失敗結果バッチ更新失敗、フォールバック処理実行: {e}")
            return self._fallback_update_failure_results(failed_results)
    
    def _fallback_update_success_results(self, successful_results: List[Dict[str, Any]]) -> int:
        """成功結果のフォールバック更新（従来のupsert方式）"""
        try:
            batch_updates = []
            for result in successful_results:
                record_id = result.get('record_id')
                form_urls = result.get('form_urls', [])
                primary_form_url = form_urls[0] if form_urls else None
                
                batch_updates.append({
                    'id': record_id,
                    'form_found': True,
                    'form_url': primary_form_url
                })
            
            response = self.supabase.table(self.target_table).upsert(
                batch_updates,
                on_conflict='id'
            ).execute()
            
            return len(response.data) if response.data else 0
            
        except Exception as e:
            logger.error(f"成功結果フォールバック更新エラー: {e}")
            return 0
    
    def _fallback_update_failure_results(self, failed_results: List[Dict[str, Any]]) -> int:
        """失敗結果のフォールバック更新（従来のupsert方式）"""
        try:
            batch_updates = []
            for result in failed_results:
                record_id = result.get('record_id')
                batch_updates.append({
                    'id': record_id,
                    'form_found': False,
                    'form_url': None
                })
            
            response = self.supabase.table(self.target_table).upsert(
                batch_updates,
                on_conflict='id'
            ).execute()
            
            return len(response.data) if response.data else 0
            
        except Exception as e:
            logger.error(f"失敗結果フォールバック更新エラー: {e}")
            return 0
    
    def _build_success_update_data(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """成功時の更新データを構築"""
        form_urls = result.get('form_urls', [])
        primary_form_url = form_urls[0] if form_urls else None
        
        update_data = {
            'form_found': True,
            'form_url': primary_form_url  # 最初のフォームURLを保存
        }
        
        return update_data
    
    def _build_failure_update_data(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """失敗時の更新データを構築"""
        update_data = {
            'form_found': False,
            'form_url': None
        }
        
        return update_data
    


def load_results_file(file_path: str) -> Dict[str, Any]:
    """
    結果ファイルを読み込み
    
    Args:
        file_path: 結果ファイルパス
        
    Returns:
        結果データ
    """
    try:
        if not os.path.exists(file_path):
            logger.error(f"結果ファイルが見つかりません: {file_path}")
            return {}
            
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        logger.info("結果ファイル読み込み完了")
        return data
        
    except Exception as e:
        logger.error(f"結果ファイル読み込みエラー: {e}")
        return {}
