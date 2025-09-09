import logging
from typing import Dict, List, Any, Tuple

from .duplicate_prevention import DuplicatePreventionManager

logger = logging.getLogger(__name__)

class AnalysisValidator:
    """解析結果の最終検証を担当するクラス"""

    def __init__(self, duplicate_prevention: DuplicatePreventionManager):
        self.duplicate_prevention = duplicate_prevention

    async def validate_final_assignments(self, input_assignments: Dict[str, Any], 
                                         field_mapping: Dict[str, Any], 
                                         form_type_info: Dict[str, Any]) -> Tuple[bool, List[str]]:
        self.duplicate_prevention.clear_assignments()
        validation_issues = []

        if form_type_info.get('primary_type') in ['search_form', 'feedback_form', 'order_form', 'newsletter_form', 'other_form', 'auth_form']:
            return True, []

        # 必須検証: フォーム種別に依存しつつ、実在しない項目は要求しない（偽陽性抑止）
        # - お問い合わせ本文: contact_form では原則必須（textarea/labelが無い非常に短い問い合わせフォームは除外されることがある）
        # - メールアドレス: DOMに存在しない/検出できないケースでは必須要求しない
        require_message = True
        require_email = False
        try:
            # 既にマッピングされていれば当然必須
            require_email = ('メールアドレス' in field_mapping)
            # input_assignments に email が含まれている場合も必須
            require_email = require_email or ('メールアドレス' in (input_assignments or {}))
        except Exception:
            pass

        if require_email and 'メールアドレス' not in field_mapping:
            validation_issues.append("Required field 'メールアドレス' is missing")
        if require_message and 'お問い合わせ本文' not in field_mapping:
            validation_issues.append("Required field 'お問い合わせ本文' is missing")

        for field_name, assignment in input_assignments.items():
            value = assignment.get('value', '')
            element_info = field_mapping.get(field_name, {})
            score = element_info.get('score', 0)
            if not self.duplicate_prevention.register_field_assignment(field_name, value or "_EMPTY_", score, element_info):
                validation_issues.append(f"Duplicate value rejected: {field_name}")

        is_valid, system_issues = self.duplicate_prevention.validate_assignments()
        validation_issues.extend(system_issues)
        
        return not validation_issues, validation_issues
