"""
ルールベースフォーム解析エンジン（オーケストレーター）
"""

import time
import logging
from typing import Dict, List, Any, Optional

from playwright.async_api import Page, Locator

from .field_patterns import FieldPatterns
from .element_scorer import ElementScorer
from .duplicate_prevention import DuplicatePreventionManager
from .field_combination_manager import FieldCombinationManager
from .form_structure_analyzer import FormStructureAnalyzer, FormStructure
from .context_text_extractor import ContextTextExtractor
from .split_field_detector import SplitFieldDetector
from .sales_prohibition_detector import SalesProhibitionDetector
from .form_pre_processor import FormPreProcessor
from .element_classifier import ElementClassifier
from .field_mapper import FieldMapper
from .unmapped_element_handler import UnmappedElementHandler
from .input_value_assigner import InputValueAssigner
from .submit_button_detector import SubmitButtonDetector
from .analysis_validator import AnalysisValidator
from .analysis_result_builder import AnalysisResultBuilder

logger = logging.getLogger(__name__)


class RuleBasedAnalyzer:
    """ルールベースフォーム解析の全体を統括するメインクラス"""

    def __init__(self, page_or_frame: Page):
        self.page = page_or_frame
        self.settings = self._load_settings()

        # Helper classes
        self.field_patterns = FieldPatterns()
        self.context_text_extractor = ContextTextExtractor(page_or_frame)
        # 共有属性キャッシュ（Locator文字列 -> 属性辞書）。後段で構造解析結果から埋める。
        self._element_attr_cache: Dict[str, Dict[str, Any]] = {}
        self.element_scorer = ElementScorer(
            self.context_text_extractor, shared_cache=self._element_attr_cache
        )
        self.duplicate_prevention = DuplicatePreventionManager()
        self.field_combination_manager = FieldCombinationManager()
        self.form_structure_analyzer = FormStructureAnalyzer(page_or_frame)
        self.split_field_detector = SplitFieldDetector()
        self.sales_prohibition_detector = SalesProhibitionDetector(page_or_frame)

        # Worker classes for each phase
        self.pre_processor = FormPreProcessor(
            page_or_frame,
            self.element_scorer,
            self.split_field_detector,
            self.field_patterns,
        )
        self.classifier = ElementClassifier(page_or_frame, self.settings)
        self.mapper = FieldMapper(
            page_or_frame,
            self.element_scorer,
            self.context_text_extractor,
            self.field_patterns,
            self.duplicate_prevention,
            self.settings,
            self._create_enhanced_element_info,
            self._generate_temp_field_value,
            self.field_combination_manager,
        )
        self.unmapped_handler = UnmappedElementHandler(
            page_or_frame,
            self.element_scorer,
            self.context_text_extractor,
            self.field_combination_manager,
            self.settings,
            self._generate_playwright_selector,
            self._get_element_details,
            self.field_patterns,
        )
        self.assigner = InputValueAssigner(
            self.field_combination_manager, self.split_field_detector
        )
        self.submit_detector = SubmitButtonDetector(
            page_or_frame, self._generate_playwright_selector
        )
        self.validator = AnalysisValidator(self.duplicate_prevention)
        self.result_builder = AnalysisResultBuilder(
            self.field_patterns, self.element_scorer, self.settings
        )

        # Analysis results
        self.field_mapping: Dict[str, Any] = {}
        self.form_structure: Optional[FormStructure] = None
        self.unmapped_elements: List[Any] = []

        logger.info("RuleBasedAnalyzer initialized")

    def _load_settings(self) -> Dict[str, Any]:
        return {
            "max_elements_per_type": 50,
            "min_score_threshold": 70,
            # フィールド別の最低スコアしきい値（汎用の誤検出抑止）
            "min_score_threshold_per_field": {
                # 一部サイトでは class に first-name/last-name が付与され、
                # ラベルが『ご担当者名』のみのケースが多いため、
                # クラス+タグ（=80点）で妥当に採用できるよう安全側に調整
                "姓": 80,
                "名": 80,
                # 汎用で安全な下限値の追加（誤検出抑止の微調整）
                "会社名": 78,
                "メールアドレス": 75,
                "都道府県": 75,
            },
            "analysis_timeout": 30,
            "enable_fallback": True,
            "enable_auto_handling": True,
            "debug_scoring": True,
            "quality_first_mode": True,
            # コア項目（必須が検出できないサイトでも優先的に確保する）
            # 既存の基本2項目に加え、氏名・カナ系をコアに昇格（汎用精度向上）
            "essential_fields": [
                "メールアドレス",
                "お問い合わせ本文",
                "統合氏名",
                "統合氏名カナ",
            ],
            "quality_threshold_boost": 15,
            "max_quality_threshold": 90,
            "quick_ranking_enabled": True,
            "quick_top_k": 15,
            "quick_top_k_essential": 25,
            "early_stop_enabled": True,
            "early_stop_score": 95,
            # 必須判定時のボーナス（安全側）
            "required_boost": 40,
            "required_phone_boost": 200,
            # 追加: 設定化されたしきい値/トークン
            "email_fallback_min_score": 60,
            "confirm_tokens": [
                "confirm",
                "confirmation",
                "確認",
                "確認用",
                "再入力",
                "もう一度",
                "再度",
            ],
            # ラジオ必須検出の探索深さ（JS側で利用）
            "radio_required_max_container_depth": 6,
            "radio_required_max_sibling_depth": 2,
        }

    async def analyze_form(
        self, client_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        analysis_start = time.time()
        logger.info("Starting comprehensive form analysis...")

        try:
            # --- Pre-processing ---
            if await self.pre_processor.check_if_scroll_needed():
                await self.pre_processor.perform_progressive_scroll()

            self.form_structure = (
                await self.form_structure_analyzer.analyze_form_structure()
            )
            logger.info(
                f"Form structure analyzed: {self.form_structure_analyzer.get_structure_summary(self.form_structure)}"
            )

            # 高速化: bounding_box辞書を作成（再利用のため）
            self._element_bounds_cache = {}
            if self.form_structure and hasattr(self.form_structure, "elements"):
                for element_info in self.form_structure.elements:
                    if element_info.locator and element_info.bounding_box:
                        element_key = str(element_info.locator)
                        self._element_bounds_cache[element_key] = (
                            element_info.bounding_box
                        )
                logger.debug(
                    f"Cached bounding boxes for {len(self._element_bounds_cache)} elements"
                )

            # 高速化: 属性キャッシュ（quick採点用）を構築
            if self.form_structure and hasattr(self.form_structure, "elements"):
                for fe in self.form_structure.elements:
                    try:
                        key = str(fe.locator)
                        self._element_attr_cache[key] = {
                            "tagName": fe.tag_name or "",
                            "type": fe.element_type or "",
                            "name": fe.name or "",
                            "id": fe.id or "",
                            "className": fe.class_name or "",
                            "placeholder": fe.placeholder or "",
                            "value": "",
                            "visibleLite": bool(fe.is_visible),
                            "enabledLite": bool(fe.is_enabled),
                        }
                    except Exception:
                        continue

            await self._prepare_context_extraction()
            structured_elements = self.form_structure.elements

            # --- Analysis Phases ---
            classified_elements = await self.classifier.classify_structured_elements(
                structured_elements
            )
            logger.info(
                f"Classified elements: {self.classifier.get_classification_summary(classified_elements)}"
            )

            unified_field_info = self.pre_processor.detect_unified_fields(
                structured_elements
            )
            form_type_info = await self.pre_processor.detect_form_type(
                structured_elements, self.form_structure
            )
            required_analysis = await self.pre_processor.analyze_required_fields(
                structured_elements
            )

            # --- Field Mapping ---
            self.field_mapping = await self.mapper.execute_enhanced_field_mapping(
                classified_elements,
                unified_field_info,
                form_type_info,
                self._element_bounds_cache,
                required_analysis,
            )
            logger.info(
                f"Mapped {len(self.field_mapping)} fields with context enhancement"
            )

            # ポストプロセス: 分割姓名が揃っている場合は統合氏名を抑制（重複入力防止・精度向上）
            try:
                if (
                    "姓" in self.field_mapping
                    and "名" in self.field_mapping
                    and "統合氏名" in self.field_mapping
                ):
                    self.field_mapping.pop("統合氏名", None)
                if (
                    "姓カナ" in self.field_mapping
                    and "名カナ" in self.field_mapping
                    and "統合氏名カナ" in self.field_mapping
                ):
                    self.field_mapping.pop("統合氏名カナ", None)
            except Exception:
                pass

            # 汎用ポストプロセス: 個人名の誤検出抑止
            # 例: 『住所またはマンション名』『ふりがな』等の文脈で誤って『名』『姓』に割り当てられた場合、
            #     統合氏名が存在するなら分割フィールドは削除して安全側に倒す。
            try:
                self._prune_suspect_name_mappings()
            except Exception as e:
                logger.debug(f"name mapping prune skipped: {e}")

            # 汎用ポストプロセス: カナ/ひらがなの整合性を正規化
            try:
                await self._normalize_kana_hiragana_fields()
            except Exception as e:
                logger.debug(f"kana/hiragana normalization skipped: {e}")

            # 汎用改善: zip系2連続の自動昇格（郵便番号1/2）
            try:
                await self._auto_promote_postal_split()
            except Exception as e:
                logger.debug(f"auto_promote_postal_split failed: {e}")

            # --- Handle Unmapped and Special Fields ---
            auto_handled = await self.unmapped_handler.handle_unmapped_elements(
                classified_elements,
                self.field_mapping,
                client_data,
                self.form_structure,
            )

            promoted = await self.unmapped_handler.promote_required_fullname_to_mapping(
                auto_handled, self.field_mapping
            )
            if promoted:
                for k in promoted:
                    auto_handled.pop(k, None)
            # 追加: 必須カナの昇格（auto_unified_kana_* → 統合氏名カナ）
            promoted_kana = (
                await self.unmapped_handler.promote_required_kana_to_mapping(
                    auto_handled, self.field_mapping
                )
            )
            if promoted_kana:
                for k in promoted_kana:
                    auto_handled.pop(k, None)

            # 分割フィールドの検出は auto_handled も含めた集合で再計算（メール確認や分割入力に対応）
            try:
                combined_for_split = {**self.field_mapping, **auto_handled}
            except Exception:
                combined_for_split = self.field_mapping
            split_groups = self._detect_split_field_patterns(combined_for_split)

            # --- Value Assignment & Validation ---
            self.assigner.required_analysis = required_analysis
            self.assigner.unified_field_info = unified_field_info
            input_assignment = await self.assigner.assign_enhanced_input_values(
                self.field_mapping, auto_handled, split_groups, client_data
            )

            is_valid, validation_issues = (
                await self.validator.validate_final_assignments(
                    input_assignment, self.field_mapping, form_type_info
                )
            )
            if not is_valid:
                logger.warning(f"Validation issues detected: {validation_issues}")

            # --- Final Steps ---
            # 送信ボタンはフォーム境界内に限定して検出（ヘッダー検索ボタン等の混入防止）
            submit_buttons = await self.submit_detector.detect_submit_buttons(
                self.form_structure.form_locator if self.form_structure else None
            )
            prohibition_result = (
                await self.sales_prohibition_detector.detect_prohibition_text()
            )

            analysis_time = time.time() - analysis_start

            # --- Build Result ---
            analysis_summary = self.result_builder.create_analysis_summary(
                self.field_mapping, auto_handled, self.classifier.special_elements, form_type_info.get('primary_type')
            )
            debug_info = self.result_builder.create_debug_info(self.unmapped_elements)

            return {
                "success": True,
                "analysis_time": analysis_time,
                "total_elements": len(structured_elements),
                "field_mapping": self.field_mapping,
                "auto_handled_elements": auto_handled,
                "input_assignments": input_assignment,
                "submit_buttons": submit_buttons,
                "special_elements": self.classifier.special_elements,
                "unmapped_elements": len(self.unmapped_elements),
                "analysis_summary": analysis_summary,
                "duplicate_prevention": self.duplicate_prevention.get_assignment_summary(),
                "split_field_patterns": self.split_field_detector.get_detector_summary(
                    split_groups
                ),
                "field_combination_summary": self.field_combination_manager.get_summary(),
                "validation_result": {
                    "is_valid": is_valid,
                    "issues": validation_issues,
                },
                "sales_prohibition": prohibition_result,
                "debug_info": debug_info if self.settings.get("debug_scoring") else {},
            }

        except Exception as e:
            logger.error(f"Form analysis failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _prune_suspect_name_mappings(self) -> None:
        try:
            if "統合氏名" not in self.field_mapping:
                return
            # 文脈上、個人名ではない可能性が高い語を追加（汎用）
            negative_ctx_tokens = [
                "住所",
                "マンション名",
                "建物名",
                "ふりがな",
                "フリガナ",
                "カナ",
                "かな",
                "ひらがな",
                "郵便",
                "郵便番号",
                "商品名",
                "部署",
                "部署名",
            ]
            negative_attr_tokens = ["kana", "furigana", "katakana", "hiragana"]
            for k in ["姓", "名"]:
                info = self.field_mapping.get(k)
                if not info:
                    continue
                ctx = (info.get("best_context_text") or "").lower()
                blob = " ".join(
                    [
                        str(info.get("name", "")).lower(),
                        str(info.get("id", "")).lower(),
                        str(info.get("class", "")).lower(),
                        str(info.get("placeholder", "")).lower(),
                    ]
                )
                if any(t.lower() in ctx for t in negative_ctx_tokens) or any(
                    t in blob for t in negative_attr_tokens
                ):
                    self.field_mapping.pop(k, None)
            # さらに安全弁: 『統合氏名』が確定しているのに、姓/名のスコアが低い場合は除去
            try:
                per_field = (
                    self.mapper.settings.get("min_score_threshold_per_field", {}) or {}
                )
                min_name_score = int(per_field.get("名", 85))
                min_last_score = int(per_field.get("姓", 85))
            except Exception:
                min_name_score = 85
                min_last_score = 85
            for k, th in [("姓", min_last_score), ("名", min_name_score)]:
                info = self.field_mapping.get(k)
                if info and int(info.get("score", 0)) < th:
                    self.field_mapping.pop(k, None)
        except Exception:
            pass

    async def _normalize_kana_hiragana_fields(self) -> None:
        """『姓/名カナ』と『姓/名ひらがな』のマッピングを要素属性に基づいて正規化する。

        目的:
        - ふりがな/ひらがな欄が『姓カナ/名カナ』に誤って割り当てられるのを汎用的に修正
        - 逆にカタカナ欄が『ひらがな』に割り当てられた場合も修正
        - 分割ひらがなが揃っている場合は『統合氏名カナ』を抑制
        """

        def _is_hiragana_like(info: dict) -> bool:
            blob = " ".join(
                [
                    str(info.get("name", "")),
                    str(info.get("id", "")),
                    str(info.get("class", "")),
                    str(info.get("placeholder", "")),
                ]
            )
            return any(k in blob for k in ["ひらがな", "ふりがな"]) and not any(
                k in blob for k in ["カナ", "カタカナ", "フリガナ"]
            )

        def _is_katakana_like(info: dict) -> bool:
            blob = " ".join(
                [
                    str(info.get("name", "")),
                    str(info.get("id", "")),
                    str(info.get("class", "")),
                    str(info.get("placeholder", "")),
                ]
            )
            return any(k in blob for k in ["カナ", "カタカナ", "フリガナ"])

        for kana_field, hira_field in [
            ("姓カナ", "姓ひらがな"),
            ("名カナ", "名ひらがな"),
        ]:
            kinfo = self.field_mapping.get(kana_field)
            hinfo = self.field_mapping.get(hira_field)
            # 『姓カナ/名カナ』があるが、実体がひらがな欄 → リネーム
            if kinfo and _is_hiragana_like(kinfo) and not hinfo:
                self.field_mapping[hira_field] = kinfo
                self.field_mapping.pop(kana_field, None)
            # 『姓ひらがな/名ひらがな』があるが、実体がカタカナ欄 → リネーム
            if hinfo and _is_katakana_like(hinfo) and not kinfo:
                self.field_mapping[kana_field] = hinfo
                self.field_mapping.pop(hira_field, None)

        # 統合カナの降格: 分割ひらがなが揃っていれば統合カナは不要
        if ("姓ひらがな" in self.field_mapping) and (
            "名ひらがな" in self.field_mapping
        ):
            if "統合氏名カナ" in self.field_mapping:
                self.field_mapping.pop("統合氏名カナ", None)

        # 統合カナがひらがな欄に割り当てられている場合でも、
        # 単一の『ふりがな/ひらがな』入力しか存在しないフォームでは統合のまま維持する。
        # （任意の一方へ強制的に割り当てると、今回のように『名ひらがな』扱いになり不整合が起きるため）
        # 2つの分割（姓/名）ひらがなが検出できた場合のみ、既存の分割正規化ロジックに委ねる。
        uinfo = self.field_mapping.get("統合氏名カナ")
        if uinfo and _is_hiragana_like(uinfo):
            try:
                # すでに分割が揃っている場合は統合を降格（上の統合カナの降格ブロックが担当）
                has_split_hira = ("姓ひらがな" in self.field_mapping) and (
                    "名ひらがな" in self.field_mapping
                )
                if has_split_hira:
                    pass  # 何もしない（既存ロジックに委譲）
                else:
                    # 分割が揃っていない場合は統合のまま維持（降格しない）
                    # → 以前は名ひらがなへ補正していたが、単一欄のケースで不適切だったため抑止
                    pass
            except Exception:
                pass

        # 欠落補完: ひらがな分割欄が存在するのにマッピング漏れしている場合、DOMから直接補完
        try:
            if self.form_structure and getattr(self.form_structure, "elements", None):
                used_selectors = {
                    (v or {}).get("selector", "")
                    for v in self.field_mapping.values()
                    if isinstance(v, dict)
                }
                for need, token in [("姓ひらがな", "姓"), ("名ひらがな", "名")]:
                    if need in self.field_mapping:
                        continue
                    for fe in self.form_structure.elements:
                        try:
                            if (fe.tag_name or "").lower() != "input":
                                continue
                            if (fe.element_type or "").lower() not in ("text", ""):
                                continue
                            if not fe.is_visible:
                                continue
                            blob = " ".join(
                                [
                                    (fe.name or ""),
                                    (fe.id or ""),
                                    (fe.class_name or ""),
                                    (fe.placeholder or ""),
                                    (fe.label_text or ""),
                                    (fe.associated_text or ""),
                                ]
                            )
                            if ("ふりがな" in blob or "ひらがな" in blob) and (
                                token in blob
                            ):
                                info = await self._get_element_details(fe.locator)
                                if info.get("selector", "") not in used_selectors:
                                    self.field_mapping[need] = info
                                    used_selectors.add(info.get("selector", ""))
                                    break
                        except Exception:
                            continue
        except Exception:
            pass

    async def _prepare_context_extraction(self):
        if getattr(self.form_structure, "form_bounds", None):
            self.context_text_extractor.set_form_bounds(self.form_structure.form_bounds)
            await self.context_text_extractor.build_form_context_index()

    def _detect_split_field_patterns(self, field_mapping: Dict[str, Any]):
        field_mappings_list = [
            {"field_name": fn, **fi} for fn, fi in field_mapping.items()
        ]
        input_order = []
        if self.form_structure and self.form_structure.elements:
            for fe in self.form_structure.elements:
                if fe.tag_name in [
                    "input",
                    "textarea",
                    "select",
                ] and fe.element_type not in ["hidden", "submit", "image", "button"]:
                    if fe.selector:
                        input_order.append(fe.selector)
        return self.split_field_detector.detect_split_patterns(
            field_mappings_list, input_order
        )

    async def _auto_promote_postal_split(self) -> None:
        """zip/postal 系フィールドが論理順で連続して2つ並ぶ場合に、
        統合『郵便番号』よりも『郵便番号1/2』の分割マッピングを優先して登録する。

        連続の定義: 入力欄のみを取り出して作成した input_order 上で index が連番。
        """
        if not (self.form_structure and self.form_structure.elements):
            return

        # 1) 入力欄の論理順（input_order）と selector->index のマップを構築
        input_order: list[str] = []
        for fe in self.form_structure.elements:
            if fe.tag_name in [
                "input",
                "textarea",
                "select",
            ] and fe.element_type not in ["hidden", "submit", "image", "button"]:
                if fe.selector:
                    input_order.append(fe.selector)
        order_index = {sel: i for i, sel in enumerate(input_order)}

        # 2) zip/postal 系候補を抽出（name/id/class/placeholder/ラベルテキスト/周辺テキスト）
        # 汎用トークン（過検出を避けるため、曖昧すぎる語は含めない。例: 'post' 単独など）
        postal_tokens = [
            "zip",
            "zipcode",
            "zip_code",
            "zip-code",
            "zip1",
            "zip2",
            "zip_first",
            "zip_last",
            "postal",
            "postalcode",
            "postal_code",
            "post_code",
            "post-code",
            "postcode",
            "postcode1",
            "postcode2",
            "郵便",
            "郵便番号",
            "〒",
            "上3桁",
            "下4桁",
            "前3桁",
            "後4桁",
            # ローマ字表記の揺れ
            "yubin",
            "yuubin",
            "yubinbango",
            "yuubinbango",
        ]
        candidates = []  # (index, FormElement)
        for fe in self.form_structure.elements:
            try:
                if fe.tag_name != "input":
                    continue
                if fe.element_type not in ["", "text", "tel"]:
                    continue
                sel = fe.selector or ""
                if sel not in order_index:
                    continue
                text_blob = " ".join(
                    [
                        (fe.name or ""),
                        (fe.id or ""),
                        (fe.class_name or ""),
                        (fe.placeholder or ""),
                        (fe.label_text or ""),
                        (fe.associated_text or ""),
                        " ".join(fe.nearby_text or []),
                    ]
                ).lower()
                if any(tok in text_blob for tok in postal_tokens):
                    candidates.append((order_index[sel], fe))
            except Exception:
                continue

        if len(candidates) < 2:
            return

        # 3) 論理順でソートし、連番ペアを探索
        candidates.sort(key=lambda t: t[0])
        pair = None
        for i in range(len(candidates) - 1):
            idx1, fe1 = candidates[i]
            idx2, fe2 = candidates[i + 1]
            # 厳密な連続(=1)に限定せず、至近(<=2)も許容（実務でラベル/説明が間に挟まるケース対策）
            if idx2 - idx1 <= 2:  # 連続/準連続
                pair = (fe1, fe2)
                break

        if not pair:
            return

        # 4) 既に郵便番号1/2が確定していれば何もしない
        if "郵便番号1" in self.field_mapping and "郵便番号2" in self.field_mapping:
            return

        fe1, fe2 = pair

        # 5) 既存の統合『郵便番号』が fe1/fe2 を指している場合は除去し、分割へ置換
        try:
            unified = self.field_mapping.get("郵便番号")
            if unified:
                u_sel = unified.get("selector", "")
                if u_sel in {fe1.selector, fe2.selector}:
                    self.field_mapping.pop("郵便番号", None)
        except Exception:
            pass

        # 6) 分割『郵便番号1/2』として登録（ただし必須のときのみ）
        try:
            # 必須でない郵便番号を無闇にマッピングすると誤入力の温床になるため抑制
            req1 = False
            req2 = False
            try:
                req1 = await self.element_scorer._detect_required_status(fe1.locator)
                req2 = await self.element_scorer._detect_required_status(fe2.locator)
            except Exception:
                req1 = False
                req2 = False

            if not (req1 or req2):
                # どちらも必須でない場合はスキップ（auto-handledにも載せない）
                return

            # fe.locator から要素詳細を取得
            info1 = await self._get_element_details(fe1.locator)
            info2 = await self._get_element_details(fe2.locator)

            # 重複防止レジストリ更新（スコア0、temp値でOK）
            self.duplicate_prevention.register_field_assignment(
                "郵便番号1", self._generate_temp_field_value("郵便番号1"), 0, info1
            )
            self.duplicate_prevention.register_field_assignment(
                "郵便番号2", self._generate_temp_field_value("郵便番号2"), 0, info2
            )

            self.field_mapping["郵便番号1"] = info1
            self.field_mapping["郵便番号2"] = info2
            logger.info(
                "Promoted zip consecutive inputs to split postal mapping (郵便番号1/2) [required]"
            )
        except Exception as e:
            logger.debug(f"Failed to promote postal split: {e}")

    # --- Helper methods passed to other classes ---

    async def _get_element_details(self, element: Locator) -> Dict[str, Any]:
        element_info = await self.element_scorer._get_element_info(element)
        selector = await self._generate_playwright_selector(element)
        return {
            "element": element,
            "selector": selector,
            "tag_name": element_info.get("tag_name", ""),
            "type": element_info.get("type", ""),
            "name": element_info.get("name", ""),
            "id": element_info.get("id", ""),
            "class": element_info.get("class", ""),
            "placeholder": element_info.get("placeholder", ""),
            "required": element_info.get("required", False),
            "visible": element_info.get("visible", True),
            "enabled": element_info.get("enabled", True),
            "score": 0,
            "score_details": {},
            "input_type": self._determine_input_type(element_info),
            "default_value": "",
        }

    async def _create_enhanced_element_info(
        self, element: Locator, score_details: Dict[str, Any], contexts
    ) -> Dict[str, Any]:
        element_info = await self._create_element_info(element, score_details)
        element_info["context"] = [
            {
                "text": ctx.text,
                "source_type": ctx.source_type,
                "confidence": ctx.confidence,
                "position": ctx.position_relative,
            }
            for ctx in contexts
        ]
        if contexts:
            element_info["best_context_text"] = (
                self.context_text_extractor.get_best_context_text(contexts)
            )
        return element_info

    async def _create_element_info(
        self, element: Locator, score_details: Dict[str, Any]
    ) -> Dict[str, Any]:
        element_info = score_details.get("element_info", {})
        selector = await self._generate_playwright_selector(element)
        return {
            "element": element,
            "selector": selector,
            "tag_name": element_info.get("tag_name", ""),
            "type": element_info.get("type", ""),
            "name": element_info.get("name", ""),
            "id": element_info.get("id", ""),
            "class": element_info.get("class", ""),
            "placeholder": element_info.get("placeholder", ""),
            "required": element_info.get("required", False),
            "visible": element_info.get("visible", True),
            "enabled": element_info.get("enabled", True),
            "score": score_details.get("total_score", 0),
            "score_details": score_details,
            "input_type": self._determine_input_type(element_info),
            "default_value": "",
        }

    async def _generate_playwright_selector(self, element: Locator) -> str:
        """Playwright用の安定したCSSセレクタを生成

        重要: CSSの `[type="text"]` は属性が存在する場合のみ一致する。
        `el.type` は属性がなくても "text" を返すため、誤セレクタとなる。
        そのため、type属性は「属性が存在する場合にのみ」付与する。

        優先順位:
        - idがあれば `[id="..."]`（CSSの #id は先頭数字/特殊文字で無効になりうるため）
        - nameがあれば `tag[name="..."]` (+ `[type="..."]` は属性存在時のみ)
        - nameが無ければ、inputの場合でも `[type]` は使わずタグのみ（属性がある場合のみ `[type]`）
        """
        try:
            info = await element.evaluate(
                """
                el => ({
                  id: el.getAttribute('id') || '',
                  name: el.getAttribute('name') || '',
                  tagName: (el.tagName || '').toLowerCase(),
                  // 属性としてのtype（存在しない場合は空文字）
                  typeAttr: el.getAttribute('type') || ''
                })
                """
            )
            # ID優先（CSSのattribute selectorを使用してエスケープ不要にする）
            el_id = info.get("id")
            if el_id:
                # 引用符とバックスラッシュを最小限エスケープ
                esc = str(el_id).replace("\\", r"\\").replace('"', r"\"")
                return f'[id="{esc}"]'

            name = info.get("name")
            tag = info.get("tagName", "input")
            type_attr = info.get("typeAttr") if tag == "input" else ""

            if name:
                # name属性は attribute selector で安全に指定
                esc_name = str(name).replace("\\", r"\\").replace('"', r"\"")
                selector = f'{tag}[name="{esc_name}"]'
                if type_attr:
                    esc_type = str(type_attr).replace("\\", r"\\").replace('"', r"\"")
                    selector += f'[type="{esc_type}"]'
                return selector

            # nameが無い場合：type属性があるinputのみ[type]を付与
            if tag == "input" and type_attr:
                esc_type2 = str(type_attr).replace("\\", r"\\").replace('"', r"\"")
                return f'{tag}[type="{esc_type2}"]'
            return tag
        except Exception:
            return "input"

    def _determine_input_type(self, element_info: Dict[str, Any]) -> str:
        tag_name = element_info.get("tag_name", "").lower()
        element_type = element_info.get("type", "").lower()
        if tag_name == "textarea":
            return "textarea"
        if tag_name == "select":
            return "select"
        if tag_name == "input":
            if element_type in ["checkbox", "radio", "email", "tel", "url", "number"]:
                return element_type
        return "text"

    def _generate_temp_field_value(self, field_name: str) -> str:
        # Simplified version for duplicate checking
        return f"temp_{field_name}"
