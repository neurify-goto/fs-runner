from __future__ import annotations

"""氏名系ポストプロセス（RuleBasedAnalyzer からの委譲用、挙動不変）。"""

from typing import Any, Dict, Callable


def prune_suspect_name_mappings(field_mapping: Dict[str, Any], settings: Dict[str, Any]) -> None:
    try:
        if "統合氏名" not in field_mapping:
            return
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
            info = field_mapping.get(k)
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
                field_mapping.pop(k, None)
        try:
            per_field = (settings.get("min_score_threshold_per_field", {}) or {})
            min_name_score = int(per_field.get("名", 85))
            min_last_score = int(per_field.get("姓", 85))
        except Exception:
            min_name_score = 85
            min_last_score = 85
        for k, th in [("姓", min_last_score), ("名", min_name_score)]:
            info = field_mapping.get(k)
            if info and int(info.get("score", 0)) < th:
                field_mapping.pop(k, None)
    except Exception:
        pass


def fix_name_mapping_mismatch(field_mapping: Dict[str, Any]) -> None:
    def blob(key: str) -> str:
        info = (field_mapping.get(key, {}) or {})
        return " ".join(
            [
                str(info.get("selector", "")),
                str(info.get("name", "")),
                str(info.get("id", "")),
                str(info.get("class", "")),
            ]
        ).lower()

    def swap(a: str, b: str) -> None:
        if (a in field_mapping) and (b in field_mapping):
            field_mapping[a], field_mapping[b] = (field_mapping[b], field_mapping[a])

    def mismatch(sei_blob: str, mei_blob: str) -> bool:
        if not sei_blob or not mei_blob:
            return False
        return ("first" in sei_blob and "last" in mei_blob) or ("mei" in sei_blob and "sei" in mei_blob)

    if mismatch(blob("姓"), blob("名")):
        swap("姓", "名")
    if mismatch(blob("姓カナ"), blob("名カナ")) and ("kana" in blob("姓カナ") and "kana" in blob("名カナ")):
        swap("姓カナ", "名カナ")


async def normalize_kana_hiragana_fields(
    field_mapping: Dict[str, Any],
    form_structure: Any,
    get_element_details: Callable[..., Any],
) -> None:
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

    for kana_field, hira_field in [("姓カナ", "姓ひらがな"), ("名カナ", "名ひらがな")]:
        kinfo = field_mapping.get(kana_field)
        hinfo = field_mapping.get(hira_field)
        if kinfo and _is_hiragana_like(kinfo) and not hinfo:
            field_mapping[hira_field] = kinfo
            field_mapping.pop(kana_field, None)
        if hinfo and _is_katakana_like(hinfo) and not kinfo:
            field_mapping[kana_field] = hinfo
            field_mapping.pop(hira_field, None)

    if ("姓ひらがな" in field_mapping) and ("名ひらがな" in field_mapping):
        if "統合氏名カナ" in field_mapping:
            field_mapping.pop("統合氏名カナ", None)

    uinfo = field_mapping.get("統合氏名カナ")
    if uinfo and _is_hiragana_like(uinfo):
        try:
            has_split_hira = ("姓ひらがな" in field_mapping) and ("名ひらがな" in field_mapping)
            if has_split_hira:
                pass
            else:
                pass
        except Exception:
            pass

    try:
        if form_structure and getattr(form_structure, "elements", None):
            used_selectors = {
                (v or {}).get("selector", "") for v in field_mapping.values() if isinstance(v, dict)
            }
            for need, token in [("姓ひらがな", "姓"), ("名ひらがな", "名")]:
                if need in field_mapping:
                    continue
                for fe in form_structure.elements:
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
                        if ("ふりがな" in blob or "ひらがな" in blob) and (token in blob):
                            info = await get_element_details(fe.locator)
                            if info.get("selector", "") not in used_selectors:
                                field_mapping[need] = info
                                used_selectors.add(info.get("selector", ""))
                                break
                    except Exception:
                        continue
    except Exception:
        pass

