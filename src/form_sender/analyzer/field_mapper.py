
import logging
from typing import Dict, List, Any, Optional, Callable, Awaitable, Tuple
from playwright.async_api import Page, Locator

from .element_scorer import ElementScorer
from .context_text_extractor import ContextTextExtractor
from .field_patterns import FieldPatterns
from .duplicate_prevention import DuplicatePreventionManager

logger = logging.getLogger(__name__)

class FieldMapper:
    """フィールドのマッピング処理を担当するクラス"""
    # 非必須だが高信頼であれば入力価値が高い項目（汎用・安全）
    OPTIONAL_HIGH_PRIORITY_FIELDS = {"件名", "電話番号"}

    def __init__(self, page: Page, element_scorer: ElementScorer, 
                 context_text_extractor: ContextTextExtractor, field_patterns: FieldPatterns,
                 duplicate_prevention: DuplicatePreventionManager, settings: Dict[str, Any],
                 create_enhanced_element_info_func: Callable[..., Awaitable[Dict[str, Any]]],
                 generate_temp_value_func: Callable[..., str],
                 field_combination_manager):
        self.page = page
        self.element_scorer = element_scorer
        self.context_text_extractor = context_text_extractor
        self.field_patterns = field_patterns
        self.duplicate_prevention = duplicate_prevention
        self.settings = settings
        self._create_enhanced_element_info = create_enhanced_element_info_func
        self._generate_temp_field_value = generate_temp_value_func
        self.field_combination_manager = field_combination_manager
        self.unified_field_info: Dict[str, Any] = {}
        self.form_type_info: Dict[str, Any] = {}

    async def execute_enhanced_field_mapping(self, classified_elements: Dict[str, List[Locator]], 
                                             unified_field_info: Dict[str, Any], 
                                             form_type_info: Dict[str, Any],
                                             element_bounds_cache: Dict[str, Any],
                                             required_analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
        self.unified_field_info = unified_field_info
        self.form_type_info = form_type_info
        self._element_bounds_cache = element_bounds_cache
        field_mapping: Dict[str, Any] = {}
        used_elements: set[int] = set()
        essential_fields_completed: set[str] = set()
        sorted_patterns = self.field_patterns.get_sorted_patterns_by_weight()

        required_elements_set = set((required_analysis or {}).get('required_elements', []))

        for field_name, field_patterns in sorted_patterns:
            if self.field_combination_manager.is_deprecated_field(field_name):
                continue
            if self._should_skip_field_for_unified(field_name) or self._should_skip_field_for_form_type(field_name):
                continue

            target_element_types = self._determine_target_element_types(field_patterns)

            # 汎用改善: 『お問い合わせ本文』は textarea が存在する場合は textarea のみを候補に限定
            # 背景: 一部フォームで context が強い input[type="text"] が誤って本文に選ばれるケースがあるため、
            #       まず textarea を厳格に優先し、textarea が無い場合のみ input を検討する。
            try:
                if field_name == 'お問い合わせ本文':
                    textareas = classified_elements.get('textareas', []) or []
                    if len(textareas) > 0:
                        target_element_types = ['textareas']
            except Exception:
                pass
            
            best_element, best_score, best_score_details, best_context = await self._find_best_element(
                field_name, field_patterns, classified_elements, target_element_types, used_elements, essential_fields_completed,
                required_elements_set
            )

            # コア項目判定に基づくマッピング決定
            is_core_field = self._is_core_field(field_name)
            base_threshold = self.settings['min_score_threshold']
            # 非コア項目は品質優先の動的閾値を使用して誤検出を抑制
            dynamic_threshold = self._get_dynamic_quality_threshold(field_name, essential_fields_completed)
            
            # 必須フィールドが検出されなかった場合は全フィールドをマッピング対象とする
            treat_all_as_required = required_analysis and required_analysis.get('treat_all_as_required', False)
            # required_analysisで検出された必須要素（name/id一致）に紐づく場合は必ずマッピング対象
            is_required_match = False
            if best_score_details:
                ei = best_score_details.get('element_info', {})
                cand_name = (ei.get('name') or '').strip()
                cand_id = (ei.get('id') or '').strip()
                if cand_name in required_elements_set or cand_id in required_elements_set:
                    is_required_match = True

            # treat_all_as_required は汎用誤入力を避けるため、
            # 本質的必須（essential_fields）に限定して有効化する
            should_map_field = (
                is_core_field or
                is_required_match or
                (bool(treat_all_as_required) and (field_name in self.settings.get('essential_fields', [])))
            )

            # 高信頼かつ汎用的に安全な任意項目（例: 件名・電話番号）は、
            # 必須一致でなくても動的しきい値を満たせば採用を許可する。
            # 背景: 多くの日本語フォームでは件名/電話は任意だが、入力しても副作用が少なく、
            # 自動送信の成功率向上に寄与するため。
            # 注意: フォーム特化の条件は追加しない（汎用改善のみ）。
            if (not should_map_field) and best_element:
                if field_name in self.OPTIONAL_HIGH_PRIORITY_FIELDS and best_score >= dynamic_threshold:
                    should_map_field = True

            # マッピング判定ロジック（精度向上版）
            map_ok = False
            if best_element and should_map_field:
                if is_core_field:
                    # コア項目のうち『お問い合わせ本文』は誤検出を防ぐため、
                    # 必須一致だけでは採用せず、最低スコアを満たすか textarea の場合のみ採用
                    if field_name == 'お問い合わせ本文':
                        tag_name = (best_score_details.get('element_info', {}) or {}).get('tag_name', '').lower()
                        map_ok = (best_score >= base_threshold) or (tag_name == 'textarea')
                    else:
                        # それ以外のコア項目は「必須一致」または最低閾値クリアで採用。
                        # ただし姓/名など一部フィールドはフィールド別しきい値を厳守して安全側に倒す。
                        per_field_thresholds = (self.settings.get('min_score_threshold_per_field', {}) or {})
                        required_threshold = per_field_thresholds.get(field_name, base_threshold)
                        # コア項目でも required 一致だけでの採用は安全側に制限。
                        # → 姓/名などは required 一致があっても required_threshold を満たさない場合は採用しない。
                        if field_name in per_field_thresholds:
                            map_ok = best_score >= required_threshold
                        else:
                            map_ok = is_required_match or (best_score >= required_threshold)
                else:
                    # 非コア項目は必須一致だけでは採用しない。
                    # スコアが動的閾値（品質優先）を満たす場合のみ採用。
                    map_ok = best_score >= dynamic_threshold

            # フィールド固有の安全ガード
            if map_ok and field_name == 'メールアドレス':
                try:
                    ei = (best_score_details or {}).get('element_info', {})
                    etype = (ei.get('type') or '').lower()
                    attrs_blob = ' '.join([
                        (ei.get('name') or ''), (ei.get('id') or ''), (ei.get('class') or ''), (ei.get('placeholder') or '')
                    ]).lower()
                    best_txt = (self.context_text_extractor.get_best_context_text(best_context) or '').lower() if best_context else ''
                    email_tokens = ['email', 'e-mail', 'mail', 'メール']
                    is_semantic_email = any(t in attrs_blob for t in email_tokens) or any(t in best_txt for t in email_tokens)
                    # type=email か、強いメール語が属性またはラベルにない限り採用しない
                    if not (etype == 'email' or is_semantic_email):
                        map_ok = False
                except Exception:
                    pass

            # 電話番号の安全ガード
            if map_ok and field_name == '電話番号':
                try:
                    ei = (best_score_details or {}).get('element_info', {})
                    etype = (ei.get('type') or '').lower()
                    attrs_blob = ' '.join([
                        (ei.get('name') or ''), (ei.get('id') or ''), (ei.get('class') or ''), (ei.get('placeholder') or '')
                    ]).lower()
                    best_txt = (self.context_text_extractor.get_best_context_text(best_context) or '').lower() if best_context else ''
                    pos_attr = any(t in attrs_blob for t in ['tel', 'phone'])
                    pos_ctx = any(t in best_txt for t in ['電話','tel','phone','携帯','mobile','cell'])
                    neg_ctx = any(t in best_txt for t in ['時', '時頃', '午前', '午後', '連絡方法']) or any(t in attrs_blob for t in ['timeno', 'h1', 'h2'])
                    if not (etype == 'tel' or pos_attr or (pos_ctx and not neg_ctx)):
                        map_ok = False
                except Exception:
                    pass

            if map_ok:
                element_info = await self._create_enhanced_element_info(best_element, best_score_details, best_context)
                try:
                    element_info['source'] = 'normal'
                except Exception:
                    pass
                temp_value = self._generate_temp_field_value(field_name)
                
                if self.duplicate_prevention.register_field_assignment(field_name, temp_value, best_score, element_info):
                    field_mapping[field_name] = element_info
                    used_elements.add(id(best_element))
                    if field_name in self.settings.get('essential_fields', []):
                        essential_fields_completed.add(field_name)
                    logger.info(f"Mapped '{field_name}' with score {best_score}")
        
        # 必須保証フェーズ（取りこぼし救済）
        try:
            await self._ensure_required_mappings(classified_elements, field_mapping, used_elements, required_elements_set)
        except Exception as e:
            logger.debug(f"Ensure required mappings failed: {e}")

        # フォールバック: 重要コア項目の取りこぼし救済
        await self._fallback_map_message_field(classified_elements, field_mapping, used_elements)
        await self._fallback_map_email_field(classified_elements, field_mapping, used_elements)
        return field_mapping

    async def _find_best_element(self, field_name, field_patterns, classified_elements, target_element_types, used_elements, essential_fields_completed, required_elements_set:set) -> Tuple[Optional[Locator], float, Dict, List]:
        best_element, best_score, best_score_details, best_context = None, 0.0, {}, []
        candidate_elements = [el for el_type in target_element_types for el in classified_elements.get(el_type, [])]

        elements_to_evaluate = await self._quick_rank_candidates(candidate_elements, field_patterns, field_name, used_elements)

        early_stopped = False
        for element in elements_to_evaluate:
            score, score_details, contexts = await self._score_element_in_detail(element, field_patterns, field_name)
            if score <= 0: continue

            active_threshold = self._get_dynamic_quality_threshold(field_name, essential_fields_completed)
            # 候補が必須に該当する場合、しきい値を最小化（必須は落とさない）
            try:
                ei = score_details.get('element_info', {})
                cand_name = (ei.get('name') or '').strip()
                cand_id = (ei.get('id') or '').strip()
                if cand_name in required_elements_set or cand_id in required_elements_set:
                    active_threshold = self.settings['min_score_threshold']
            except Exception:
                pass
            if score > best_score and score >= active_threshold:
                best_element, best_score, best_score_details, best_context = element, score, score_details, contexts
                if self._check_early_stop(field_name, score, score_details, contexts, field_patterns):
                    early_stopped = True
                    break
        if early_stopped:
            logger.debug(f"Early stop for '{field_name}'")
        return best_element, best_score, best_score_details, best_context

    async def _quick_rank_candidates(self, elements, field_patterns, field_name, used_elements):
        if not self.settings.get('quick_ranking_enabled', True):
            return [el for el in elements if id(el) not in used_elements]
        
        quick_scored = []
        for el in elements:
            if id(el) in used_elements: continue
            try:
                q_score = await self.element_scorer.calculate_element_score_quick(el, field_patterns, field_name)
                if q_score > -900: quick_scored.append((q_score, el))
            except Exception: continue
        
        quick_scored.sort(key=lambda x: x[0], reverse=True)
        top_k = self.settings['quick_top_k_essential'] if field_name in self.settings['essential_fields'] else self.settings['quick_top_k']
        return [el for _, el in quick_scored[:top_k]]

    async def _score_element_in_detail(self, element, field_patterns, field_name):
        element_bounds = self._element_bounds_cache.get(str(element))
        # 情報付与用にコンテキストは取得するが、採点は ElementScorer に一元化する
        contexts = await self.context_text_extractor.extract_context_for_element(element, element_bounds)
        score, score_details = await self.element_scorer.calculate_element_score(element, field_patterns, field_name)
        if score <= 0:
            return 0, {}, []
        # ここでの追加ボーナスは廃止（重複加点防止）
        return score, score_details, contexts

    def _is_confirmation_field(self, element_info: Dict[str, Any], contexts: List) -> bool:
        """属性とコンテキスト（ラベル/見出し）から確認用入力欄を判定"""
        confirm_tokens = [t.lower() for t in self.settings.get('confirm_tokens', ['confirm','confirmation','確認','確認用','再入力','もう一度','再度'])]
        try:
            attrs = ' '.join([
                (element_info.get('name') or ''),
                (element_info.get('id') or ''),
                (element_info.get('class') or ''),
                (element_info.get('placeholder') or ''),
            ]).lower()
        except Exception:
            attrs = ''
        if any(tok in attrs for tok in confirm_tokens):
            return True
        try:
            best_txt = (self.context_text_extractor.get_best_context_text(contexts) or '').lower()
        except Exception:
            best_txt = ''
        return any(tok in best_txt for tok in confirm_tokens)

    def _check_early_stop(self, field_name, score, score_details, contexts, field_patterns):
        if not self.settings.get('early_stop_enabled', True) or field_name not in self.settings.get('essential_fields', []):
            return False
        ei = score_details.get('element_info', {})
        tag = (ei.get('tag_name') or '').lower()
        typ = (ei.get('type') or '').lower()
        strong_type = (field_name == 'メールアドレス' and typ == 'email') or (field_name == 'お問い合わせ本文' and tag == 'textarea')
        strict_patterns = field_patterns.get('strict_patterns', [])
        best_txt = (self.context_text_extractor.get_best_context_text(contexts) or '').lower()
        has_strict = any(sp.lower() in best_txt for sp in strict_patterns)
        return strong_type and has_strict and score >= self.settings.get('early_stop_score', 95)

    async def _fallback_map_message_field(self, classified_elements, field_mapping, used_elements):
        """本文取りこぼし救済

        優先度:
        1) textarea があれば textarea のみを対象に厳格に判定
        2) textarea が無い場合に限り、input[type=text] を強い本文ラベルに基づき限定的に救済
        """
        target_field = 'お問い合わせ本文'
        if target_field in field_mapping:
            return

        patterns = self.field_patterns.get_pattern(target_field) or {}
        strict_tokens = {'お問い合わせ', '本文', 'メッセージ', 'ご要望', 'ご質問', '備考'}

        # 1) textarea 優先（従来ロジック）
        textarea_candidates = classified_elements.get('textareas', []) or []
        best = (None, 0, None, [])
        for el in textarea_candidates:
            if id(el) in used_elements:
                continue
            el_bounds = self._element_bounds_cache.get(str(el)) if hasattr(self, '_element_bounds_cache') else None
            contexts = await self.context_text_extractor.extract_context_for_element(el, el_bounds)
            best_txt = (self.context_text_extractor.get_best_context_text(contexts) or '').lower()
            if not any(tok in best_txt for tok in strict_tokens):
                continue
            score, details = await self.element_scorer.calculate_element_score(el, patterns, target_field)
            if score > best[1]:
                best = (el, score, details, contexts)

        el, score, details, contexts = best
        if el and score >= 60:
            info = await self._create_enhanced_element_info(el, details, contexts)
            try:
                info['source'] = 'fallback'
            except Exception:
                pass
            tmp = self._generate_temp_field_value(target_field)
            if self.duplicate_prevention.register_field_assignment(target_field, tmp, score, info):
                field_mapping[target_field] = info
                logger.info(f"Fallback mapped '{target_field}' via textarea label-context (score {score})")
            return

        # 2) textarea が無い場合のみ、text input を限定救済
        text_inputs = classified_elements.get('text_inputs', []) or []
        if textarea_candidates or not text_inputs:
            return

        best = (None, 0, None, [])
        for el in text_inputs:
            if id(el) in used_elements:
                continue
            try:
                ei = await self.element_scorer._get_element_info(el)
            except Exception:
                ei = {}
            # name/id/class の属性に本文系語が含まれるか（誤検出抑止の補助）
            blob = ' '.join([(ei.get('name') or ''), (ei.get('id') or ''), (ei.get('class') or '')]).lower()
            attr_hint = any(k in blob for k in ['message', 'inquiry', 'comment', 'content', 'details'])

            el_bounds = self._element_bounds_cache.get(str(el)) if hasattr(self, '_element_bounds_cache') else None
            contexts = await self.context_text_extractor.extract_context_for_element(el, el_bounds)
            best_txt = (self.context_text_extractor.get_best_context_text(contexts) or '').lower()
            if not any(tok in best_txt for tok in strict_tokens):
                continue
            # 文脈の強さ + 属性ヒントの双方がある場合のみ採点・救済対象
            if not attr_hint:
                continue

            s, details = await self.element_scorer.calculate_element_score(el, patterns, target_field)
            # 安全側の救済閾値（email_fallback と同等レベル以上）
            if s > best[1]:
                best = (el, s, details, contexts)

        el, score, details, contexts = best
        if el and score >= max(65, int(self.settings.get('email_fallback_min_score', 60))):
            info = await self._create_enhanced_element_info(el, details, contexts)
            try:
                info['source'] = 'fallback'
            except Exception:
                pass
            tmp = self._generate_temp_field_value(target_field)
            if self.duplicate_prevention.register_field_assignment(target_field, tmp, score, info):
                field_mapping[target_field] = info
                logger.info(f"Fallback mapped '{target_field}' via text-input label+attr (score {score})")

    async def _fallback_map_email_field(self, classified_elements, field_mapping, used_elements):
        """メールアドレスの取りこぼし救済

        - type="email" が存在しない/見つからないフォームで、type="text"のメール欄を
          強いラベルコンテキスト（th/dt/label）に基づいて安全に昇格させる。
        - 確認用（confirm/check）や確認入力欄（placeholderに確認を含む）は除外。
        """
        target_field = 'メールアドレス'
        if target_field in field_mapping:
            return

        patterns = self.field_patterns.get_pattern(target_field) or {}
        strict_tokens = {'メールアドレス', 'メール', 'email', 'e-mail'}
        confirm_tokens = {'confirm', 'confirmation', '確認', '確認用', '再入力', 'もう一度', '再度'}

        candidates = []
        # 優先: email_inputs、その後 text_inputs
        for bucket in ['email_inputs', 'text_inputs']:
            for el in classified_elements.get(bucket, []) or []:
                if id(el) in used_elements:
                    continue
                try:
                    ei = await self.element_scorer._get_element_info(el)
                    # 確認用/チェック用を除外
                    blob = ' '.join([
                        (ei.get('name') or ''), (ei.get('id') or ''), (ei.get('class') or ''), (ei.get('placeholder') or '')
                    ]).lower()
                    # 確認用の強いシグナルのみで除外（"check" 単独では除外しない）
                    if any(k in blob for k in confirm_tokens):
                        continue
                    # コンテキストに強いメール語が含まれるか
                    el_bounds = self._element_bounds_cache.get(str(el)) if hasattr(self, '_element_bounds_cache') else None
                    contexts = await self.context_text_extractor.extract_context_for_element(el, el_bounds)
                    # 確認用フィールドは除外
                    if self._is_confirmation_field(ei, contexts):
                        continue
                    best_txt = (self.context_text_extractor.get_best_context_text(contexts) or '').lower()
                    if not any(tok in best_txt for tok in [t.lower() for t in strict_tokens]):
                        continue
                    # input[type=email] は基本的に候補に含める（上の確認用除外に既に通している）
                    # スコア計算
                    score, details = await self.element_scorer.calculate_element_score(el, patterns, target_field)
                    if score <= 0:
                        continue
                    candidates.append((score, el, details, contexts))
                except Exception:
                    continue

        if not candidates:
            return

        candidates.sort(key=lambda x: x[0], reverse=True)
        score, el, details, contexts = candidates[0]
        # 設定化した安全側の閾値
        if score >= int(self.settings.get('email_fallback_min_score', 60)):
            info = await self._create_enhanced_element_info(el, details, contexts)
            try:
                info['source'] = 'fallback'
            except Exception:
                pass
            tmp = self._generate_temp_field_value(target_field)
            if self.duplicate_prevention.register_field_assignment(target_field, tmp, score, info):
                field_mapping[target_field] = info
                logger.info(f"Fallback mapped '{target_field}' via label-context (score {score})")

    def _determine_target_element_types(self, field_patterns: Dict[str, Any]) -> List[str]:
        """候補バケットをフィールド定義から厳選

        - tag が input の場合は `text_inputs` を優先し、不要な `textareas` を含めない
        - types が email/tel/url/number を含む場合はそれぞれを追加
        - tag が textarea/select の場合はそのバケットを追加
        - いずれにも該当しない場合のみ汎用フォールバック（ただし textareas は除外）
        """
        target_types = set()
        pattern_types = field_patterns.get('types', [])
        pattern_tags = field_patterns.get('tags', [])

        # types に基づく厳密な候補
        if 'email' in pattern_types:
            target_types.add('email_inputs')
        if 'tel' in pattern_types:
            target_types.update(['tel_inputs', 'text_inputs'])
        if 'url' in pattern_types:
            target_types.add('url_inputs')
        if 'number' in pattern_types:
            target_types.add('number_inputs')
        if 'text' in pattern_types:
            target_types.add('text_inputs')

        # tag による候補
        if 'input' in pattern_tags:
            target_types.add('text_inputs')
        if 'textarea' in pattern_tags:
            target_types.add('textareas')
        if 'select' in pattern_tags:
            target_types.add('selects')

        # フォールバック（textareas は含めない＝精度優先）
        if not target_types:
            target_types.update(['text_inputs', 'email_inputs', 'tel_inputs'])

        return list(target_types)

    def _should_skip_field_for_unified(self, field_name: str) -> bool:
        # 統合氏名（漢字）がある場合は「姓」「名」のみスキップ
        if self.unified_field_info.get('has_fullname') and field_name in ['姓', '名']:
            return True
        # 統合カナがある場合は分割カナをスキップ
        if self.unified_field_info.get('has_kana_unified') and field_name in ['姓カナ', '名カナ']:
            return True
        # 統合ひらがながある場合は分割ひらがなをスキップ
        if self.unified_field_info.get('has_hiragana_unified') and field_name in ['姓ひらがな', '名ひらがな']:
            return True
        # 統合電話がある場合は分割電話をスキップ
        if self.unified_field_info.get('has_phone_unified') and field_name in ['電話1', '電話2', '電話3']:
            return True
        # 汎用改善: 分割姓名が存在する場合は、統合氏名をスキップして先に分割を優先
        # 理由:
        #  - 統合氏名が先に確定すると最初の入力欄を占有し、
        #    『姓/名』どちらか一方の取りこぼしや誤マッピングが発生しやすい。
        #  - FormPreProcessor 側ではカナ/ひらがな要素を除外したうえで、
        #    漢字の分割姓名が別要素として存在する場合のみ has_name_split_fields を True にしている。
        if field_name == '統合氏名' and self.unified_field_info.get('has_name_split_fields'):
            try:
                logger.info("Skip '統合氏名' due to detected split name fields (prefer 分割: 姓/名)")
            except Exception:
                pass
            return True
        return False

    async def _ensure_required_mappings(self, classified_elements: Dict[str, List[Locator]],
                                         field_mapping: Dict[str, Dict[str, Any]], used_elements: set,
                                         required_elements_set: set) -> None:
        """必須要素を必ず field_mapping に登録する救済フェーズ"""
        if not required_elements_set:
            return

        # 既に使用済みname/idの組を控える
        used_names_ids = set()
        for info in field_mapping.values():
            try:
                used_names_ids.add((info.get('name',''), info.get('id','')))
            except Exception:
                pass

        # 走査対象
        buckets = ['email_inputs','tel_inputs','url_inputs','number_inputs','text_inputs','textareas','selects','radios','checkboxes']
        auto_counter = 1
        for bucket in buckets:
            for el in classified_elements.get(bucket, []):
                try:
                    if id(el) in used_elements:
                        continue
                    ei = await self.element_scorer._get_element_info(el)
                    nm = (ei.get('name') or '').strip()
                    idv = (ei.get('id') or '').strip()
                    if not (nm in required_elements_set or idv in required_elements_set):
                        continue

                    if (nm, idv) in used_names_ids:
                        continue

                    if self._is_nonfillable_required(ei):
                        continue

                    contexts = await self.context_text_extractor.extract_context_for_element(el)
                    field_name = self._infer_logical_field_name_for_required(ei, contexts)

                    # 汎用テキスト（auto_required_text_*）は誤入力リスクが高く、
                    # マッピング品質を下げるため救済登録の対象から除外する。
                    # （必要に応じて auto-handled 側の処理に委譲）
                    if field_name.startswith('auto_required_text_'):
                        continue

                    # 同一論理フィールドが既に確定している場合は重複登録を抑止
                    if field_name in field_mapping:
                        continue
                    # 必須救済で登録する要素は、評価スコアが 0 のままだと
                    # downstream の評価（例: テストの低信頼アラート）で誤検知される。
                    # そこで、救済登録時は安全側の保守的なスコアを与える。
                    # しきい値は settings の最小スコア閾値を使用（デフォルト: 70）。
                    salvage_score = max(15, int(self.settings.get('min_score_threshold', 70)))
                    details = {'element_info': ei, 'total_score': salvage_score}
                    info = await self._create_enhanced_element_info(el, details, contexts)
                    try:
                        info['source'] = 'required_rescue'
                    except Exception:
                        pass
                    info['required'] = True

                    temp_value = self._generate_temp_field_value(field_name)
                    # 重複抑止にも救済スコアを渡しておく（後続の参照整合のため）
                    if self.duplicate_prevention.register_field_assignment(field_name, temp_value, salvage_score, info):
                        key = field_name
                        field_mapping[key] = info
                        used_elements.add(id(el))
                        used_names_ids.add((nm, idv))
                except Exception as e:
                    logger.debug(f"Ensure required mapping for element failed: {e}")

    def _infer_logical_field_name_for_required(self, element_info: Dict[str, Any], contexts: List) -> str:
        tag = (element_info.get('tag_name') or '').lower()
        typ = (element_info.get('type') or '').lower()
        name_id_cls = ' '.join([(element_info.get('name') or ''), (element_info.get('id') or ''), (element_info.get('class') or '')]).lower()
        try:
            ctx_text = (self.context_text_extractor.get_best_context_text(contexts) or '').lower()
        except Exception:
            ctx_text = ''

        if tag == 'input' and typ == 'email':
            return 'メールアドレス'
        if tag == 'input' and typ == 'tel':
            return '電話番号'
        if tag == 'textarea':
            return 'お問い合わせ本文'

        # 汎用入力(type=text)でも文脈/属性から論理フィールドを推定（救済判定）
        # 1) メールアドレス: ラベル/見出し/placeholder/属性にメール系語が含まれる
        email_tokens = ['メール', 'e-mail', 'email', 'mail']
        confirm_tokens = ['confirm', 'confirmation', '確認', '確認用', '再入力', 'もう一度', '再度']
        if tag == 'input' and typ in ['', 'text']:
            if any(tok in ctx_text for tok in email_tokens) or any(tok in name_id_cls for tok in ['email', 'mail']):
                if not self._is_confirmation_field(element_info, contexts):
                    return 'メールアドレス'
            # 2) 電話番号: 文脈/属性に電話系語が含まれる
            if any(tok in ctx_text for tok in ['電話', 'tel', 'phone', 'telephone']) or any(tok in name_id_cls for tok in ['tel', 'phone']):
                return '電話番号'

        # --- Name field inference (split aware) ---
        kana_tokens = ['kana','furigana','katakana','カナ','カタカナ','フリガナ']
        hira_tokens = ['hiragana','ひらがな']
        last_tokens = ['lastname','last_name','last-name','last','family-name','family_name','surname','sei','姓']
        # 『名』単独は住所系の『マンション名』等に誤反応しやすいため除外し、
        # より明確な表現のみを用いる
        first_tokens = ['firstname','first_name','first-name','first','given-name','given_name','forename','mei','お名前','名前']
        has_kana = any(t in name_id_cls for t in kana_tokens) or ('フリガナ' in ctx_text)
        has_hira = any(t in name_id_cls for t in hira_tokens) or ('ひらがな' in ctx_text)
        is_last = any(t in name_id_cls for t in last_tokens) or any(t in ctx_text for t in ['姓','せい','苗字'])
        # 非個人名（会社名/商品名/部署名/建物名…）の文脈では『名』の判定を抑止
        from .element_scorer import ElementScorer
        non_personal_ctx = bool(ElementScorer.NON_PERSONAL_NAME_PATTERN.search(ctx_text or ''))
        is_first_token_hit = any(t in name_id_cls for t in first_tokens) or any(t in ctx_text for t in ['名','めい'])
        is_first = is_first_token_hit and not non_personal_ctx
        has_kanji = ('kanji' in name_id_cls) or ('漢字' in ctx_text)

        # Prioritize split-specific logical names when tokens available
        if has_kana:
            if is_last and not is_first:
                return '姓カナ'
            if is_first and not is_last:
                return '名カナ'
            # ambiguous kana → unified kana
            return '統合氏名カナ'

        if has_hira:
            if is_last and not is_first:
                return '姓ひらがな'
            if is_first and not is_last:
                return '名ひらがな'
            # ambiguous hiragana → fall back to unified kana policy (none specified); keep as auto text

        # Kanji or unspecified script
        if is_last and not is_first:
            return '姓'
        if is_first and not is_last:
            return '名'

        # Unified fallbacks
        if any(tok in ctx_text for tok in ['お名前','氏名','おなまえ']) or any(t in name_id_cls for t in ['your-name','fullname','full_name','name']):
            return '統合氏名'

        return 'auto_required_text_1'

    def _is_nonfillable_required(self, element_info: Dict[str, Any]) -> bool:
        name_id_cls = ' '.join([(element_info.get('name') or ''), (element_info.get('id') or ''), (element_info.get('class') or '')]).lower()
        input_type = (element_info.get('type') or '').lower()
        tag = (element_info.get('tag_name') or '').lower()

        # 1) 技術的に自動入力しない対象（認証/確認/トークン等）
        blacklist = ['captcha','image_auth','image-auth','spam-block','token','otp','verification',
                     'email_confirm','mail_confirm','email_confirmation','confirm_email','confirm','re_email','re-mail']
        if any(b in name_id_cls for b in blacklist):
            return True

        # 2) クリック/選択系は ensure_required で直接マッピングせず、自動ハンドラに委譲
        #    - checkbox, radio は _auto_handle_checkboxes / _auto_handle_radios
        #    - select は _auto_handle_selects
        if input_type in ['checkbox', 'radio']:
            return True
        if tag == 'select':
            return True

        return False

    def _should_skip_field_for_form_type(self, field_name: str) -> bool:
        return field_name in self.form_type_info.get('irrelevant_fields', [])

    def _get_dynamic_quality_threshold(self, field_name: str, essential_fields_completed: set) -> float:
        # フィールド別の最低スコアしきい値（優先採用）
        per_field = (self.settings.get('min_score_threshold_per_field', {}) or {})
        if field_name in per_field:
            return per_field[field_name]
        
        # 必須フィールド（essential_fields）は常に標準閾値
        if field_name in self.settings.get('essential_fields', []):
            return self.settings['min_score_threshold']
        
        # 品質優先モードでない場合は標準閾値
        if not self.settings.get('quality_first_mode', False):
            return self.settings['min_score_threshold']
        
        # 重要だが任意になりがちなフィールドは、
        # 必須項目完了後でも過度にしきい値を吊り上げない（誤検出抑止と網羅性のバランス）
        if field_name in self.OPTIONAL_HIGH_PRIORITY_FIELDS:
            return min(self.settings['min_score_threshold'] + self.settings['quality_threshold_boost'],
                       self.settings['max_quality_threshold'])
        
        # 任意フィールドに対しては高い閾値を設定（不要な入力を回避）
        # 特に必須フィールドが全て完了した後は、さらに高い閾値を適用
        if len(essential_fields_completed) >= len(self.settings.get('essential_fields', [])):
            # 必須フィールド完了後：任意フィールドは非常に高い信頼度が必要
            return min(self.settings['min_score_threshold'] + self.settings['quality_threshold_boost'] + 50, 400)
        else:
            # 必須フィールド未完了：任意フィールドも高い閾値
            return min(self.settings['min_score_threshold'] + self.settings['quality_threshold_boost'], self.settings['max_quality_threshold'])
        
        return self.settings['min_score_threshold']

    def _is_core_field(self, field_name: str) -> bool:
        """
        コア項目かどうかを判定（入力すべき重要なフィールド）
        
        Args:
            field_name: フィールド名
            
        Returns:
            コア項目の場合True
        """
        # コア項目の定義：フォーム送信に必要最小限の項目
        core_fields = {
            # 氏名系
            "統合氏名", "姓", "名",
            
            # 会社情報
            "会社名",
            
            # 連絡手段
            "メールアドレス",
            
            # 本文・内容
            "お問い合わせ本文"
        }
        
        return field_name in core_fields

    def _calculate_context_bonus(self, contexts, field_name: str, field_patterns: Dict[str, Any]) -> float:
        if not contexts: return 0.0
        bonus = 0.0
        best_context = max(contexts, key=lambda x: x.confidence)
        strict_patterns = field_patterns.get('strict_patterns', [])
        matched = any(p.lower() in best_context.text.lower() for p in strict_patterns)
        if matched:
            bonus += 20 + best_context.confidence * 15
        return min(bonus, 50)
