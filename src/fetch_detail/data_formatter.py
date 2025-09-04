"""
データ整形・バリデーションモジュール

Supabase書き込み用のデータ整形とバリデーション機能を提供
"""

import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


class DataFormatter:
    """データ整形・バリデーションクラス"""
    
    def __init__(self):
        # スクレイピング対象フィールドの定義
        self.scraping_fields = [
            'company_url', 'representative', 'employee_count', 'tel', 
            'postal_code', 'capital', 'established_year', 'established_month',
            'closing_month', 'average_age', 'average_salary', 'national_id'
        ]
    
    def build_update_data(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """スクレイピング結果から更新データを構築"""
        # 全てのオブジェクトが同じキーセットを持つよう、全フィールドを初期化
        update_data = {field: None for field in self.scraping_fields}
        
        # 実際のデータで上書き
        for field in self.scraping_fields:
            if result.get(field):
                update_data[field] = result.get(field)
        
        return update_data
    
    def prepare_batch_updates(self, results_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """バッチ更新用データを準備"""
        batch_updates = []
        
        # 成功したレコードのみを抽出してバッチ処理
        successful_results = [r for r in results_data if r.get('status') == 'success' and r.get('record_id')]
        
        if not successful_results:
            logger.info("更新すべき成功レコードがありません")
            return batch_updates
        
        # バッチ更新用データを構築
        for result in successful_results:
            record_id = result.get('record_id')
            update_data = self.build_update_data(result)
            
            if update_data:
                update_data['id'] = record_id  # upsertで必要
                batch_updates.append(update_data)
        
        logger.info(f"バッチ更新データ準備完了: {len(batch_updates)}件")
        return batch_updates
    
    def validate_processing_results(self, results_data: Dict[str, Any]) -> bool:
        """処理結果データの妥当性検証"""
        try:
            # 必要なキーが存在するかチェック
            required_keys = ['results', 'total_successful', 'total_failed']
            for key in required_keys:
                if key not in results_data:
                    logger.error(f"必要なキー '{key}' が結果データに含まれていません")
                    return False
            
            # 結果リストが空でないかチェック
            results_list = results_data.get('results', [])
            if not isinstance(results_list, list):
                logger.error("results は配列である必要があります")
                return False
            
            if not results_list:
                logger.warning("結果リストが空です")
                return True  # 空でも有効
            
            # 各結果レコードの構造チェック
            for i, result in enumerate(results_list):
                if not isinstance(result, dict):
                    logger.error(f"結果レコード {i} は辞書である必要があります")
                    return False
                
                # 必要なフィールドの存在チェック
                if 'status' not in result:
                    logger.error(f"結果レコード {i} に 'status' フィールドがありません")
                    return False
                
                # 成功レコードには record_id が必要
                if result.get('status') == 'success' and not result.get('record_id'):
                    logger.warning(f"成功レコード {i} に 'record_id' がありません")
            
            logger.info("処理結果データの検証完了")
            return True
            
        except Exception as e:
            logger.error(f"結果データ検証エラー: {e}")
            return False
    
    def create_processing_log_data(self, batch_id: str, status: str, stats: Dict[str, Any]) -> Dict[str, Any]:
        """処理ログデータを作成"""
        from datetime import datetime
        
        log_data = {
            'batch_id': batch_id,
            'status': status,
            'successful_count': stats.get('total_successful', 0),
            'failed_count': stats.get('total_failed', 0),
            'execution_time': stats.get('execution_time', 0),
            'processed_at': datetime.utcnow().isoformat(),
            'worker_type': 'github_actions'
        }
        
        return log_data
    
    def normalize_field_values(self, update_data: Dict[str, Any]) -> Dict[str, Any]:
        """フィールド値の正規化処理"""
        normalized_data = update_data.copy()
        
        try:
            # 空文字列をNoneに変換
            for key, value in normalized_data.items():
                if isinstance(value, str) and value.strip() == '':
                    normalized_data[key] = None
                elif value in ['-', 'N/A', 'null', 'None']:
                    normalized_data[key] = None
            
            # 数値型フィールドの処理
            numeric_fields = ['average_age', 'average_salary', 'employee_count', 'capital']
            for field in numeric_fields:
                if field in normalized_data and normalized_data[field] is not None:
                    try:
                        # 文字列の場合は数値に変換を試行
                        if isinstance(normalized_data[field], str):
                            # カンマを除去
                            clean_value = normalized_data[field].replace(',', '')
                            # 数値部分のみ抽出
                            import re
                            match = re.search(r'(\d+)', clean_value)
                            if match:
                                normalized_data[field] = int(match.group(1))
                            else:
                                normalized_data[field] = None
                    except (ValueError, AttributeError):
                        normalized_data[field] = None
            
            logger.debug("フィールド値正規化完了")
            
        except Exception as e:
            logger.warning(f"フィールド値正規化エラー: {e}")
        
        return normalized_data