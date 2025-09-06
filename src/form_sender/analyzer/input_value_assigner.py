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

            input_type = field_info.get('input_type')
            # 住所/都道府県は分割割当よりも文脈ヒューリスティクスを優先
            if field_name == '都道府県':
                input_value = self._handle_prefecture_assignment(field_info, client_data)
            elif field_name.startswith('住所'):
                input_value = self._handle_address_assignment(field_name, field_info, client_data)
            else:
                input_value = split_assignments.get(field_name)
                if input_value is None or str(input_value).strip() == '':
                    input_value = self._generate_enhanced_input_value(field_name, field_info, client_data)

            # 選択式（select/checkbox/radio）のクライアント値割り当て制約
            # 方針: クライアント情報を当てはめる可能性があるのは address_1（都道府県）と gender のみ
            # ここでは select のみを対象にし、性別以外はアルゴリズム選択に委譲する
            auto_action = None
            extra = {}
            if input_type == 'select':
                allowed = field_name in {'性別', '都道府県'}
                if not allowed:
                    # 値は使わず、3段階アルゴリズムで選択させる
                    input_value = ''
                    auto_action = 'select_by_algorithm'
                else:
                    # 許可フィールドでも値が無ければアルゴリズム選択を有効化
                    if not (input_value or '').strip():
                        auto_action = 'select_by_algorithm'

            assign = {
                'selector': field_info['selector'],
                'input_type': input_type,
                'value': input_value,
                'required': field_info.get('required', False)
            }
            if auto_action:
                assign['auto_action'] = auto_action
            # field_infoに自動動作指定がある場合は引き継ぐ（確認用メール等）
            if field_info.get('auto_action'):
                assign['auto_action'] = field_info.get('auto_action')
            if field_info.get('copy_from_field'):
                assign['copy_from_field'] = field_info.get('copy_from_field')
            assign.update(extra)

            input_assignments[field_name] = assign

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
        # フォールバック: 単一フィールドの郵便番号にも7桁を投入
        try:
            if '郵便番号1' in input_assignments:
                v = str(input_assignments['郵便番号1'].get('value', '') or '').strip()
                if not v:
                    client = client_data.get('client', {}) if isinstance(client_data, dict) else {}
                    combined = (client.get('postal_code_1', '') or '') + (client.get('postal_code_2', '') or '')
                    if combined.strip():
                        input_assignments['郵便番号1']['value'] = combined
        except Exception:
            pass
        # 共通の取り違えを補正（例: sei/mei の入れ違い、sei_kana/mei_kanaの入れ違い）
        try:
            self._fix_name_selector_mismatch(field_mapping, input_assignments)
            self._enforce_name_values(input_assignments, client_data)
        except Exception as e:
            logger.debug(f"name selector mismatch fix skipped: {e}")

        # 追加の安全弁: 都道府県の空値補完（text/selectを問わず）
        try:
            if '都道府県' in input_assignments:
                v = str(input_assignments['都道府県'].get('value','') or '').strip()
                if not v:
                    pref = self._handle_prefecture_assignment({}, client_data)
                    if pref:
                        input_assignments['都道府県']['value'] = pref
                        logger.info("Filled empty '都道府県' from client address_1 (fallback)")
        except Exception:
            pass

        return input_assignments

    def _should_input_field(self, field_name: str, field_info: Dict[str, Any]) -> bool:
        if self._is_fax_field(field_name, field_info):
            return False
        if self.required_analysis.get('treat_all_as_required', False):
            return True
        core_fields = ['件名', 'お問い合わせ本文', 'メールアドレス', '姓', '名', '氏名', 'お名前', '統合氏名', '電話番号', '会社名']
        if field_name in core_fields:
            return True
        return field_info.get('required', False)

    def _is_fax_field(self, field_name: str, field_info: Dict[str, Any]) -> bool:
        return 'fax' in field_name.lower() or 'fax' in field_info.get('selector', '').lower()

    def _generate_enhanced_input_value(self, field_name: str, field_info: Dict[str, Any], client_data: Dict[str, Any]) -> str:
        # Get value from field combination manager
        value = self.field_combination_manager.get_field_value_for_type(field_name, 'single', client_data)

        # 住所/郵便/電話/都道府県の汎用整形・割当（追加）
        def _client() -> Dict[str, Any]:
            return client_data.get('client') if isinstance(client_data, dict) and 'client' in client_data else client_data

        def _ctx_blob() -> str:
            try:
                best = (field_info.get('best_context_text') or '')
            except Exception:
                best = ''
            parts = [field_info.get('name',''), field_info.get('id',''), field_info.get('class',''), field_info.get('placeholder',''), best]
            return ' '.join([p for p in parts if p]).lower()

        def _format_postal(v: str) -> str:
            vv = (v or '').replace('-', '').strip()
            ph = (field_info.get('placeholder','') or '')
            if len(vv) == 7 and ('-' in ph or '〒' in ph or '〒' in _ctx_blob()):
                return f"{vv[:3]}-{vv[3:]}"
            return vv

        def _format_phone(v: str) -> str:
            vv = (v or '').replace('-', '').strip()
            ph = (field_info.get('placeholder','') or '').lower()
            if '-' in ph and vv.isdigit() and len(vv) in (10,11):
                if len(vv) == 10:
                    return f"{vv[:2]}-{vv[2:6]}-{vv[6:]}"
                else:
                    return f"{vv[:3]}-{vv[3:7]}-{vv[7:]}"
            return vv

        blob = _ctx_blob()
        client = _client()

        if field_name == '郵便番号':
            pv = self.field_combination_manager.get_field_value_for_type('郵便番号', 'single', client_data)
            return _format_postal(pv)

        if field_name == '電話番号':
            phv = self.field_combination_manager.get_field_value_for_type('電話番号', 'single', client_data)
            return _format_phone(phv or value)

        if field_name == '都道府県':
            return self._handle_prefecture_assignment(field_info, client_data)

        # 本文は常に確定値を適用（フォールバック条件に依存しない）
        if field_name == 'お問い合わせ本文':
            if isinstance(client_data, dict):
                t = client_data.get('targeting', {})
                msg = t.get('message','')
                if msg:
                    return msg
        # 統合氏名は組み合わせ値を使用
        if field_name == '統合氏名':
            full = self.field_combination_manager.generate_combined_value('full_name', client_data)
            if full:
                return full
        # 統合氏名カナは種別判定のうえ確定値を生成
        if field_name == "統合氏名カナ":
            kana_type = 'katakana'
            try:
                # 1) コンテキスト
                ctx = (field_info.get('best_context_text') or '')
                if not ctx and isinstance(field_info.get('context'), list):
                    ctx = next((c.get('text','') for c in field_info['context'] if isinstance(c, dict) and c.get('text')), '')
                ctx_blob = str(ctx)
                if ('ひらがな' in ctx_blob) or ('hiragana' in ctx_blob.lower()):
                    kana_type = 'hiragana'
                else:
                    # 2) プレースホルダーの文字種
                    placeholder = str(field_info.get('placeholder', '') or '')
                    def _has_hiragana(s: str) -> bool:
                        return any('ぁ' <= ch <= 'ゖ' for ch in s)
                    def _has_katakana(s: str) -> bool:
                        return any('ァ' <= ch <= 'ヺ' or ch == 'ー' for ch in s)
                    if placeholder:
                        if _has_hiragana(placeholder) and not _has_katakana(placeholder):
                            kana_type = 'hiragana'
                        elif _has_katakana(placeholder) and not _has_hiragana(placeholder):
                            kana_type = 'katakana'
                    # 3) name/id/class のヒント
                    if kana_type == 'katakana':
                        blob = ' '.join([
                            str(field_info.get('name','') or ''),
                            str(field_info.get('id','') or ''),
                            str(field_info.get('class','') or ''),
                        ]).lower()
                        if 'hiragana' in blob:
                            kana_type = 'hiragana'
            except UnicodeError as e:
                logger.warning(f"Unicode error in kana detection: {e}")
                kana_type = 'katakana'
            except Exception as e:
                logger.error(f"Unexpected error in kana type detection: {e}")
                kana_type = 'katakana'
            return self.field_combination_manager.generate_unified_kana_value(kana_type, client_data)
            
            # If still empty after specific mapping, use fallback
            if not value:
                # For any unmappable field (when all are required or specific field is required), use full-width space
                if (self.required_analysis.get('treat_all_as_required', False) or 
                    field_info.get('required', False)):
                    value = "　"  # 全角スペース
                else:
                    # For non-required fields, use empty string
                    value = ""

        # 住所/住所_補助* の文脈に応じた割当
        if field_name.startswith('住所'):
            addr = self._handle_address_assignment(field_name, field_info, client_data)
            if addr:
                return addr

        return value

    def _handle_prefecture_assignment(self, field_info: Dict[str, Any], client_data: Dict[str, Any]) -> str:
        """都道府県フィールドへの値割り当て。
        - クライアントデータの `address_1` を最優先
        - 異常時は空文字を返す
        """
        try:
            client = client_data.get('client') if isinstance(client_data, dict) and 'client' in client_data else client_data
            pref = (client or {}).get('address_1', '')
            return str(pref or '').strip()
        except Exception as e:
            logger.debug(f"prefecture assignment skipped: {e}")
            return ''

    def _handle_address_assignment(self, field_name: str, field_info: Dict[str, Any], client_data: Dict[str, Any]) -> str:
        """住所関連フィールドへの値割り当て（文脈駆動）。
        - 住所_補助*, 市区町村、番地/建物などの文脈を見て適切に構成
        - デフォルトは住所全体
        """
        try:
            def _client() -> Dict[str, Any]:
                return client_data.get('client') if isinstance(client_data, dict) and 'client' in client_data else client_data

            client = _client()
            blob = ''
            try:
                best = (field_info.get('best_context_text') or '')
            except Exception:
                best = ''
            parts = [field_info.get('name',''), field_info.get('id',''), field_info.get('class',''), field_info.get('placeholder',''), best]
            blob = ' '.join([p for p in parts if p]).lower()

            city_tokens = ['市区町村','市区','city','区','町','town','丁目']
            detail_tokens = ['番地','丁目','建物','building','マンション','ビル','部屋','room','apt','apartment','号室','詳細']
            pref_tokens = ['都道府県','prefecture','県','都','府']

            def join_nonempty(parts, sep=''):
                return sep.join([p for p in parts if p])

            if any(t in blob for t in pref_tokens):
                v = client.get('address_1','')
                if v:
                    return v
            if field_name.startswith('住所_補助') or any(t in blob for t in detail_tokens):
                v = join_nonempty([client.get('address_4',''), client.get('address_5','')], '　')
                if v:
                    return v
            if any(t in blob for t in city_tokens):
                v = join_nonempty([client.get('address_2',''), client.get('address_3','')])
                if v:
                    return v
            # デフォルトは住所全体
            full_addr = self.field_combination_manager.generate_combined_value('address', client_data)
            return full_addr or ''
        except Exception as e:
            logger.debug(f"address assignment skipped: {e}")
            return ''

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
