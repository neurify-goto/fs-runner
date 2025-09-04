
import json
import logging
import os
import time
from typing import List, Dict, Any, Optional

from groq import Groq
from config.manager import get_groq_config

from utils.datetime_utils import utc_to_jst, to_jst_isoformat

logger = logging.getLogger(__name__)


class GroqClient:
    """Groq APIとのやり取りをカプセル化するクラス"""

    def __init__(self):
        """Groq APIクライアント初期化"""
        self.groq_client = None
        self.groq_api_key = os.getenv('GROQ_API_KEY')

        if self.groq_api_key:
            try:
                self.groq_client = Groq(api_key=self.groq_api_key)
                logger.info("Groq APIクライアントを初期化しました")
            except Exception as e:
                logger.error(f"Groq APIクライアント初期化エラー: {e}")
                self.groq_client = None
        else:
            logger.warning("GROQ_API_KEY環境変数が設定されていません")

        # Batch API設定
        self.GROQ_MODEL = "openai/gpt-oss-120b"
        self.GROQ_TEMPERATURE = 0.6
        self.GROQ_TOP_P = 1
        self.GROQ_REASONING_EFFORT = "medium"
        self.GROQ_BATCH_COMPLETION_WINDOW = "24h"
        
        # 設定ファイルからmax_tokensを読み込み
        try:
            groq_config = get_groq_config()
            self.GROQ_MAX_TOKENS = groq_config["max_tokens"]
        except Exception as e:
            logger.warning(f"Groq設定の読み込みに失敗、デフォルト値を使用: {e}")
            self.GROQ_MAX_TOKENS = 4096

    def create_groq_batch_request(self, prompt_ready_results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """プロンプト準備済み企業のGroq真のBatch APIリクエスト処理"""
        if not prompt_ready_results or not self.groq_client:
            return None

        logger.info(f"Groq Batch APIリクエスト処理開始: {len(prompt_ready_results)}件")

        try:
            # 1. JSONLファイル用のバッチリクエスト生成
            batch_requests = []
            skipped_companies = []
            
            for result in prompt_ready_results:
                system_prompt = result.get('system_prompt', '')
                user_prompt = result.get('user_prompt', '')
                record_id = result.get('record_id')
                company_name = result.get('company_name', '')

                if not system_prompt or not user_prompt or not record_id:
                    logger.warning(f"Record ID {record_id}: プロンプトデータが不完全のためスキップ")
                    skipped_companies.append({
                        'record_id': record_id,
                        'company_name': company_name,
                        'error': 'プロンプトデータが不完全'
                    })
                    continue

                # Batch API形式でリクエスト作成
                batch_request = {
                    "custom_id": f"company_{record_id}",
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": self.GROQ_MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "temperature": self.GROQ_TEMPERATURE,
                        "max_completion_tokens": self.GROQ_MAX_TOKENS,
                        "top_p": self.GROQ_TOP_P,
                        "reasoning_effort": self.GROQ_REASONING_EFFORT,
                        "response_format": {"type": "json_object"}
                    }
                }
                batch_requests.append(batch_request)
                logger.debug(f"バッチリクエスト追加: Record ID {record_id}")

            if not batch_requests:
                logger.error("有効なバッチリクエストがありません")
                return {
                    'success': False,
                    'error': '有効なバッチリクエストがありません',
                    'processing_type': 'batch',
                    'company_count': len(prompt_ready_results),
                    'skipped_companies': skipped_companies
                }

            logger.info(f"バッチリクエスト生成完了: {len(batch_requests)}件")

            # 2. JSONLファイル作成とアップロード
            batch_file_path = f"/tmp/groq_batch_{int(time.time())}.jsonl"
            try:
                with open(batch_file_path, 'w', encoding='utf-8') as f:
                    for request in batch_requests:
                        f.write(json.dumps(request, ensure_ascii=False) + '\n')
                        
                logger.info(f"バッチファイル作成完了: {batch_file_path} ({len(batch_requests)}行)")

                # ファイルアップロード
                with open(batch_file_path, 'rb') as f:
                    batch_input_file = self.groq_client.files.create(
                        file=f,
                        purpose="batch"
                    )
                
                logger.info(f"バッチファイルアップロード完了: {batch_input_file.id}")

                # 3. バッチジョブ作成
                batch = self.groq_client.batches.create(
                    input_file_id=batch_input_file.id,
                    endpoint="/v1/chat/completions",
                    completion_window=self.GROQ_BATCH_COMPLETION_WINDOW
                )
                
                logger.info(f"Groq Batch APIジョブ作成完了: {batch.id} (ステータス: {batch.status})")

                # Groq APIから取得した時刻をJSTに変換（エラーハンドリング付き）
                created_at_jst = None
                expires_at_jst = None
                
                try:
                    if batch.created_at:
                        created_at_jst = to_jst_isoformat(batch.created_at)
                    else:
                        logger.warning("batch.created_at is None, using current JST time")
                        from utils.datetime_utils import now_jst
                        created_at_jst = now_jst().isoformat()
                except (ValueError, TypeError) as e:
                    logger.error(f"Failed to convert batch.created_at to JST: {batch.created_at}, error: {e}")
                    # フォールバック: 現在時刻を使用
                    from utils.datetime_utils import now_jst
                    created_at_jst = now_jst().isoformat()
                
                try:
                    if batch.expires_at:
                        expires_at_jst = to_jst_isoformat(batch.expires_at)
                except (ValueError, TypeError) as e:
                    logger.error(f"Failed to convert batch.expires_at to JST: {batch.expires_at}, error: {e}")
                    # expires_atの変換に失敗した場合はNoneのまま

                return {
                    'success': True,
                    'processing_type': 'batch',
                    'batch_id': batch.id,
                    'input_file_id': batch_input_file.id,
                    'status': batch.status,
                    'created_at': created_at_jst,
                    'expires_at': expires_at_jst,
                    'completion_window': self.GROQ_BATCH_COMPLETION_WINDOW,
                    'company_count': len(batch_requests),
                    'skipped_count': len(skipped_companies),
                    'record_ids': [r.get('record_id') for r in prompt_ready_results if r.get('record_id')],
                    'skipped_companies': skipped_companies
                }

            finally:
                # 一時ファイルクリーンアップ
                if os.path.exists(batch_file_path):
                    os.unlink(batch_file_path)
                    logger.debug(f"一時ファイル削除: {batch_file_path}")

        except Exception as e:
            logger.error(f"Groq Batch API処理エラー: {e}")
            return {
                'success': False,
                'processing_type': 'batch',
                'error': str(e),
                'company_count': len(prompt_ready_results),
                'record_ids': [r.get('record_id') for r in prompt_ready_results if r.get('record_id')]
            }
