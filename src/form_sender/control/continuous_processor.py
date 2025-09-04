"""
連続処理制御

5時間制限、営業時間、日次送信数制限等の制御機能
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List

from supabase import create_client

from ..database.manager import DatabaseManager
from ..template.company_processor import CompanyPlaceholderAnalyzer

logger = logging.getLogger(__name__)


class ContinuousProcessController:
    """連続処理制御クラス（新アーキテクチャ用）"""
    
    def __init__(self, targeting_id: int, max_execution_time: int = 5 * 60 * 60):
        self.targeting_id = targeting_id
        self.max_execution_time = max_execution_time  # 5時間制限
        self.start_time = time.time()
        self.processed_count = 0
        self.success_count = 0
        self.failed_count = 0
        self.supabase_client = None
        self.db_manager = None
        
        # FORM_SENDER.md 1.4.1仕様準拠: 大量取得+一時ファイル管理方式
        self.temp_companies_file = None
        self.companies_buffer = []  # 一時ファイルから読み込んだ企業バッファ
        
        # 動的MAX_COMPANY_IDキャッシュ
        self._max_record_id_cache = None
        self._max_record_id_cache_time = 0
        self._max_record_id_cache_duration = 24 * 60 * 60  # 24時間キャッシュ
        
        # 設定値キャッシュ（パフォーマンス向上用）
        self._validated_config_cache = None
        self._config_cache_key = None
        
    def initialize_supabase(self):
        """Supabase初期化"""
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
        
        if not supabase_url or not supabase_key:
            raise ValueError("Supabase credentials not found")
        
        self.supabase_client = create_client(supabase_url, supabase_key)
        self.db_manager = DatabaseManager(self.supabase_client)
    
    def _get_config_value(self, config: Dict[str, Any], key: str, fallback_key: str = None) -> Any:
        """
        2シート構造とフラット構造両対応の設定値取得
        
        Args:
            config: 設定データ
            key: 取得するキー
            fallback_key: フォールバックキー
        
        Returns:
            設定値またはNone
        """
        # フラット構造での取得を試行
        if key in config:
            return config[key]
        
        # 2シート構造での取得を試行
        if 'targeting' in config and key in config['targeting']:
            return config['targeting'][key]
        
        # フォールバック
        if fallback_key and fallback_key in config:
            return config[fallback_key]
            
        return None
    
    def _validate_and_cache_config(self, client_config: Dict[str, Any]) -> Dict[str, Any]:
        """設定値を検証してキャッシュから返す（パフォーマンス向上）"""
        # キャッシュキーを生成（設定内容のハッシュ）
        import hashlib
        config_str = json.dumps(client_config, sort_keys=True)
        cache_key = hashlib.md5(config_str.encode()).hexdigest()
        
        # キャッシュヒット時は検証済み設定を返す
        if self._config_cache_key == cache_key and self._validated_config_cache:
            return self._validated_config_cache
        
        # 営業日設定の検証（2シート構造対応）
        send_days = self._get_config_value(client_config, 'send_days_of_week')
        if send_days is None:
            error_msg = "営業日設定(send_days_of_week)がclient_configに存在しません"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        if isinstance(send_days, str):
            try:
                send_days = json.loads(send_days)
            except json.JSONDecodeError:
                error_msg = f"営業日設定(send_days_of_week)の形式が不正です: {send_days}"
                logger.error(error_msg)
                raise ValueError(error_msg)
                
        if not isinstance(send_days, list) or not all(isinstance(day, int) and 0 <= day <= 6 for day in send_days):
            error_msg = f"営業日設定(send_days_of_week)は0-6の整数リストである必要があります: {send_days}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # 営業時間設定の検証（2シート構造対応）
        start_time_str = self._get_config_value(client_config, 'send_start_time')
        if start_time_str is None:
            error_msg = "営業開始時間設定(send_start_time)がclient_configに存在しません"
            logger.error(error_msg)
            raise ValueError(error_msg)
            
        end_time_str = self._get_config_value(client_config, 'send_end_time')
        if end_time_str is None:
            error_msg = "営業終了時間設定(send_end_time)がclient_configに存在しません"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        def parse_time_to_minutes(time_str: str, field_name: str) -> int:
            """時間文字列（HH:MM）を分に変換"""
            try:
                hours, minutes = time_str.split(':')
                hour_int = int(hours)
                minute_int = int(minutes)
                
                if not (0 <= hour_int <= 23):
                    raise ValueError(f"時間は0-23の範囲である必要があります: {hour_int}")
                if not (0 <= minute_int <= 59):
                    raise ValueError(f"分は0-59の範囲である必要があります: {minute_int}")
                    
                return hour_int * 60 + minute_int
            except (ValueError, IndexError) as e:
                error_msg = f"{field_name}の形式が不正です（HH:MM形式で入力してください）: {time_str}, エラー: {e}"
                logger.error(error_msg)
                raise ValueError(error_msg)
        
        start_time_minutes = parse_time_to_minutes(start_time_str, "営業開始時間(send_start_time)")
        end_time_minutes = parse_time_to_minutes(end_time_str, "営業終了時間(send_end_time)")
        
        # 営業時間の論理チェック
        if start_time_minutes >= end_time_minutes:
            error_msg = f"営業開始時間は終了時間より前である必要があります: 開始={start_time_str}, 終了={end_time_str} (深夜営業は現在対応していません)"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # 日次送信数制限の検証（2シート構造対応）
        max_daily = self._get_config_value(client_config, 'max_daily_sends')
        if max_daily is None:
            error_msg = "日次送信数制限設定(max_daily_sends)がclient_configに存在しません"
            logger.error(error_msg)
            raise ValueError(error_msg)
            
        try:
            max_daily = int(max_daily)
            if max_daily <= 0:
                error_msg = f"日次送信数制限(max_daily_sends)は正の整数である必要があります: {max_daily}"
                logger.error(error_msg)
                raise ValueError(error_msg)
            if max_daily > 50000:
                error_msg = f"日次送信数制限(max_daily_sends)は現実的な範囲内である必要があります（上限: 50,000件/日）: {max_daily}"
                logger.error(error_msg)
                raise ValueError(error_msg)
        except (ValueError, TypeError) as e:
            error_msg = f"日次送信数制限(max_daily_sends)の形式が不正です: {max_daily}, エラー: {e}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # 検証済み設定をキャッシュに保存
        validated_config = {
            'send_days_of_week': send_days,
            'send_start_time': start_time_str,
            'send_end_time': end_time_str,
            'start_time_minutes': start_time_minutes,
            'end_time_minutes': end_time_minutes,
            'max_daily_sends': max_daily
        }
        
        self._validated_config_cache = validated_config
        self._config_cache_key = cache_key
        
        return validated_config
        
    def _get_max_record_id(self) -> int:
        """companiesテーブルの最大IDを動的取得（キャッシュ機能付き）"""
        try:
            current_time = time.time()
            
            # キャッシュが有効な場合はそれを返す
            if (self._max_record_id_cache is not None and 
                current_time - self._max_record_id_cache_time < self._max_record_id_cache_duration):
                logger.debug(f"Using cached max record ID: {self._max_record_id_cache}")
                return self._max_record_id_cache
            
            # データベースから最大IDを取得
            def get_max_id():
                return self.supabase_client.table('companies') \
                    .select('id') \
                    .order('id', desc=True) \
                    .limit(1) \
                    .execute()
            
            response = self.db_manager.execute_with_retry_sync(
                f"get_max_record_id_targeting_{self.targeting_id}",
                get_max_id
            )
            
            if response.data and len(response.data) > 0:
                max_id = response.data[0]['id']
                # キャッシュを更新
                self._max_record_id_cache = max_id
                self._max_record_id_cache_time = current_time
                logger.info(f"Updated max company ID cache: {max_id}")
                return max_id
            else:
                # データが取得できない場合はフォールバック値を使用
                fallback_max_id = 536156
                logger.warning(f"Could not get max company ID from database, using fallback: {fallback_max_id}")
                return fallback_max_id
                
        except Exception as e:
            # エラー時もフォールバック値を使用
            fallback_max_id = 536156
            logger.error(f"Error getting max company ID: {e}, using fallback: {fallback_max_id}")
            return fallback_max_id
    
    def _apply_unified_filters(self, companies: List[Dict[str, Any]], client_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """RPC・基本クエリ共通のフィルタリング処理（統一化）"""
        try:
            if not companies:
                return companies
            
            # 1. ng_companies正規表現除外フィルタ（2シート構造対応）
            ng_companies = self._get_config_value(client_config, 'ng_companies', '') or ''
            ng_companies = ng_companies.strip()
            if ng_companies:
                ng_pattern = ng_companies.replace(',', '|').replace('，', '|')
                import re
                try:
                    ng_regex = re.compile(ng_pattern, re.IGNORECASE)
                    companies = [c for c in companies if not ng_regex.search(c.get('name', ''))]
                    logger.debug(f"Applied ng_companies filter, remaining: {len(companies)}")
                except re.error as e:
                    logger.warning(f"Invalid ng_companies regex pattern '{ng_pattern}': {e}")
            
            # 2. instruction_valid条件フィルタ（従来の指示書有無に依存しない）
            companies = [c for c in companies if c.get('instruction_valid') is not False]
            
            # 3. 必須フィールドチェック（instruction_jsonは廃止のため不要）
            companies = [c for c in companies if c.get('form_url')]
            
            logger.debug(f"Applied unified filters, final count: {len(companies)}")
            return companies
            
        except Exception as e:
            logger.error(f"Error applying unified filters: {e}")
            return companies  # エラー時は元のリストをそのまま返す
    
    def _validate_targeting_sql_client_side(self, targeting_sql: str) -> bool:
        """【セキュリティ強化】クライアントサイド事前検証"""
        try:
            if not targeting_sql or not targeting_sql.strip():
                return True  # 空文字は有効
            
            sql_upper = targeting_sql.upper()
            
            # 危険なキーワードの事前検出（拡張版）
            dangerous_patterns = [
                'DROP', 'DELETE', 'UPDATE', 'INSERT', 'CREATE', 'ALTER',
                'EXEC', 'EXECUTE', 'UNION', 'SCRIPT', 'DECLARE', 'TRUNCATE',
                'GRANT', 'REVOKE', 'SET', 'RESET', '--', ';', '/*', '*/',
                # SQL注入の典型的パターン
                "' OR '", '" OR "', '1=1', '1 = 1',
                # 条件を常にtrueにする攻撃
                "'='", '"="', 'OR 1', 'OR TRUE',
            ]
            
            for pattern in dangerous_patterns:
                if pattern in sql_upper:
                    logger.warning(f"Client-side validation: dangerous pattern detected: {pattern}")
                    return False
            
            # 長さ制限（RPC側と同じ制限を適用）
            if len(targeting_sql) > 2000:
                logger.warning(f"Client-side validation: input too long ({len(targeting_sql)} chars)")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Error in client-side validation: {e}")
            return False  # エラー時は安全側に倒す

    def _call_get_target_companies_rpc(self, targeting_sql: str, ng_companies: str, start_id: int, limit: int, exclude_ids: set) -> List[Dict[str, Any]]:
        """新しいRPC関数を呼び出して企業データを取得（セキュリティ強化版）"""
        try:
            # 【セキュリティ強化】クライアントサイド事前検証
            if not self._validate_targeting_sql_client_side(targeting_sql):
                raise ValueError(f"Client-side validation failed for targeting_sql: potentially unsafe content detected")
            
            # ng_companies の基本的な事前検証
            if ng_companies and len(ng_companies) > 500:
                raise ValueError(f"ng_companies too long: {len(ng_companies)} chars (max 500)")
            
            # exclude_ids を配列に変換
            exclude_ids_array = list(exclude_ids) if exclude_ids else None
            
            logger.info("🛡️ Client-side security validation passed")
            
            # RPC関数を実行
            response = self.supabase_client.rpc('get_target_companies_with_sql', {
                'targeting_sql': targeting_sql,
                'ng_companies': ng_companies,
                'start_id': start_id,
                'limit_count': limit,
                'exclude_ids': exclude_ids_array
            }).execute()
            
            if response.data is None:
                logger.warning("RPC function returned null data")
                return []
                
            companies = response.data
            logger.info(f"RPC function returned {len(companies)} companies")
            
            return companies
            
        except Exception as e:
            # エラー分類の詳細化
            error_msg = str(e).lower()
            if 'network' in error_msg or 'connection' in error_msg:
                logger.error(f"Network error calling RPC: {e}")
                raise ValueError(f"Network error: Unable to connect to database") from e
            elif 'invalid targeting_sql' in error_msg:
                logger.error(f"SQL validation error: {e}")
                raise ValueError(f"Invalid SQL condition: {e}") from e
            elif 'permission' in error_msg or 'auth' in error_msg:
                logger.error(f"Permission error calling RPC: {e}")
                raise ValueError(f"Permission denied: Check database access rights") from e
            else:
                logger.error(f"Unknown error calling get_target_companies_with_sql RPC: {e}")
                raise ValueError(f"Database operation failed: {e}") from e
    
    def _get_companies_using_rpc(self, client_config: Dict[str, Any], start_id: int, limit: int, exclude_ids: set) -> List[Dict[str, Any]]:
        """RPC関数を使用して企業データを取得（直接SQL WHERE句実行）"""
        try:
            # 設定値を取得
            targeting_sql = self._get_config_value(client_config, 'targeting_sql', '') or ''
            ng_companies = self._get_config_value(client_config, 'ng_companies', '') or ''
            
            logger.info(f"=== RPC FUNCTION CALL ===")
            logger.info(f"targeting_sql: '{targeting_sql}'")
            logger.info(f"ng_companies: '{ng_companies}'")
            logger.info(f"start_id: {start_id}")
            logger.info(f"limit: {limit}")
            logger.info(f"exclude_ids: {len(exclude_ids)} items")
            
            # RPC関数を呼び出し
            companies = self._call_get_target_companies_rpc(
                targeting_sql=targeting_sql.strip(),
                ng_companies=ng_companies.strip(),
                start_id=start_id,
                limit=limit,
                exclude_ids=exclude_ids
            )
            
            logger.info(f"=== RPC FUNCTION SUCCESS ===")
            logger.info(f"Retrieved {len(companies)} companies")
            
            return companies
            
        except Exception as e:
            logger.error(f"Critical error using RPC function: {e}")
            raise ValueError(f"Failed to get companies using RPC: {e}") from e


        
    def is_within_time_limit(self) -> bool:
        """5時間制限チェック"""
        elapsed = time.time() - self.start_time
        return elapsed < self.max_execution_time
        
    def is_within_business_hours(self, client_config: Dict[str, Any]) -> bool:
        """営業時間チェック（FORM_SENDER.md 168-205仕様準拠）"""
        try:
            # キャッシュから検証済み設定を取得
            validated_config = self._validate_and_cache_config(client_config)
            
            # JST時刻を正確に計算
            jst = timezone(timedelta(hours=9))
            now_jst = datetime.now(jst)
            
            # 曜日チェック（0=月曜日, 1=火曜日, ..., 6=日曜日）
            current_day_of_week = now_jst.weekday()
            
            # 営業日チェック（キャッシュ済み）
            send_days = validated_config['send_days_of_week']
            if current_day_of_week not in send_days:
                logger.info(f"Outside business days: current={current_day_of_week}, allowed={send_days}")
                return False
            
            # 時間帯チェック（分単位で正確に）
            current_hour = now_jst.hour
            current_minute = now_jst.minute
            current_time_minutes = current_hour * 60 + current_minute
            
            # キャッシュ済みの時間設定を使用
            start_time_minutes = validated_config['start_time_minutes']
            end_time_minutes = validated_config['end_time_minutes']
            start_time_str = validated_config['send_start_time']
            end_time_str = validated_config['send_end_time']
            
            is_within_time = start_time_minutes <= current_time_minutes <= end_time_minutes
            
            logger.info(f"Business hours check: current={current_hour:02d}:{current_minute:02d} JST, "
                       f"business={start_time_str}-{end_time_str}, within_hours={is_within_time}")
            
            return is_within_time
            
        except ValueError:
            # 設定不備によるValueErrorは再raiseして処理を停止
            raise
        except Exception as e:
            # システム例外（ネットワークエラー等）は処理継続
            logger.error(f"Business hours check system error: {e}")
            return True  # システムエラー時のみ継続（安全側に倒す）
    
    def get_today_success_count(self) -> int:
        """今日の送信成功数を取得（JST基準）"""
        try:
            # JST時刻で今日の範囲を計算
            jst = timezone(timedelta(hours=9))
            now_jst = datetime.now(jst)
            today_jst = now_jst.date()
            
            # 今日のJST日付で範囲を設定
            start_of_day = f'{today_jst.isoformat()}T00:00:00+09:00'
            end_of_day = f'{today_jst.isoformat()}T23:59:59+09:00'
            
            # 再試行機能付きでDB問い合わせ
            def get_success_count():
                response = self.supabase_client.table('submissions') \
                    .select('id') \
                    .eq('targeting_id', self.targeting_id) \
                    .eq('success', True) \
                    .gte('submitted_at', start_of_day) \
                    .lte('submitted_at', end_of_day) \
                    .execute()
                return response
            
            response = self.db_manager.execute_with_retry_sync(
                f"get_today_success_count_targeting_{self.targeting_id}",
                get_success_count
            )
            
            count = len(response.data) if response.data else 0
            # Targeting IDは非表示
            logger.info(f"Today's success count: {count}")
            
            return count
            
        except Exception as e:
            logger.error(f"Error getting today's success count (all retries failed): {e}")
            return 0  # エラー時は0を返して安全側に倒す
    
    def is_within_daily_limit(self, max_daily_sends: int) -> bool:
        """日次送信数制限チェック"""
        today_count = self.get_today_success_count()
        return today_count < max_daily_sends
    
    def save_result_immediately(self, record_id: int, status: str, error_type: Optional[str] = None, instruction_valid_updated: bool = False, bot_protection_detected: bool = False):
        """企業処理結果を即座にDBに保存（FORM_SENDER.md 308-332仕様準拠）"""
        try:
            # JST時刻で記録
            jst = timezone(timedelta(hours=9))
            now_jst = datetime.now(jst)
            
            # submissionsテーブルに記録 (statusカラムは削除済みのため除外)
            submission_data = {
                'targeting_id': self.targeting_id,
                'company_id': record_id,
                'success': status == 'success',
                'error_type': error_type,
                'submitted_at': now_jst.isoformat()
            }
            
            # 再試行機能付きでDB保存
            def insert_submission():
                return self.supabase_client.table('submissions').insert(submission_data).execute()
            
            result = self.db_manager.execute_with_retry_sync(
                f"save_submission_company_{record_id}",
                insert_submission
            )
            
            # bot protection検出時にcompaniesテーブルのフラグを更新
            if bot_protection_detected:
                try:
                    def update_bot_protection():
                        return self.supabase_client.table('companies').update({
                            'bot_protection_detected': True
                        }).eq('id', record_id).execute()
                    
                    self.db_manager.execute_with_retry_sync(
                        f"update_bot_protection_company_{record_id}",
                        update_bot_protection
                    )
                    logger.info(f"Updated bot_protection_detected=true for record_id={record_id}")
                except Exception as e:
                    logger.error(f"Error updating bot_protection_detected for company {record_id}: {e}")
            
            # カウンター更新
            self.processed_count += 1
            if status == 'success':
                self.success_count += 1
            else:
                self.failed_count += 1
            
            logger.info(f"Saved result: record_id={record_id}, status={status}, "
                       f"error_type={error_type}, processed={self.processed_count}")
            
        except Exception as e:
            logger.error(f"Error saving result immediately (all retries failed): {e}")
            # カウンターは更新（記録失敗でもプロセスは継続）
            self.processed_count += 1
            if status == 'success':
                self.success_count += 1
            else:
                self.failed_count += 1
    
    # update_instruction_validity削除 - RuleBasedAnalyzerリアルタイム解析ではDBのinstruction_validフラグを使用しない
    
    def should_continue_processing(self, client_config: Dict[str, Any]) -> Tuple[bool, str]:
        """処理継続可否の判定（営業時間チェックはworker側で実施）"""
        # 時間制限チェック
        if not self.is_within_time_limit():
            return False, "時間制限（5時間）に到達"
            
        # キャッシュから検証済み設定を取得
        validated_config = self._validate_and_cache_config(client_config)
        
        # 日次送信数制限チェック（キャッシュ済み）
        max_daily = validated_config['max_daily_sends']
        if not self.is_within_daily_limit(max_daily):
            return False, f"日次送信数上限（{max_daily}件）に到達"
        
        return True, "継続可能"
    
    def _create_bulk_fetch_query(self, client_config: Dict[str, Any]) -> str:
        """FORM_SENDER.md 1.4.1仕様準拠: 大量取得用SQLクエリ構築（営業禁止検出済み企業除外対応）"""
        base_conditions = [
            "c.form_url IS NOT NULL",
            # instruction_json は廃止（RuleBasedAnalyzerを常用）
            "(c.instruction_valid IS NULL OR c.instruction_valid = true)",
            "(c.prohibition_detected IS NULL OR c.prohibition_detected = false)"
        ]
        
        # targeting_sql条件を追加（安全性検証付き、2シート構造対応）
        targeting_sql = self._get_config_value(client_config, 'targeting_sql', '') or ''
        targeting_sql = targeting_sql.strip()
        if targeting_sql:
            # WHERE句の重複を防ぐため、WHERE句を除去
            if targeting_sql.upper().startswith('WHERE '):
                targeting_sql = targeting_sql[6:].strip()
            
            # SQL安全性検証を適用
            sanitized_targeting_sql = self._sanitize_sql_conditions(targeting_sql)
            if sanitized_targeting_sql:
                base_conditions.append(f"({sanitized_targeting_sql})")
            else:
                logger.warning("targeting_sql was rejected due to security concerns, ignoring")
        
        # ng_companies除外処理（FORM_SENDER.md 1.4.2仕様準拠、安全性強化、2シート構造対応）
        ng_companies = self._get_config_value(client_config, 'ng_companies', '') or ''
        ng_companies = ng_companies.strip()
        if ng_companies:
            # カンマ区切りをパイプ区切りに変換して正規表現パターン作成
            ng_pattern = ng_companies.replace(',', '|').replace('，', '|')  # 全角カンマも対応
            
            # より厳密な安全化処理
            sanitized_ng_pattern = self._sanitize_sql_conditions(ng_pattern)
            if sanitized_ng_pattern and len(sanitized_ng_pattern) < 1000:  # パターンが長すぎないかチェック
                base_conditions.append(f"c.name !~ '{sanitized_ng_pattern}'")
            else:
                logger.warning("ng_companies pattern was rejected due to security or length concerns, ignoring")
        
        where_clause = " AND ".join(base_conditions)
        logger.debug(f"Bulk fetch query conditions: {len(base_conditions)} conditions including prohibition_detected exclusion")
        return where_clause
    
    def _get_required_company_columns(self, client_config: Dict[str, Any]) -> set:
        """企業固有プレースホルダーに必要な追加カラムを取得"""
        try:
            required_columns = CompanyPlaceholderAnalyzer.get_required_company_columns(client_config)
            # Targeting IDは非表示
            logger.info(f"Required company columns: {required_columns}")
            return required_columns
        except Exception as e:
            # エラー詳細は非表示
            logger.error("Error getting required company columns: System error occurred")
            return set()
    
    def _build_select_columns(self, additional_columns: set) -> str:
        """基本カラム + 追加カラムでSELECT文を構築"""
        # 基本的な必須カラム（実際に存在するカラムのみ）
        base_columns = {
            'id', 'company_name', 'form_url',
            'instruction_valid'
        }
        
        # 追加カラムと結合（重複を自動除去）
        all_columns = base_columns | additional_columns
        
        # カンマ区切りの文字列として返す
        select_string = ', '.join(sorted(all_columns))
        logger.debug(f"Select columns: {select_string}")
        return select_string
    
    def _query_companies_from_id(self, client_config: Dict[str, Any], start_id: int, limit: int, exclude_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        """指定したID以上の企業を抽出（RPC関数使用、直接SQL WHERE句実行）"""
        try:
            # 除外IDセットを準備
            if exclude_ids is None:
                exclude_ids = set()
            
            # RPC関数を使用して企業データを取得
            companies = self._get_companies_using_rpc(client_config, start_id, limit, exclude_ids)
            
            # 統一フィルタを適用（念のため、主な処理はRPC側で完了）
            companies = self._apply_unified_filters(companies, client_config)
            
            # 優先度に基づく並び替えと制限
            companies = self._get_companies_by_priority(companies, limit)
            
            logger.info(f"Retrieved {len(companies)} companies using RPC function with unified filters including prohibition_detected exclusion (ID >= {start_id})")
            
            return companies
            
        except Exception as e:
            logger.error(f"Error querying companies from ID {start_id} using RPC: {e}")
            raise ValueError(f"Failed to query companies: {e}") from e
    
    def _fetch_companies_bulk(self, client_config: Dict[str, Any], limit: int = 1000) -> List[Dict[str, Any]]:
        """FORM_SENDER.md 1.4.1仕様準拠: 最大1000件の企業を一括取得（ランダムID開始点方式）"""
        try:
            # 企業ID指定モード（テスト用）
            if client_config.get('company_id') is not None:
                specific_company_id = client_config.get('company_id')
                logger.info(f"Specific company mode: fetching company ID {specific_company_id}")
                
                # 指定企業のみを取得
                companies = self._query_companies_from_id(client_config, specific_company_id, 1)
                if companies:
                    # IDが一致するもののみに絞り込み
                    specific_companies = [c for c in companies if c['id'] == specific_company_id]
                    if specific_companies:
                        logger.info(f"Found specified company: {specific_companies[0].get('company_name', 'Unknown')}")
                        return specific_companies
                    else:
                        logger.warning(f"Company ID {specific_company_id} not found in results")
                        return []
                else:
                    logger.warning(f"No company found with ID {specific_company_id}")
                    return []
            
            # 従来のランダム取得モード
            import random
            
            # ランダムな開始IDを生成 (動的最大ID取得)
            max_record_id = self._get_max_record_id()
            random_start_id = random.randint(1, max_record_id)
            
            logger.info(f"Fetching bulk companies (limit: {limit}) starting from random ID: {random_start_id}")
            
            # 1. random_id以上の企業を抽出
            companies = self._query_companies_from_id(client_config, random_start_id, limit)
            logger.info(f"Fetched {len(companies)} companies from ID >= {random_start_id}")
            
            # 2. 不足時は1以上で補完（効率的な除外ID処理）
            if len(companies) < limit:
                additional_needed = limit - len(companies)
                logger.info(f"Need {additional_needed} more companies, fetching from ID >= 1")
                
                # 既に取得した企業のIDを除外セットとして使用
                existing_ids = {c['id'] for c in companies}
                
                # 効率的なクエリで重複を事前に除外（除外IDを制限してパフォーマンス向上）
                limited_existing_ids = existing_ids if len(existing_ids) <= 50 else set(list(existing_ids)[:50])
                additional_companies = self._query_companies_from_id(
                    client_config, 1, additional_needed * 2, exclude_ids=limited_existing_ids  # 除外を考慮して多めに取得
                )
                
                # 念のための重複チェック（データベースレベルで除外されているはずだが安全のため）
                filtered_additional = []
                for company in additional_companies:
                    if company['id'] not in existing_ids and len(companies) + len(filtered_additional) < limit:
                        filtered_additional.append(company)
                
                companies.extend(filtered_additional)
                
                logger.info(f"Added {len(filtered_additional)} companies from beginning, total: {len(companies)}")
            
            return companies
            
        except Exception as e:
            logger.error(f"Error fetching companies in bulk (all retries failed): {e}")
            return []
    
    def _save_companies_to_temp_file(self, companies: List[Dict[str, Any]]):
        """FORM_SENDER.md 1.4.1仕様準拠: 企業情報を一時ファイルに保存"""
        try:
            # 既存の一時ファイルがある場合はクリーンアップ
            if self.temp_companies_file and os.path.exists(self.temp_companies_file):
                os.remove(self.temp_companies_file)
            
            # 新しい一時ファイルを作成
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
                json.dump(companies, f, ensure_ascii=False, indent=2)
                self.temp_companies_file = f.name
            
            logger.info(f"Saved {len(companies)} companies to temporary file: {self.temp_companies_file}")
            
        except Exception as e:
            logger.error(f"Error saving companies to temporary file: {e}")
            raise
    
    def _load_next_batch_from_temp_file(self, batch_size: int = 10) -> List[Dict[str, Any]]:
        """FORM_SENDER.md 1.4.1仕様準拠: 一時ファイルから10件ずつ読み込み"""
        try:
            # バッファに残りがある場合はそれを使用
            if len(self.companies_buffer) >= batch_size:
                batch = self.companies_buffer[:batch_size]
                self.companies_buffer = self.companies_buffer[batch_size:]
                return batch
            
            # バッファが不足している場合は一時ファイルから補充
            if self.temp_companies_file and os.path.exists(self.temp_companies_file):
                with open(self.temp_companies_file, 'r') as f:
                    all_companies = json.load(f)
                
                # 処理済みの企業を除外してバッファに補充
                remaining_companies = all_companies[self.processed_count:]
                self.companies_buffer.extend(remaining_companies)
                
                # バッチを作成
                if len(self.companies_buffer) >= batch_size:
                    batch = self.companies_buffer[:batch_size]
                    self.companies_buffer = self.companies_buffer[batch_size:]
                    return batch
                else:
                    # 残り全部を返す
                    batch = self.companies_buffer
                    self.companies_buffer = []
                    return batch
            
            return []
            
        except Exception as e:
            logger.error(f"Error loading batch from temporary file: {e}")
            return []
    
    def get_target_companies_batch(self, client_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        FORM_SENDER.md 1.4.1仕様準拠: 大量取得+一時ファイル管理方式で10件ずつ取得
        
        処理フロー:
        1. 初回大量取得: ワークフロー開始時に最大1000件の処理対象企業を取得
        2. 一時ファイル保存: 取得した企業情報をJSON形式で一時ファイルに保存
        3. 順次処理: ファイルから10件ずつ読み込んで処理実行
        4. 残件管理: 一時ファイルの残件数が0件になったら再クエリ実行
        5. 再取得: 新しい処理対象がある場合は追加取得して一時ファイルに追記
        """
        try:
            # Step 3: ファイルから10件ずつ読み込み
            batch = self._load_next_batch_from_temp_file(10)
            
            # Step 4: 残件管理 - バッチが空の場合は再取得を試行
            if not batch:
                logger.info("No companies in buffer, attempting bulk fetch...")
                
                # Step 1 & 5: 初回大量取得 / 再取得
                companies = self._fetch_companies_bulk(client_config, 1000)
                
                if companies:
                    # Step 2: 一時ファイル保存
                    self._save_companies_to_temp_file(companies)
                    
                    # Step 3: ファイルから10件ずつ読み込み（再試行）
                    batch = self._load_next_batch_from_temp_file(10)
                    
                    logger.info(f"Bulk fetch successful, loaded batch of {len(batch)} companies")
                else:
                    logger.info("No more companies available for processing")
                    return []
            
            # 取得結果の詳細ログ（会社名は機密情報のためマスク）
            if batch:
                logger.info(f"Loaded batch: {len(batch)} companies ready for processing")
                for i, company in enumerate(batch[:3]):  # 最初の3件のみログ出力
                    logger.debug(
                        f"Company {i+1}: id={company['id']}, has_form_url={bool(company.get('form_url'))}, mapping='rule_based'"
                    )
            
            return batch
            
        except Exception as e:
            logger.error(f"Error getting target companies batch: {e}")
            return []
    
    def _filter_by_submission_history(self, companies: List[Dict[str, Any]], allow_failed: bool = False) -> List[Dict[str, Any]]:
        """送信履歴による企業フィルタリング（優先度付き）"""
        try:
            if not companies:
                return []
                
            record_ids = [company['id'] for company in companies]
            
            # 全送信記録を取得（成功・失敗両方）
            def get_submission_history():
                return self.supabase_client.table('submissions') \
                    .select('company_id, success') \
                    .eq('targeting_id', self.targeting_id) \
                    .in_('company_id', record_ids) \
                    .execute()
            
            response = self.db_manager.execute_with_retry_sync(
                f"get_submission_history_targeting_{self.targeting_id}",
                get_submission_history
            )
            
            submission_data = response.data if response.data else []
            
            # 企業IDごとの送信状況を分析
            company_status = {}
            for record in submission_data:
                record_id = record['company_id']  # DBカラム名はcompany_idのまま
                success = record['success']
                
                if record_id not in company_status:
                    company_status[record_id] = {'success': False, 'failed': False}
                
                if success is True:
                    company_status[record_id]['success'] = True
                elif success is False:
                    company_status[record_id]['failed'] = True
            
            # フィルタリングロジック
            filtered_companies = []
            for company in companies:
                record_id = company['id']
                
                if record_id not in company_status:
                    # 記録なし企業 - 常に対象
                    filtered_companies.append(company)
                elif company_status[record_id]['success']:
                    # 成功記録あり - 常に除外
                    continue
                elif company_status[record_id]['failed'] and allow_failed:
                    # 失敗記録のみ - allow_failedがTrueの場合のみ対象
                    filtered_companies.append(company)
                # else: 失敗記録のみでallow_failed=False - 除外
            
            no_record_count = len([c for c in companies if c['id'] not in company_status])
            success_excluded = len([c for c in companies if c['id'] in company_status and company_status[c['id']]['success']])
            failed_excluded = len([c for c in companies if c['id'] in company_status and company_status[c['id']]['failed'] and not company_status[c['id']]['success'] and not allow_failed])
            
            logger.info(f"Company filtering results: no_record={no_record_count}, "
                       f"success_excluded={success_excluded}, failed_excluded={failed_excluded}, "
                       f"allow_failed={allow_failed}, final_count={len(filtered_companies)}")
            
            return filtered_companies
            
        except Exception as e:
            logger.error(f"Error filtering by submission history: {e}")
            return companies  # エラー時は元のリストをそのまま返す
    
    def _get_companies_by_priority(self, all_companies: List[Dict[str, Any]], target_limit: int) -> List[Dict[str, Any]]:
        """優先度付き企業取得（2段階取得ロジック）"""
        try:
            if not all_companies:
                return []
            
            logger.info(f"Starting priority-based company selection from {len(all_companies)} candidates")
            
            # 第1段階: 記録なし企業を優先取得
            no_record_companies = self._filter_by_submission_history(all_companies, allow_failed=False)
            logger.info(f"Phase 1 - No record companies: {len(no_record_companies)}")
            
            if len(no_record_companies) >= target_limit:
                # 記録なし企業だけで十分
                result = no_record_companies[:target_limit]
                logger.info(f"Sufficient companies from no-record group: {len(result)}")
                return result
            
            # 第2段階: 記録なし企業が不足している場合、成功記録のない企業を追加
            logger.info(f"Phase 1 insufficient ({len(no_record_companies)}/{target_limit}), proceeding to phase 2")
            
            # 成功記録のない企業（記録なし + 失敗のみ）を取得
            no_success_companies = self._filter_by_submission_history(all_companies, allow_failed=True)
            logger.info(f"Phase 2 - No success record companies: {len(no_success_companies)}")
            
            # target_limitまで取得
            result = no_success_companies[:target_limit]
            logger.info(f"Final selection: {len(result)} companies (no_record: {len(no_record_companies)}, "
                       f"additional_no_success: {len(result) - len(no_record_companies)})")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in priority-based company selection: {e}")
            # エラー時は従来の方法にフォールバック
            return self._filter_by_submission_history(all_companies, allow_failed=False)
    
    def _filter_already_sent_companies(self, companies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """送信済み企業をフィルタリング（フォールバック用・後方互換）"""
        # 新しいフィルタリング関数を使用（成功記録のない企業のみ）
        return self._filter_by_submission_history(companies, allow_failed=False)
    
    def cleanup_temp_files(self):
        """一時ファイルのクリーンアップ"""
        try:
            if self.temp_companies_file and os.path.exists(self.temp_companies_file):
                os.remove(self.temp_companies_file)
                logger.info(f"Cleaned up temporary file: {self.temp_companies_file}")
                self.temp_companies_file = None
        except Exception as e:
            logger.error(f"Error cleaning up temporary file: {e}")
    
    def get_processing_summary(self) -> Dict[str, Any]:
        """処理サマリーを取得"""
        elapsed_time = time.time() - self.start_time
        return {
            'targeting_id': self.targeting_id,
            'processed_count': self.processed_count,
            'success_count': self.success_count,
            'failed_count': self.failed_count,
            'elapsed_time': elapsed_time,
            'processing_mode': 'continuous_loop'
        }
