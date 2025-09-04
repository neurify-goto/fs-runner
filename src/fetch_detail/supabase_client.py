"""
Supabaseクライアント管理モジュール

Supabaseへの接続とデータベース操作機能を提供
"""

import logging
from typing import Dict, List, Any, Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)


class SupabaseClientManager:
    """Supabaseクライアント管理クラス"""
    
    def __init__(self, supabase_url: str, supabase_key: str):
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
            logger.info("Supabaseクライアント初期化完了")
        except Exception as e:
            logger.error(f"Supabaseクライアント初期化エラー: {e}")
            logger.error(f"エラータイプ: {type(e).__name__}")
            logger.error("URLまたはキーの設定エラー")
            
            # より詳細なエラー情報を取得
            import traceback
            logger.error(f"スタックトレース: {traceback.format_exc()}")
            raise
    
    def batch_upsert_companies(self, batch_updates: List[Dict[str, Any]]) -> bool:
        """企業データの一括更新（信頼性向上版）"""
        try:
            if not batch_updates:
                logger.info("更新データが空のため、処理をスキップします")
                return True
            
            logger.info(f"バッチ更新開始: {len(batch_updates)}件")
            
            # トランザクション的なバッチ更新を試行
            max_retries = 3
            chunk_size = 50  # チャンクサイズを制限してエラー率を下げる
            
            for attempt in range(max_retries):
                try:
                    success_count = 0
                    
                    # データを小さなチャンクに分割してバッチ処理
                    for i in range(0, len(batch_updates), chunk_size):
                        chunk = batch_updates[i:i + chunk_size]
                        
                        logger.info(f"チャンク処理 {i//chunk_size + 1}: {len(chunk)}件")
                        
                        response = self.supabase.table('companies').upsert(
                            chunk,
                            on_conflict='id'  # idカラムでの衝突時は更新
                        ).execute()
                        
                        chunk_count = len(response.data) if response.data else 0
                        success_count += chunk_count
                        
                        logger.info(f"チャンク更新成功: {chunk_count}件")
                    
                    if success_count == len(batch_updates):
                        logger.info(f"バッチ更新完全成功: {success_count}件のレコードを更新しました")
                        return True
                    else:
                        logger.info(f"バッチ更新部分成功: {success_count}/{len(batch_updates)}件")
                        
                        # 完全成功でない場合はリトライ
                        if attempt < max_retries - 1:
                            logger.info(f"バッチ更新リトライ {attempt + 1}/{max_retries}")
                            continue
                        else:
                            # 最終試行での部分成功も受け入れる（70%以上で成功とみなす）
                            success_rate = success_count / len(batch_updates)
                            logger.info(f"最終バッチ更新結果: 成功率{success_rate*100:.1f}%")
                            return success_rate >= 0.7
                    
                except Exception as chunk_error:
                    logger.error(f"バッチ更新エラー (試行 {attempt + 1}): {chunk_error}")
                    
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(2 ** attempt)  # 指数バックオフ
                        continue
                    else:
                        return False
            
            return False
            
        except Exception as e:
            logger.error(f"バッチ更新致命的エラー: {e}")
            return False
    
    def individual_update_companies(self, batch_updates: List[Dict[str, Any]]) -> tuple[int, int]:
        """個別更新フォールバック（最小化版）"""
        success_count = 0
        failure_count = 0
        
        logger.info(f"個別更新フォールバック開始: {len(batch_updates)}件")
        
        # 失敗したレコードのみを特定するため、まず一括確認
        failed_records = self._identify_failed_records(batch_updates)
        
        if not failed_records:
            logger.info("すべてのレコードが正常に処理済み、個別更新不要")
            return len(batch_updates), 0
        
        logger.info(f"個別更新対象: {len(failed_records)}件（全体の{len(failed_records)/len(batch_updates)*100:.1f}%）")
        
        # 失敗レコードのみを個別更新
        for update_data in failed_records:
            try:
                record_id = update_data.get('id')
                if not record_id:
                    failure_count += 1
                    continue
                
                # idフィールドを除去してupdate用データを準備
                clean_data = {k: v for k, v in update_data.items() if k != 'id'}
                
                response = self.supabase.table('companies').update(
                    clean_data
                ).eq('id', record_id).execute()
                
                if response.data and len(response.data) > 0:
                    success_count += 1
                else:
                    failure_count += 1
                    logger.warning(f"更新対象が見つかりません: record_id={record_id}")
                    
            except Exception as individual_error:
                failure_count += 1
                logger.error(f"個別更新エラー (record_id={record_id}): {individual_error}")
        
        # 全体の成功数 = バッチ更新で成功した数 + 個別更新で成功した数
        total_success = len(batch_updates) - len(failed_records) + success_count
        logger.info(f"個別更新結果: 個別成功={success_count}, 個別失敗={failure_count}, 全体成功={total_success}")
        return total_success, failure_count
    
    def _identify_failed_records(self, batch_updates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """バッチ更新で失敗したレコードを特定（具体化版）"""
        try:
            failed_records = []
            
            # バッチ更新後の実際の結果を確認
            for update_data in batch_updates:
                record_id = update_data.get('id')
                if not record_id:
                    failed_records.append(update_data)
                    continue
                
                try:
                    # 実際に更新されたかを確認
                    response = self.supabase.table('companies').select('id,company_url').eq('id', record_id).execute()
                    
                    if not response.data:
                        # レコードが存在しない場合は失敗
                        failed_records.append(update_data)
                        continue
                    
                    # 更新が期待通りに行われたかを確認
                    current_data = response.data[0]
                    expected_company_url = update_data.get('company_url')
                    
                    if expected_company_url and current_data.get('company_url') != expected_company_url:
                        # 期待した値に更新されていない場合は失敗扱い
                        failed_records.append(update_data)
                    
                except Exception as check_error:
                    logger.warning(f"レコード確認エラー (record_id={record_id}): {check_error}")
                    # 確認できない場合は安全のため失敗扱い
                    failed_records.append(update_data)
            
            success_rate = ((len(batch_updates) - len(failed_records)) / len(batch_updates)) * 100
            logger.info(f"バッチ更新結果確認: 成功率{success_rate:.1f}%, 失敗レコード{len(failed_records)}件")
            
            return failed_records
            
        except Exception as e:
            logger.error(f"失敗レコード特定エラー: {e}")
            # エラー時は保守的に全レコードを対象とする
            return batch_updates
    
    
    def test_connection(self) -> bool:
        """接続テスト"""
        try:
            # 簡単なクエリを実行して接続を確認
            response = self.supabase.table('companies').select('id').limit(1).execute()
            logger.info("Supabase接続テスト成功")
            return True
        except Exception as e:
            logger.error(f"Supabase接続テスト失敗: {e}")
            return False