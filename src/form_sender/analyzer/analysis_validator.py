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

        essential_fields = ['メールアドレス', 'お問い合わせ本文']
        for field in essential_fields:
            if field not in field_mapping:
                validation_issues.append(f"Required field '{field}' is missing")

        for field_name, assignment in input_assignments.items():
            value = assignment.get('value', '')
            element_info = field_mapping.get(field_name, {})
            score = element_info.get('score', 0)
            if not self.duplicate_prevention.register_field_assignment(field_name, value or "_EMPTY_", score, element_info):
                validation_issues.append(f"Duplicate value rejected: {field_name}")

        is_valid, system_issues = self.duplicate_prevention.validate_assignments()
        validation_issues.extend(system_issues)
        
        return not validation_issues, validation_issues
