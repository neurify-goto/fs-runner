#!/usr/bin/env python3
"""
Supabase Form Analyzer Writer

フォーム解析処理結果をSupabaseに書き込むクラス。
GitHub ActionsのWorkflow内で実行され、処理結果をデータベースに保存する。
修正済み：companiesテーブル3フィールドのみ、batch_requestテーブル使用。
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, Any, List, Optional

from supabase import create_client, Client

from utils.datetime_utils import now_jst, to_jst_isoformat

logger = logging.getLogger(__name__)


class SupabaseFormAnalyzerWriter:
    """フォーム解析結果のSupabase書き込みクラス（修正版）"""
    
    def __init__(self):
        self.supabase_url = os.getenv('SUPABASE_URL')
        self.supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
        
        if not self.supabase_url or not self.supabase_key:
            raise ValueError("SUPABASE_URL および SUPABASE_SERVICE_ROLE_KEY 環境変数が必要です")
        
        self.supabase: Client = create_client(self.supabase_url, self.supabase_key)
        logger.info("Supabaseクライアントを初期化しました")
    
    def load_results_file(self, results_file: str) -> Dict[str, Any]:
        """処理結果ファイルを読み込み"""
        try:
            with open(results_file, 'r', encoding='utf-8') as f:
                results = json.load(f)
            logger.info(f"処理結果ファイルを読み込みました: {results_file}")
            return results
        except Exception as e:
            logger.error(f"処理結果ファイル読み込みエラー: {e}")
            raise
    
    async def update_companies_form_analyzer_status(self, results: List[Dict[str, Any]], groq_batch_result: Optional[Dict[str, Any]] = None) -> None:
        """companiesテーブルのバルク更新（I/O負荷軽減版）"""
        try:
            if not results:
                logger.warning("更新対象の結果がありません")
                return
            
            # バルク更新用のデータ配列を構築
            update_records = []
            
            for result in results:
                record_id = result.get('record_id')
                status = result.get('status')
                prohibition_detected = result.get('prohibition_detected', False)
                prohibition_phrases = result.get('prohibition_phrases', [])
                
                if not record_id:
                    logger.warning("record_idが見つからないため、スキップします")
                    continue
                
                # 更新データの準備（3フィールドのみ）
                update_data = {
                    'id': record_id,  # upsert用のID
                    'prohibition_detected': prohibition_detected
                }
                
                # prohibition_candidates: 検出された場合のみ書き込み
                if prohibition_detected and prohibition_phrases:
                    update_data['prohibition_candidates'] = json.dumps(prohibition_phrases, ensure_ascii=False)
                
                # batch_id: Groq Batch APIに送信したもののみ書き込み
                if (status == 'success' and 
                    not prohibition_detected and 
                    result.get('system_prompt') and 
                    result.get('user_prompt') and 
                    groq_batch_result and 
                    groq_batch_result.get('success') and
                    groq_batch_result.get('batch_id')):
                    update_data['batch_id'] = groq_batch_result['batch_id']
                
                update_records.append(update_data)
            
            if not update_records:
                logger.warning("バルク更新対象レコードがありません")
                return
            
            # バルク更新を実行（upsert形式）
            logger.info(f"companiesテーブルバルク更新開始: {len(update_records)}件")
            
            # バッチサイズで分割して処理（Supabase API制限対策）
            batch_size = 100
            updated_count = 0
            error_count = 0
            
            for i in range(0, len(update_records), batch_size):
                batch = update_records[i:i + batch_size]
                
                try:
                    # upsert操作でバルク更新
                    response = self.supabase.table('companies').upsert(
                        batch, 
                        on_conflict='id',
                        ignore_duplicates=False
                    ).execute()
                    
                    if response.data:
                        updated_count += len(batch)
                        logger.debug(f"バッチ {i // batch_size + 1}: {len(batch)}件更新完了")
                    else:
                        error_count += len(batch)
                        logger.warning(f"バッチ {i // batch_size + 1}: 更新レスポンスが空です")
                
                except Exception as e:
                    error_count += len(batch)
                    logger.error(f"バッチ {i // batch_size + 1} 更新エラー: {e}")
            
            logger.info(f"企業ステータスバルク更新完了: 成功={updated_count}, 失敗={error_count}")
            
        except Exception as e:
            logger.error(f"企業ステータス一括更新エラー: {e}")
            raise
    
    async def create_batch_request_record(self, batch_id: str, groq_batch_result: Dict[str, Any], record_ids: List[int]) -> None:
        """batch_requestテーブルにレコード作成（実際のスキーマに合わせて修正）"""
        try:
            # batch_requestテーブルの実際のスキーマに準拠したデータを作成
            batch_request_data = {
                'batch_id': batch_id,
                'company_ids': json.dumps(record_ids, ensure_ascii=False),  # JSON文字列として保存
                'requested': True,  # バッチリクエストが送信されたことを示す
                'created_at': to_jst_isoformat(now_jst()),  # 日本時間のcreated_atをISO形式で明示的に設定
                # 'completed': 書き込みなし（結果取得処理時にTrueを設定）
            }
            
            # batch_requestテーブルに挿入
            response = self.supabase.table('batch_request').insert(batch_request_data).execute()
            
            if response.data:
                logger.info(f"batch_requestレコード作成完了: batch_id={batch_id}")
            else:
                logger.warning(f"batch_requestレコード作成レスポンスが空です: batch_id={batch_id}")
            
        except Exception as e:
            logger.error(f"batch_requestレコード作成エラー: batch_id={batch_id}, エラー={e}")
            raise
    
    async def write_results(self, batch_id: str, results_file: str, status: str) -> Dict[str, Any]:
        """処理結果をSupabaseに書き込み（統合・トランザクション版）"""
        try:
            logger.info(f"フォーム解析結果のSupabase書き込み開始: batch_id={batch_id}")
            
            # 処理結果ファイルを読み込み
            results_data = self.load_results_file(results_file)
            
            # 結果から必要な情報を抽出
            results_list = results_data.get('results', [])
            groq_batch_result = results_data.get('groq_batch_result')
            
            if not results_list:
                logger.warning("処理結果が空のため、書き込み処理をスキップします")
                return {
                    'success': True,
                    'message': '処理結果が空のため、書き込み処理をスキップしました',
                    'companies_updated': 0
                }
            
            # 統合書き込み処理（エラーハンドリング改善）
            companies_updated = 0
            batch_created = False
            errors = []
            
            try:
                # Step 1: companiesテーブルの更新（バルク処理）
                await self.update_companies_form_analyzer_status(results_list, groq_batch_result)
                companies_updated = len(results_list)
                logger.info(f"companiesテーブル更新完了: {companies_updated}件")
                
            except Exception as e:
                error_msg = f"companiesテーブル更新失敗: {e}"
                logger.error(error_msg)
                errors.append(error_msg)
            
            try:
                # Step 2: batch_requestテーブルのレコード作成（Groq Batch API成功時のみ）
                if groq_batch_result and groq_batch_result.get('success') and groq_batch_result.get('batch_id'):
                    record_ids = groq_batch_result.get('record_ids', [])
                    await self.create_batch_request_record(
                        groq_batch_result['batch_id'], 
                        groq_batch_result, 
                        record_ids
                    )
                    batch_created = True
                    logger.info("batch_requestレコード作成完了")
                    
            except Exception as e:
                error_msg = f"batch_requestテーブル書き込み失敗: {e}"
                logger.error(error_msg)
                errors.append(error_msg)
                # batch_request失敗でも処理は継続（companies更新は重要）
            
            # 結果の判定
            success = len(errors) == 0
            if success:
                logger.info(f"フォーム解析結果のSupabase書き込み完了: batch_id={batch_id}")
                message = 'フォーム解析結果の書き込みが完了しました'
            else:
                logger.warning(f"フォーム解析結果の書き込みで一部エラーが発生: {errors}")
                message = f'一部エラーが発生しましたが処理を継続しました: {", ".join(errors)}'
            
            return {
                'success': success,
                'message': message,
                'companies_updated': companies_updated,
                'batch_created': batch_created,
                'errors': errors if errors else None
            }
            
        except Exception as e:
            logger.error(f"フォーム解析結果書き込み全体エラー: {e}")
            return {
                'success': False,
                'error': str(e),
                'companies_updated': 0,
                'batch_created': False
            }