import logging
from typing import Dict, Any, Optional

from .field_combination_manager import FieldCombinationManager
from .split_field_detector import SplitFieldDetector

logger = logging.getLogger(__name__)

class InputValueAssigner:
    """入力値の生成と割り当てを担当するクラス"""

    def __init__(self, field_combination_manager: FieldCombinationManager, 
                 split_field_detector: SplitFieldDetector):
        self.field_combination_manager = field_combination_manager
        self.split_field_detector = split_field_detector
        self.required_analysis: Dict[str, Any] = {}
        self.unified_field_info: Dict[str, Any] = {}

    async def assign_enhanced_input_values(self, field_mapping: Dict[str, Dict[str, Any]], 
                                           auto_handled: Dict[str, Dict[str, Any]], 
                                           split_groups, client_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        input_assignments = {}
        client_data = client_data or {}

        split_assignments = self.split_field_detector.generate_field_assignments(split_groups, client_data)

        for field_name, field_info in field_mapping.items():
            if not self._should_input_field(field_name, field_info):
                continue
            
            input_value = split_assignments.get(field_name, self._generate_enhanced_input_value(field_name, field_info, client_data))
            
            input_assignments[field_name] = {
                'selector': field_info['selector'], 'input_type': field_info['input_type'],
                'value': input_value, 'required': field_info.get('required', False)
            }

        for field_name, field_info in auto_handled.items():
            value = field_info.get('default_value', True)
            if field_info.get('auto_action') == 'copy_from':
                src = field_info.get('copy_from_field', '')
                value = input_assignments.get(src, {}).get('value', '')
            
            input_assignments[field_name] = {
                'selector': field_info['selector'], 'input_type': field_info['input_type'],
                'value': value, 'required': field_info.get('required', False),
                'auto_action': field_info.get('auto_action', 'default')
            }
        # 共通の取り違えを補正（例: sei/mei の入れ違い、sei_kana/mei_kanaの入れ違い）
        try:
            self._fix_name_selector_mismatch(field_mapping, input_assignments)
            self._enforce_name_values(input_assignments, client_data)
        except Exception as e:
            logger.debug(f"name selector mismatch fix skipped: {e}")

        return input_assignments

    def _should_input_field(self, field_name: str, field_info: Dict[str, Any]) -> bool:
        if self._is_fax_field(field_name, field_info):
            return False
        if self.required_analysis.get('treat_all_as_required', False):
            return True
        core_fields = ['件名', 'お問い合わせ本文', 'メールアドレス', '姓', '名', '氏名', 'お名前']
        if field_name in core_fields:
            return True
        return field_info.get('required', False)

    def _is_fax_field(self, field_name: str, field_info: Dict[str, Any]) -> bool:
        return 'fax' in field_name.lower() or 'fax' in field_info.get('selector', '').lower()

    def _generate_enhanced_input_value(self, field_name: str, field_info: Dict[str, Any], client_data: Dict[str, Any]) -> str:
        # Get value from field combination manager
        value = self.field_combination_manager.get_field_value_for_type(field_name, 'single', client_data)
        
        # If empty value returned, try specific field mappings
        if not value:
            # Map Japanese field names to appropriate combination types
            if field_name == "統合氏名":
                value = self.field_combination_manager.generate_combined_value('full_name', client_data)
            elif field_name == "お問い合わせ本文":
                # Get message from targeting data
                if isinstance(client_data, dict):
                    targeting_info = client_data.get('targeting', {})
                    value = targeting_info.get('message', '')
            
            # If still empty after specific mapping, use fallback
            if not value:
                # For any unmappable field (when all are required or specific field is required), use full-width space
                if (self.required_analysis.get('treat_all_as_required', False) or 
                    field_info.get('required', False)):
                    value = "　"  # 全角スペース
                else:
                    # For non-required fields, use empty string
                    value = ""
        
        return value

    def _fix_name_selector_mismatch(self, field_mapping: Dict[str, Dict[str, Any]], input_assignments: Dict[str, Any]) -> None:
        """
        一般的なフォームで見られる 'sei' / 'mei'（および *_kana）入れ違いを補正する。
        - 例: 『姓』が #mei、『名』が #sei を指しているケース
        - 例: 『姓カナ』が #mei_kana、『名カナ』が #sei_kana を指しているケース
        汎用ヒューリスティクスのみを用い、他のケースに影響しないよう限定的に適用。
        """
        def sel(name: str) -> str:
            return (field_mapping.get(name, {}).get('selector') or '').lower()

        def swap(a: str, b: str) -> None:
            if a in input_assignments and b in input_assignments:
                input_assignments[a]['value'], input_assignments[b]['value'] = (
                    input_assignments[b]['value'], input_assignments[a]['value']
                )
                logger.info(f"Auto-corrected value assignment swap: {a} <-> {b}")

        def _is_sei_mei_mismatch(sei_sel: str, mei_sel: str) -> bool:
            # より厳格な判定（典型トークンの相互混入）
            sei_patterns = ['sei', 'last', 'family']
            mei_patterns = ['mei', 'first', 'given']
            if not sei_sel or not mei_sel:
                return False
            sei_in_mei = any(p in mei_sel for p in sei_patterns)
            mei_in_sei = any(p in sei_sel for p in mei_patterns)
            return sei_in_mei and mei_in_sei and ('kana' not in sei_sel and 'kana' not in mei_sel)

        def _is_sei_mei_kana_mismatch(sei_sel: str, mei_sel: str) -> bool:
            # カナの入れ違い（厳格に *_kana を含むか確認）
            if not sei_sel or not mei_sel:
                return False
            return ('mei' in sei_sel and 'sei' in mei_sel) and ('kana' in sei_sel and 'kana' in mei_sel)

        # 1) 漢字の姓/名
        last_sel, first_sel = sel('姓'), sel('名')
        if _is_sei_mei_mismatch(last_sel, first_sel):
            swap('姓', '名')

        # 2) カナの姓/名
        last_kana_sel, first_kana_sel = sel('姓カナ'), sel('名カナ')
        if _is_sei_mei_kana_mismatch(last_kana_sel, first_kana_sel):
            swap('姓カナ', '名カナ')

    def _enforce_name_values(self, input_assignments: Dict[str, Any], client_data: Dict[str, Any]) -> None:
        """
        姓/名/カナの値はクライアントデータからの確定値を採用して上書きする。
        - マッピング段階の軽微な取り違えの影響を排除（安全・汎用）
        """
        client = client_data.get('client', {}) if isinstance(client_data, dict) else {}
        mapping = {
            '姓': client.get('last_name', ''),
            '名': client.get('first_name', ''),
            '姓カナ': client.get('last_name_kana', ''),
            '名カナ': client.get('first_name_kana', ''),
        }
        for k, v in mapping.items():
            if k in input_assignments and v:
                input_assignments[k]['value'] = v
                logger.info(f"Enforced canonical value for '{k}' from client data")
