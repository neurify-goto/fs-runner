from __future__ import annotations

"""必須要素救済ロジックの分離（振る舞い不変）。

FieldMapper._ensure_required_mappings の実装を移し、
公開APIは RequiredRescue.ensure_required_mappings として提供する。
"""

from typing import Any, Dict, List, Callable
from playwright.async_api import Locator
import logging

logger = logging.getLogger(__name__)


class RequiredRescue:
    def __init__(
        self,
        *,
        element_scorer,
        context_text_extractor,
        field_patterns,
        settings: Dict[str, Any],
        duplicate_prevention,
        create_enhanced_element_info: Callable[..., Any],
        generate_temp_field_value: Callable[[str], str],
        is_confirmation_field_func: Callable[[Dict[str, Any], List], bool],
        infer_logical_name_func: Callable[[Dict[str, Any], List], str],
    ) -> None:
        self.element_scorer = element_scorer
        self.context_text_extractor = context_text_extractor
        self.field_patterns = field_patterns
        self.settings = settings
        self.duplicate_prevention = duplicate_prevention
        self._create_enhanced_element_info = create_enhanced_element_info
        self._generate_temp_field_value = generate_temp_field_value
        self._is_confirmation_field = is_confirmation_field_func
        self._infer_logical_field_name_for_required = infer_logical_name_func

    async def ensure_required_mappings(
        self,
        classified_elements: Dict[str, List[Locator]],
        field_mapping: Dict[str, Dict[str, Any]],
        used_elements: set,
        required_elements_set: set,
    ) -> None:
        # used_names_ids は先行救済ブロックでも参照されるため先に初期化
        used_names_ids = set()
        # 先行救済: 郵便番号（単一フィールド）の取りこぼし対策
        # - name/id/class/label に zip/postal/郵便番号 系トークンがある input[type=tel/text]
        # - フィールド未確定時のみ
        try:
            if "郵便番号" not in field_mapping:
                postal_candidates = (
                    (classified_elements.get("tel_inputs") or [])
                    + (classified_elements.get("text_inputs") or [])
                )
                for el in postal_candidates:
                    if id(el) in used_elements:
                        continue
                    ei = await self.element_scorer._get_element_info(el)
                    if not ei.get("visible", True):
                        continue
                    nm = (ei.get("name", "") or "").lower()
                    ide = (ei.get("id", "") or "").lower()
                    cls = (ei.get("class", "") or "").lower()
                    attrs = f"{nm} {ide} {cls}"
                    if not any(t in attrs for t in ["zip", "postal", "postcode", "zipcode", "郵便", "郵便番号", "〒"]):
                        # ラベル側にもヒントが無ければ次へ
                        contexts = await self.context_text_extractor.extract_context_for_element(el)
                        best_txt = (self.context_text_extractor.get_best_context_text(contexts) or "").lower()
                        if not any(t in best_txt for t in ["郵便番号", "郵便", "zip", "postal", "〒"]):
                            continue
                    # フィールド固有ガード（誤検出抑止）
                    from .mapping_safeguards import passes_safeguard as _passes
                    contexts = await self.context_text_extractor.extract_context_for_element(el)
                    details = {"element_info": ei, "total_score": 85}
                    if not _passes("郵便番号", details, contexts, self.context_text_extractor, {}, self.settings):
                        continue
                    info = await self._create_enhanced_element_info(el, details, contexts)
                    try:
                        info["source"] = "required_rescue_postal"
                    except Exception:
                        pass
                    temp_value = self._generate_temp_field_value("郵便番号")
                    if self.duplicate_prevention.register_field_assignment(
                        "郵便番号", temp_value, 85, info
                    ):
                        field_mapping["郵便番号"] = info
                        used_elements.add(id(el))
                        used_names_ids.add((nm, ide))
                        break
        except Exception as e:
            logger.debug(f"Required postal rescue skipped: {e}")
        # 先行救済: name/id='email' の必須入力を確実に登録（確認欄は除外）
        try:
            if "メールアドレス" not in field_mapping:
                for bucket in ["email_inputs", "text_inputs", "other_inputs"]:
                    for el in classified_elements.get(bucket, []) or []:
                        if id(el) in used_elements:
                            continue
                        ei = await self.element_scorer._get_element_info(el)
                        nm = (ei.get("name", "") or "").lower()
                        ide = (ei.get("id", "") or "").lower()
                        attrs = (
                            nm + " " + ide + " " + (ei.get("class", "") or "")
                        ).lower()
                        # 罠/スパム対策フィールドは除外
                        if any(t in attrs for t in ["honeypot", "honey", "trap", "botfield", "no-print", "noprint"]):
                            continue
                        if nm == "email" or ide == "email":
                            # 確認/チェック用でない & 必須マーカー
                            if any(k in attrs for k in ["confirm", "確認", "check"]):
                                continue
                            is_req = await self.element_scorer._detect_required_status(
                                el
                            )
                            if not is_req:
                                # ラベル側の必須判定（補助）
                                contexts = await self.context_text_extractor.extract_context_for_element(
                                    el
                                )
                                req_markers = ["*", "必須", "[必須]", "［必須］"]
                                txts = " ".join(
                                    [
                                        (getattr(c, "text", "") or "")
                                        for c in (contexts or [])
                                    ]
                                )
                                is_req = any(m in txts for m in req_markers)
                            if is_req:
                                details = {"element_info": ei, "total_score": 85}
                                info = await self._create_enhanced_element_info(
                                    el, details, []
                                )
                                try:
                                    info["source"] = "required_rescue_email_attr"
                                except Exception:
                                    pass
                                temp_value = self._generate_temp_field_value(
                                    "メールアドレス"
                                )
                                if self.duplicate_prevention.register_field_assignment(
                                    "メールアドレス", temp_value, 85, info
                                ):
                                    field_mapping["メールアドレス"] = info
                                    used_elements.add(id(el))
                                    used_names_ids.add(
                                        (ei.get("name", ""), ei.get("id", ""))
                                    )
                                    raise StopIteration
        except StopIteration:
            pass
        except Exception as e:
            logger.debug(f"pre-salvage email failed: {e}")
        # 既存の field_mapping から重複防止用の name/id を収集
        for info in field_mapping.values():
            try:
                used_names_ids.add((info.get("name", ""), info.get("id", "")))
            except Exception:
                pass

        required_markers = [
            "*",
            "必須",
            "Required",
            "Mandatory",
            "Must",
            "(必須)",
            "（必須）",
            "[必須]",
            "［必須］",
        ]
        allowed_required_sources = {
            "dt_label",
            "th_label",
            "th_label_index",
            "label_for",
            "aria_labelledby",
            "label_element",
        }

        buckets = [
            "email_inputs",
            "tel_inputs",
            "url_inputs",
            "number_inputs",
            "text_inputs",
            "textareas",
            "selects",
            "radios",
            "checkboxes",
        ]
        auto_counter = 1
        for bucket in buckets:
            for el in classified_elements.get(bucket, []):
                try:
                    if id(el) in used_elements:
                        continue
                    ei = await self.element_scorer._get_element_info(el)
                    nm = (ei.get("name") or "").strip()
                    idv = (ei.get("id") or "").strip()
                    in_known_required = (
                        nm in required_elements_set or idv in required_elements_set
                    )
                    if not in_known_required:
                        low = f"{nm} {idv}".lower()
                        if ("must" in low) or ("required" in low):
                            in_known_required = True
                    has_required_marker = False
                    if not in_known_required:
                        try:
                            contexts = await self.context_text_extractor.extract_context_for_element(
                                el
                            )
                            for ctx in contexts or []:
                                if (
                                    getattr(ctx, "source_type", "")
                                    in allowed_required_sources
                                ):
                                    txt = ctx.text or ""
                                    if any(m in txt for m in required_markers):
                                        has_required_marker = True
                                        break
                        except Exception:
                            has_required_marker = False
                    if not (in_known_required or has_required_marker):
                        continue

                    if (nm, idv) in used_names_ids:
                        continue

                    if self._is_nonfillable_required(ei):
                        continue

                    contexts = (
                        await self.context_text_extractor.extract_context_for_element(
                            el
                        )
                    )
                    field_name = self._infer_logical_field_name_for_required(ei, contexts)
                    # テキストエリア誤判定救済: 住所系テキストエリアを『お問い合わせ本文』にしない
                    try:
                        if field_name == "お問い合わせ本文":
                            texts = " ".join([(getattr(c, 'text', '') or '') for c in (contexts or [])])
                            blob = " ".join([
                                (ei.get("name", "") or ""),
                                (ei.get("id", "") or ""),
                                (ei.get("class", "") or ""),
                                (ei.get("placeholder", "") or ""),
                                texts,
                            ]).lower()
                            addr_tokens = [
                                "住所", "address", "addr", "street", "city", "prefecture", "都道府県", "市区町村",
                                "p-region", "p-locality", "p-street-address", "p-extended-address",
                            ]
                            if any(t in blob for t in addr_tokens):
                                field_name = "住所"
                    except Exception:
                        pass

                    # フィールド固有の早期除外（誤検出抑止）
                    try:
                        from .candidate_filters import allow_candidate as _allow
                        if not await _allow(field_name, el, ei):
                            continue
                    except Exception:
                        pass

                    # 重要フィールドはセーフガードを通す
                    try:
                        if field_name in {"メールアドレス", "電話番号", "郵便番号", "都道府県", "お問い合わせ本文"}:
                            from .mapping_safeguards import passes_safeguard as _pass
                            # 簡易スコア詳細（最低限）
                            details_tmp = {"element_info": ei, "total_score": 60}
                            if not _pass(field_name, details_tmp, contexts, self.context_text_extractor, {}, self.settings):
                                continue
                    except Exception:
                        pass

                    try:
                        if field_name.startswith("auto_required_text_"):
                            ctx_texts = " ".join(
                                [
                                    (getattr(c, "text", "") or "")
                                    for c in (contexts or [])
                                ]
                            )
                            # カナ救済
                            if any(
                                t in ctx_texts
                                for t in ["ふりがな", "フリガナ", "カナ", "かな"]
                            ):
                                field_name = "統合氏名カナ"
                            else:
                                # 郵便番号/住所 救済（placeholder/属性もヒントに）
                                blob = " ".join(
                                    [
                                        (ei.get("name", "") or ""),
                                        (ei.get("id", "") or ""),
                                        (ei.get("class", "") or ""),
                                        (ei.get("placeholder", "") or ""),
                                        ctx_texts,
                                    ]
                                ).lower()
                                # 先に 郵便番号 系を優先判定（住所系文脈に埋もれやすいため）
                                postal_tokens = [
                                    "郵便", "郵便番号", "postal", "postcode", "post_code", "zip", "zipcode", "〒",
                                    "上3桁", "前3桁", "下4桁", "後4桁",
                                ]
                                if any(t in blob for t in postal_tokens):
                                    # 分割ヒント
                                    name_id = (ei.get("name", "") + " " + ei.get("id", "")).lower()
                                    if any(h in name_id for h in ["-1", "_1", "1"]):
                                        field_name = "郵便番号1"
                                    elif any(h in name_id for h in ["-2", "_2", "2"]):
                                        field_name = "郵便番号2"
                                    else:
                                        field_name = "郵便番号"
                                else:
                                    # 住所救済
                                    addr_tokens = [
                                        "住所",
                                        "所在地",
                                        "address",
                                        "addr",
                                        "street",
                                        "city",
                                        "都道府県",
                                        "prefecture",
                                        "郵便",
                                        "zip",
                                        "postal",
                                        "県",
                                        "市",
                                        "区",
                                        "丁目",
                                        "番地",
                                        "-",
                                        "ー",
                                        "－",
                                    ]
                                    if any(t in blob for t in addr_tokens):
                                        field_name = "住所"
                                addr_tokens = [
                                    "住所",
                                    "所在地",
                                    "address",
                                    "addr",
                                    "street",
                                    "city",
                                    "都道府県",
                                    "prefecture",
                                    "郵便",
                                    "zip",
                                    "postal",
                                    "県",
                                    "市",
                                    "区",
                                    "丁目",
                                    "番地",
                                    "-",
                                    "ー",
                                    "－",
                                ]
                                if any(t in blob for t in addr_tokens):
                                    field_name = "住所"
                        # 住所の重複救済/郵便番号の誤判定是正
                        # まず、住所と判定されたが郵便番号らしい場合は郵便番号へ補正
                        try:
                            blob2 = " ".join([
                                (ei.get("name", "") or ""),
                                (ei.get("id", "") or ""),
                                (ei.get("class", "") or ""),
                                (ei.get("placeholder", "") or ""),
                                " ".join([(getattr(c, 'text', '') or '') for c in (contexts or [])]),
                            ]).lower()
                        except Exception:
                            blob2 = ""
                        if field_name == "住所":
                            postal_hint = any(t in blob2 for t in [
                                "郵便", "郵便番号", "postal", "postcode", "post_code", "zip", "zipcode", "〒", "post"
                            ])
                            if postal_hint:
                                name_id = (ei.get("name", "") + " " + ei.get("id", "")).lower()
                                if any(h in name_id for h in ["-1", "_1", "1"]):
                                    field_name = "郵便番号1"
                                elif any(h in name_id for h in ["-2", "_2", "2"]):
                                    field_name = "郵便番号2"
                                else:
                                    field_name = "郵便番号"

                        # 住所の重複救済: すでに『住所』が確定済みの場合は補助スロットへ振り分け
                        if field_name == "住所" and "住所" in field_mapping:
                            suffix = 1
                            while f"住所_補助{suffix}" in field_mapping:
                                suffix += 1
                            field_name = f"住所_補助{suffix}"
                        elif self._is_confirmation_field(ei, contexts) or any(
                            t
                            in (
                                (ei.get("name", "").lower())
                                + " "
                                + (ei.get("id", "").lower())
                            )
                            for t in [
                                "mail2",
                                "email2",
                                "email_check",
                                "mail_check",
                                "confirm-email",
                                "email-confirm",
                            ]
                        ):
                            field_name = f"auto_email_confirm_{auto_counter}"
                    except Exception:
                        pass

                    try:
                        patterns_for_field = (
                            self.field_patterns.get_pattern(field_name) or {}
                        )
                        if await self.element_scorer._is_excluded_element_with_context(
                            ei, el, patterns_for_field
                        ):
                            continue
                    except Exception:
                        pass

                    try:
                        if field_name.startswith("auto_email_confirm_"):
                            key = field_name
                        elif field_name.startswith("auto_required_text_"):
                            base = "auto_required_text_"
                            n = 1
                            while f"{base}{n}" in field_mapping:
                                n += 1
                            key = f"{base}{n}"
                        else:
                            key = field_name
                    except Exception:
                        key = field_name

                    salvage_score = max(
                        15, int(self.settings.get("min_score_threshold", 70))
                    )
                    details = {"element_info": ei, "total_score": salvage_score}
                    info = await self._create_enhanced_element_info(
                        el, details, contexts
                    )
                    try:
                        info["source"] = "required_rescue"
                    except Exception:
                        pass
                    info["required"] = True
                    if field_name.startswith("auto_email_confirm_"):
                        info["input_type"] = "email"
                        try:
                            info["auto_action"] = "copy_from"
                            info["copy_from_field"] = "メールアドレス"
                        except Exception:
                            pass

                    temp_value = self._generate_temp_field_value(key)
                    if self.duplicate_prevention.register_field_assignment(
                        key, temp_value, salvage_score, info
                    ):
                        field_mapping[key] = info
                        used_elements.add(id(el))
                        used_names_ids.add((nm, idv))
                except Exception as e:
                    logger.debug(f"Ensure required mapping for element failed: {e}")

    def _is_nonfillable_required(self, element_info: Dict[str, Any]) -> bool:
        name_id_cls = (
            (element_info.get("name", "") or "")
            + " "
            + (element_info.get("id", "") or "")
            + " "
            + (element_info.get("class", "") or "")
        ).lower()
        input_type = (element_info.get("type") or "").lower()
        tag = (element_info.get("tag_name") or "").lower()

        blacklist = [
            "captcha",
            "image_auth",
            "image-auth",
            "spam-block",
            "token",
            "otp",
            "verification",
            "email_confirm",
            "mail_confirm",
            "email_confirmation",
            "confirm_email",
            "confirm",
            "re_email",
            "re-mail",
            "login",
            "signin",
            "sign_in",
            "auth",
            "authentication",
            "login_id",
            "password",
            "pass",
            "pswd",
            "mfa",
            "totp",
        ]
        if any(b in name_id_cls for b in blacklist):
            return True
        if input_type in ["checkbox", "radio"]:
            return True
        if tag == "select":
            return True
        return False
