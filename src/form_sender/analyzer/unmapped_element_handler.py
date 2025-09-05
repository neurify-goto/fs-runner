import logging
from typing import Dict, List, Any, Optional, Callable, Awaitable, Tuple
from playwright.async_api import Page, Locator

from .element_scorer import ElementScorer
from .context_text_extractor import ContextTextExtractor
from .field_combination_manager import FieldCombinationManager
from .form_structure_analyzer import FormStructure

logger = logging.getLogger(__name__)


class UnmappedElementHandler:
    """未マッピング要素の自動処理を担当するクラス"""

    def __init__(
        self,
        page: Page,
        element_scorer: ElementScorer,
        context_text_extractor: ContextTextExtractor,
        field_combination_manager: FieldCombinationManager,
        settings: Dict[str, Any],
        generate_playwright_selector_func: Callable[[Locator], Awaitable[str]],
        get_element_details_func: Callable[[Locator], Awaitable[Dict[str, Any]]],
        field_patterns,
    ):
        self.page = page
        self.element_scorer = element_scorer
        self.context_text_extractor = context_text_extractor
        self.field_combination_manager = field_combination_manager
        self.settings = settings
        self._generate_playwright_selector = generate_playwright_selector_func
        self._get_element_details = get_element_details_func
        self.field_patterns = field_patterns
        # 近傍コンテナの必須検出結果キャッシュ
        self._container_required_cache: Dict[str, bool] = {}

        # 必須マーカー（重複利用を避けるためクラス定数的に保持）
        self.REQUIRED_MARKERS = ['*','※','必須','Required','Mandatory','Must','(必須)','（必須）','[必須]','［必須］']

    async def _detect_group_required_via_container(self, first_radio: Locator) -> bool:
        """見出しコンテナ側の必須マーカーを探索して判定（設定化・キャッシュ付）。"""
        try:
            key = await self._generate_playwright_selector(first_radio)
        except Exception:
            key = str(first_radio)
        if key in self._container_required_cache:
            return self._container_required_cache[key]

        max_depth = int(self.settings.get('radio_required_max_container_depth', 6))
        sib_depth = int(self.settings.get('radio_required_max_sibling_depth', 2))
        js = f"""
            (el) => {{
              const MARKERS = ['*','※','必須','Required','Mandatory','Must','(必須)','（必須）','[必須]','［必須］'];
              const hasMarker = (node) => {{
                if (!node) return false;
                const txt = (node.innerText || node.textContent || '').trim();
                if (!txt) return false;
                return MARKERS.some(m => txt.includes(m));
              }};
              let p = el; let depth = 0;
              while (p && depth < {max_depth}) {{
                const tag = (p.tagName || '').toLowerCase();
                if (['p','div','li','fieldset','dd'].includes(tag)) {{
                  if (hasMarker(p)) return true;
                  let sib = p.previousElementSibling; let sdepth = 0;
                  while (sib && sdepth < {sib_depth}) {{
                    if (hasMarker(sib)) return true;
                    sib = sib.previousElementSibling; sdepth++;
                  }}
                }}
                p = p.parentElement; depth++;
              }}
              return false;
            }}
        """
        try:
            found = bool(await first_radio.evaluate(js))
        except Exception as e:
            logger.debug(f"Container required detection failed: {e}")
            found = False
        self._container_required_cache[key] = found
        return found

    async def handle_unmapped_elements(
        self,
        classified_elements: Dict[str, List[Locator]],
        field_mapping: Dict[str, Dict[str, Any]],
        client_data: Optional[Dict[str, Any]],
        form_structure: Optional[FormStructure],
    ) -> Dict[str, Dict[str, Any]]:
        if not self.settings.get("enable_auto_handling", True):
            return {}

        auto_handled = {}
        mapped_element_ids = {id(info["element"]) for info in field_mapping.values()}

        checkbox_handled = await self._auto_handle_checkboxes(
            classified_elements.get("checkboxes", []), mapped_element_ids
        )
        auto_handled.update(checkbox_handled)

        radio_handled = await self._auto_handle_radios(
            classified_elements.get("radios", []), mapped_element_ids, client_data
        )
        auto_handled.update(radio_handled)

        select_handled = await self._auto_handle_selects(
            classified_elements.get("selects", []), mapped_element_ids, client_data
        )
        auto_handled.update(select_handled)

        email_conf = await self._auto_handle_email_confirmation(
            classified_elements.get("email_inputs", [])
            + classified_elements.get("text_inputs", []),
            mapped_element_ids,
        )
        auto_handled.update(email_conf)

        # 統合氏名が既にマッピングされている場合は、auto_fullname 系の生成を抑止
        if "統合氏名" not in field_mapping:
            fullname_handled = await self._auto_handle_unified_fullname(
                classified_elements.get("text_inputs", []),
                mapped_element_ids,
                client_data,
                form_structure,
            )
            auto_handled.update(fullname_handled)

        # 統合カナが既にマッピング済み、または分割カナ(姓カナ/名カナ)が両方揃っている場合は
        # auto_unified_kana_* の生成を抑止して重複入力を防ぐ
        has_unified_kana = "統合氏名カナ" in field_mapping
        has_split_kana = ("姓カナ" in field_mapping) and ("名カナ" in field_mapping)
        if not (has_unified_kana or has_split_kana):
            kana_handled = await self._auto_handle_unified_kana(
                classified_elements.get("text_inputs", []),
                mapped_element_ids,
                client_data,
                form_structure,
            )
            auto_handled.update(kana_handled)

        logger.info(
            f"Auto-handled elements: checkboxes={len(checkbox_handled)}, radios={len(radio_handled)}, selects={len(select_handled)}"
        )
        return auto_handled

    async def _auto_handle_checkboxes(
        self, checkboxes: List[Locator], mapped_element_ids: set
    ) -> Dict[str, Dict[str, Any]]:
        handled: Dict[str, Dict[str, Any]] = {}
        try:
            groups: Dict[str, List[Tuple[Locator, Dict[str, Any]]]] = {}
            for cb in checkboxes:
                if id(cb) in mapped_element_ids:
                    continue
                info = await self.element_scorer._get_element_info(cb)
                if not info.get("visible", True):
                    continue
                key = info.get("name") or info.get("id") or f"cb_{id(cb)}"
                groups.setdefault(key, []).append((cb, info))

            pri1 = ["営業", "提案", "メール"]
            pri2 = ["その他"]

            for group_key, items in groups.items():
                group_required = False
                for cb, info in items:
                    if await self.element_scorer._detect_required_status(cb):
                        group_required = True
                        break
                    name_id_class = " ".join(
                        [
                            info.get("name", ""),
                            info.get("id", ""),
                            info.get("class", ""),
                        ]
                    ).lower()
                    if any(
                        k in name_id_class for k in [
                            "acceptance", "consent", "同意", "policy", "privacy", "個人情報", "personal"
                        ]
                    ):
                        group_required = True
                        break
                # 追加: プライバシー/規約同意の文脈検出（name/id/classに現れないケースの補完）
                is_privacy_group = False
                if not group_required:
                    try:
                        privacy_tokens_primary = [
                            "プライバシー", "プライバシーポリシー", "個人情報", "個人情報保護", "個人情報の取り扱い",
                            "privacy", "privacy policy", "個人情報の取扱い", "個人情報保護方針",
                            "利用規約", "terms", "terms of service"
                        ]
                        agree_tokens = ["同意", "承諾", "同意する", "agree", "確認の上", "に同意"]
                        for cb, info in items:
                            contexts = await self.context_text_extractor.extract_context_for_element(cb)
                            # すべてのコンテキストを連結して広く判定（best のみだと取りこぼしが出る）
                            texts = []
                            try:
                                texts = [c.text for c in (contexts or []) if getattr(c, 'text', None)]
                            except Exception:
                                texts = []
                            best = (self.context_text_extractor.get_best_context_text(contexts) or "")
                            blob = (" ".join(texts + [best])).lower()
                            if any(tok in blob for tok in [t.lower() for t in privacy_tokens_primary]):
                                if any(tok in blob for tok in [t.lower() for t in agree_tokens]) or len(items) == 1:
                                    is_privacy_group = True
                                    break
                    except Exception:
                        is_privacy_group = False

                if not group_required and not is_privacy_group:
                    continue

                texts: List[str] = []
                for cb, info in items:
                    contexts = (
                        await self.context_text_extractor.extract_context_for_element(
                            cb
                        )
                    )
                    best = (
                        self.context_text_extractor.get_best_context_text(contexts)
                        if contexts
                        else ""
                    )
                    val = info.get("value", "")
                    texts.append(
                        (
                            best or val or info.get("name", "") or info.get("id", "")
                        ).strip()
                    )

                # プライバシー同意系は「同意/agree」を含む選択肢を優先
                if is_privacy_group:
                    idx = 0
                    for i, t in enumerate(texts):
                        tl = (t or "").lower()
                        if any(k in tl for k in ["同意", "agree", "承諾"]):
                            idx = i
                            break
                else:
                    idx = self._choose_priority_index(texts, pri1, pri2)
                cb, info = items[idx]
                selector = await self._generate_playwright_selector(cb)
                field_name = f"auto_checkbox_{group_key}"
                handled[field_name] = {
                    "element": cb,
                    "selector": selector,
                    "tag_name": "input",
                    "type": "checkbox",
                    "name": info.get("name", ""),
                    "id": info.get("id", ""),
                    "input_type": "checkbox",
                    "auto_action": "check",
                    "selected_index": idx,
                    "selected_option_text": texts[idx],
                    "default_value": True,
                    "required": True,
                    "auto_handled": True,
                }
        except Exception as e:
            logger.debug(f"Error in checkbox auto handling: {e}")
        return handled

    async def _auto_handle_radios(
        self,
        radios: List[Locator],
        mapped_element_ids: set,
        client_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        handled: Dict[str, Dict[str, Any]] = {}
        radio_groups = {}
        for radio in radios:
            if id(radio) in mapped_element_ids:
                continue
            try:
                element_info = await self.element_scorer._get_element_info(radio)
                if not element_info.get("visible", True):
                    continue
                name = element_info.get("name", f"unnamed_radio_{id(radio)}")
                if name not in radio_groups:
                    radio_groups[name] = []
                radio_groups[name].append((radio, element_info))
            except Exception as e:
                logger.debug(f"Error grouping radio: {e}")

        pri1 = ["営業", "提案", "メール"]
        pri2 = ["その他"]

        # クライアント性別の正規化（male/female/other）
        def _normalize_gender(val: str) -> Optional[str]:
            if not val:
                return None
            v = val.strip().lower()
            male_tokens = ["男性", "だんせい", "男", "male", "man"]
            female_tokens = ["女性", "じょせい", "女", "female", "woman"]
            other_tokens = [
                "その他",
                "未回答",
                "無回答",
                "回答しない",
                "other",
                "prefer not",
            ]
            if any(t in v for t in male_tokens):
                return "male"
            if any(t in v for t in female_tokens):
                return "female"
            if any(t in v for t in other_tokens):
                return "other"
            return None

        client_info = (
            client_data.get("client")
            if isinstance(client_data, dict) and "client" in client_data
            else client_data
        ) or {}
        client_gender_norm = _normalize_gender(str(client_info.get("gender", "") or ""))
        for group_name, radio_list in radio_groups.items():
            if not radio_list:
                continue
            is_gender_field = any(
                keyword in group_name.lower()
                for keyword in ["性別", "gender", "sex", "男女"]
            )
            group_required = is_gender_field
            if not group_required:
                for radio, _ in radio_list:
                    if await self.element_scorer._detect_required_status(radio):
                        group_required = True
                        break

            # 追加の汎用判定: コンテキスト上に必須マーカーが存在するか（閾値なしで広く検出）
            # 例: 「お問い合わせ項目 (必須)」のようにグループ見出し側にのみ付くケース
            if not group_required:
                try:
                    group_required = await self._detect_group_required_via_container(radio_list[0][0])
                except Exception as e:
                    logger.debug(f"Container required detection error for group '{group_name}': {e}")
            if not group_required:
                continue

            texts: List[str] = []
            for radio, info in radio_list:
                contexts = (
                    await self.context_text_extractor.extract_context_for_element(radio)
                )
                best = (
                    self.context_text_extractor.get_best_context_text(contexts)
                    if contexts
                    else ""
                )
                val = info.get("value", "")
                texts.append(
                    (best or val or info.get("name", "") or info.get("id", "")).strip()
                )

            # クライアント性別が判明している場合は最優先で一致候補を選択
            idx = None
            if client_gender_norm and is_gender_field:

                def _option_gender(text: str) -> Optional[str]:
                    tl = (text or "").lower()
                    if any(k in tl for k in ["男", "男性", "male"]):
                        return "male"
                    if any(k in tl for k in ["女", "女性", "female"]):
                        return "female"
                    if any(k in tl for k in ["その他", "other"]):
                        return "other"
                    return None

                for i, t in enumerate(texts):
                    if _option_gender(t) == client_gender_norm:
                        idx = i
                        break

            # フォールバック: 既存優先度ロジック
            if idx is None:
                idx = (
                    self._choose_gender_index(texts)
                    if is_gender_field
                    else self._choose_priority_index(texts, pri1, pri2)
                )

            radio, element_info = radio_list[idx]
            selector = await self._generate_playwright_selector(radio)
            field_name = f"auto_radio_{group_name}"
            handled[field_name] = {
                "element": radio,
                "selector": selector,
                "tag_name": "input",
                "type": "radio",
                "name": element_info.get("name", ""),
                "id": element_info.get("id", ""),
                "input_type": "radio",
                "auto_action": "select",
                "selected_index": idx,
                "selected_option_text": texts[idx],
                "default_value": True,
                "required": True,
                "auto_handled": True,
                "group_size": len(radio_list),
            }
        return handled

    async def _auto_handle_selects(
        self,
        selects: List[Locator],
        mapped_element_ids: set,
        client_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        handled: Dict[str, Dict[str, Any]] = {}
        pri1 = ["営業", "提案", "メール"]
        pri2 = ["その他"]
        client_info = (
            client_data.get("client")
            if isinstance(client_data, dict) and "client" in client_data
            else client_data
        ) or {}
        prefecture_target = (client_info.get("address_1") or "").strip()
        client_gender_norm = None

        # 同じ正規化ヘルパー
        def _normalize_gender(val: str) -> Optional[str]:
            if not val:
                return None
            v = val.strip().lower()
            male_tokens = ["男性", "だんせい", "男", "male", "man"]
            female_tokens = ["女性", "じょせい", "女", "female", "woman"]
            other_tokens = [
                "その他",
                "未回答",
                "無回答",
                "回答しない",
                "other",
                "prefer not",
            ]
            if any(t in v for t in male_tokens):
                return "male"
            if any(t in v for t in female_tokens):
                return "female"
            if any(t in v for t in other_tokens):
                return "other"
            return None

        client_gender_norm = _normalize_gender(str(client_info.get("gender", "") or ""))

        for i, select in enumerate(selects):
            if id(select) in mapped_element_ids:
                continue
            try:
                element_info = await self.element_scorer._get_element_info(select)
                if not element_info.get("visible", True):
                    continue

                # 必須判定（フォールバック付き）
                required = await self.element_scorer._detect_required_status(select)
                if not required:
                    # DL構造: <dd> の直前 <dt> に必須マーカーがあるか簡易チェック（JS側も try-catch）
                    try:
                        required = await select.evaluate(
                            """
                            (el, MARKERS) => {
                              try {
                                if (!el || !el.tagName) return false;
                                let p = el;
                                while (p && (p.tagName||'').toLowerCase() !== 'dd') p = p.parentElement;
                                if (!p) return false;
                                let dt = p.previousElementSibling;
                                while (dt && (dt.tagName||'').toLowerCase() !== 'dt') dt = dt.previousElementSibling;
                                if (!dt) return false;
                                const t = (dt.innerText || dt.textContent || '').trim();
                                if (!t) return false;
                                return MARKERS.some(m => t.includes(m));
                              } catch { return false; }
                            }
                            """,
                            list(self.REQUIRED_MARKERS),
                        )
                    except Exception as e:
                        logger.debug(f"DT/DD required detection failed for select: {e}")
                        required = False
                if not required:
                    continue
                opt_data = await select.evaluate(
                    "el => Array.from(el.options).map(o => ({text: (o.textContent || '').trim(), value: o.value || ''}))"
                )
                if len(opt_data) < 2:
                    continue

                texts = [d.get("text", "") for d in opt_data]
                values = [d.get("value", "") for d in opt_data]
                is_pref_select = any("東京都" in tx for tx in texts) and any(
                    "大阪府" in tx for tx in texts
                )
                is_gender_select = any(
                    any(k in (tx or "") for k in ["男", "男性", "male"]) for tx in texts
                ) and any(
                    any(k in (tx or "") for k in ["女", "女性", "female"])
                    for tx in texts
                )
                idx = None
                if is_gender_select and client_gender_norm:
                    # クライアント性別に一致する選択肢を優先
                    targets = {
                        "male": ["男", "男性", "male"],
                        "female": ["女", "女性", "female"],
                        "other": ["その他", "other"],
                    }.get(client_gender_norm, [])
                    for cand_text in texts:
                        pass
                    cand = [
                        k
                        for k, tx in enumerate(texts)
                        if any(
                            t in (tx or "").lower()
                            for t in [s.lower() for s in targets]
                        )
                    ]
                    if cand:
                        idx = cand[0]
                elif is_pref_select:
                    if prefecture_target:
                        cand = [
                            k for k, tx in enumerate(texts) if prefecture_target in tx
                        ]
                        if cand:
                            idx = cand[-1]
                    if idx is None:
                        for fallback in ["東京都", "大阪府"]:
                            cand = [k for k, tx in enumerate(texts) if fallback in tx]
                            if cand:
                                idx = cand[-1]
                                break
                if idx is None:
                    idx = self._choose_priority_index(texts, pri1, pri2)

                selector = await self._generate_playwright_selector(select)
                field_name = f"auto_select_{i+1}"
                handled[field_name] = {
                    "element": select,
                    "selector": selector,
                    "tag_name": "select",
                    "type": "",
                    "name": element_info.get("name", ""),
                    "id": element_info.get("id", ""),
                    "input_type": "select",
                    "auto_action": "select_index",
                    "selected_index": idx,
                    "selected_option_text": texts[idx],
                    "default_value": values[idx] or texts[idx],
                    "required": True,
                    "auto_handled": True,
                    "options_count": len(opt_data),
                }
            except Exception as e:
                logger.debug(f"Error auto-handling select {i}: {e}")
        return handled

    def _choose_priority_index(
        self, texts: List[str], pri1: List[str], pri2: List[str]
    ) -> int:
        def last_match(keys: List[str]) -> Optional[int]:
            idxs = [i for i, t in enumerate(texts) if any(k in (t or "") for k in keys)]
            return idxs[-1] if idxs else None

        idx = last_match(pri1)
        if idx is not None:
            return idx
        idx = last_match(pri2)
        if idx is not None:
            return idx
        return max(0, len(texts) - 1)

    def _choose_gender_index(self, texts: List[str]) -> int:
        male_keywords = ["男", "男性", "male", "man", "Men", "Male"]
        for i, text in enumerate(texts):
            if text and any(keyword in text for keyword in male_keywords):
                return i
        return 0

    async def _auto_handle_email_confirmation(
        self, candidates: List[Locator], mapped_element_ids: set
    ) -> Dict[str, Dict[str, Any]]:
        handled = {}
        confirmation_patterns = [
            "email_confirm",
            "mail_confirm",
            "email_confirmation",
            "confirm_email",
            "confirm_mail",
            "メール確認",
            "確認用メール",
            "email_check",
            "mail_check",
            "re_email",
            "re_mail",
        ]
        for i, el in enumerate(candidates):
            if id(el) in mapped_element_ids:
                continue
            info = await self.element_scorer._get_element_info(el)
            if not info.get("visible", True):
                continue
            name_id_class = " ".join(
                [info.get("name", ""), info.get("id", ""), info.get("class", "")]
            ).lower()
            if any(p in name_id_class for p in confirmation_patterns):
                selector = await self._generate_playwright_selector(el)
                required = await self.element_scorer._detect_required_status(el)
                field_name = f"auto_email_confirm_{i+1}"
                handled[field_name] = {
                    "element": el,
                    "selector": selector,
                    "tag_name": info.get("tag_name", "input"),
                    "type": info.get("type", "email") or "email",
                    "name": info.get("name", ""),
                    "id": info.get("id", ""),
                    "input_type": "email",
                    "auto_action": "copy_from",
                    "copy_from_field": "メールアドレス",
                    "default_value": "",
                    "required": required,
                    "auto_handled": True,
                }
        return handled

    async def _auto_handle_unified_fullname(
        self,
        text_inputs: List[Locator],
        mapped_element_ids: set,
        client_data: Optional[Dict[str, Any]],
        form_structure: Optional[FormStructure],
    ) -> Dict[str, Dict[str, Any]]:
        handled = {}
        unified_patterns = set(self.field_patterns.get_unified_name_patterns())
        if form_structure and form_structure.elements:
            for i, fe in enumerate(form_structure.elements):
                if id(fe.locator) in mapped_element_ids:
                    continue
                label_text = (fe.label_text or "").lower()
                placeholder_text = (fe.placeholder or "").lower()
                if any(p in label_text for p in unified_patterns) or any(
                    p in placeholder_text for p in unified_patterns
                ):
                    info = await self.element_scorer._get_element_info(fe.locator)
                    if not info.get("visible", True):
                        continue
                    selector = await self._generate_playwright_selector(fe.locator)
                    required = await self.element_scorer._detect_required_status(
                        fe.locator
                    )
                    fullname = self.field_combination_manager.generate_combined_value(
                        "full_name", client_data or {}
                    )
                    if not fullname:
                        continue
                    field_name = f"auto_fullname_label_{i+1}"
                    handled[field_name] = {
                        "element": fe.locator,
                        "selector": selector,
                        "tag_name": info.get("tag_name", "input"),
                        "type": info.get("type", "text") or "text",
                        "name": info.get("name", ""),
                        "id": info.get("id", ""),
                        "input_type": "text",
                        "auto_action": "fill",
                        "default_value": fullname,
                        "required": required,
                        "auto_handled": True,
                    }
        return handled

    async def _auto_handle_unified_kana(
        self,
        text_inputs: List[Locator],
        mapped_element_ids: set,
        client_data: Optional[Dict[str, Any]],
        form_structure: Optional[FormStructure],
    ) -> Dict[str, Dict[str, Any]]:
        handled: Dict[str, Dict[str, Any]] = {}
        try:
            for i, el in enumerate(text_inputs):
                if id(el) in mapped_element_ids:
                    continue
                info = await self.element_scorer._get_element_info(el)
                if not info.get("visible", True):
                    continue
                name_id_cls = " ".join(
                    [info.get("name", ""), info.get("id", ""), info.get("class", "")]
                ).lower()
                # 候補判定: name/id/classに kana/furigana/カナ 等、またはラベルに「フリガナ」
                contexts = (
                    await self.context_text_extractor.extract_context_for_element(el)
                )
                best = (
                    self.context_text_extractor.get_best_context_text(contexts) or ""
                ).lower()
                is_kana_like = (
                    any(k in name_id_cls for k in ["kana", "furigana", "katakana"])
                    or ("フリガナ" in best)
                    or ("ふりがな" in best)
                )
                if not is_kana_like:
                    continue
                # CAPTCHAや認証は除外
                if any(
                    b in name_id_cls for b in ["captcha", "image_auth", "spam-block"]
                ):
                    continue

                # kana/hiragana の種別推定（簡易）
                kana_type = (
                    "hiragana"
                    if any(
                        h in (best + " " + name_id_cls)
                        for h in ["ひらがな", "hiragana"]
                    )
                    else "katakana"
                )
                value = self.field_combination_manager.generate_unified_kana_value(
                    kana_type, client_data or {}
                )
                if not value:
                    continue

                selector = await self._generate_playwright_selector(el)
                required = await self.element_scorer._detect_required_status(el)
                field_name = f"auto_unified_kana_{i+1}"
                handled[field_name] = {
                    "element": el,
                    "selector": selector,
                    "tag_name": info.get("tag_name", "input"),
                    "type": info.get("type", "text") or "text",
                    "name": info.get("name", ""),
                    "id": info.get("id", ""),
                    "input_type": "text",
                    "auto_action": "fill",
                    "default_value": value,
                    "required": required,
                    "auto_handled": True,
                }
        except Exception as e:
            logger.debug(f"Auto handle unified kana failed: {e}")
        return handled

    async def promote_required_fullname_to_mapping(
        self,
        auto_handled: Dict[str, Dict[str, Any]],
        field_mapping: Dict[str, Dict[str, Any]],
    ) -> List[str]:
        promoted_keys: List[str] = []
        if "統合氏名" in field_mapping:
            return promoted_keys
        candidates = [
            (k, v)
            for k, v in (auto_handled or {}).items()
            if k.startswith("auto_fullname") and v.get("required")
        ]
        if not candidates:
            return promoted_keys
        key, info = candidates[0]
        el = info.get("element")
        if not el:
            return promoted_keys
        try:
            element_info = await self._get_element_details(el)
            element_info["score"] = 100
            field_mapping["統合氏名"] = element_info
            promoted_keys.append(key)
        except Exception as e:
            logger.debug(f"Promote required fullname failed: {e}")
        return promoted_keys
