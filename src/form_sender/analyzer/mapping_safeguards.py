"""FieldMapper のフィールド固有ガードを分離（振る舞い不変）。

各フィールド（メール/電話/郵便番号/都道府県）に対し、
要素属性やコンテキストに基づいて『採用してよいか』を判定する。

Public API:
 - passes_safeguard(field_name, best_score_details, best_context, context_text_extractor, field_patterns, settings) -> bool
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _best_context_text(best_context: Optional[List], ctx_extractor) -> str:
    try:
        return (ctx_extractor.get_best_context_text(best_context) or "").lower()
    except Exception:
        return ""


def _attrs_blob(ei: Dict[str, Any]) -> str:
    try:
        return " ".join(
            [
                (ei.get("name") or ""),
                (ei.get("id") or ""),
                (ei.get("class") or ""),
                (ei.get("placeholder") or ""),
            ]
        ).lower()
    except Exception:
        return ""


def _passes_email(ei: Dict[str, Any], best_txt: str) -> bool:
    etype = (ei.get("type") or "").lower()
    attrs_blob = _attrs_blob(ei)
    email_tokens = ["email", "e-mail", "mail", "メール"]
    is_semantic_email = any(t in attrs_blob for t in email_tokens) or any(
        t in best_txt for t in email_tokens
    )
    return bool(etype == "email" or is_semantic_email)


def _passes_phone(ei: Dict[str, Any], best_txt: str) -> bool:
    etype = (ei.get("type") or "").lower()
    attrs_blob = _attrs_blob(ei)
    pos_attr = any(t in attrs_blob for t in ["tel", "phone"])
    pos_ctx = any(t in best_txt for t in ["電話", "tel", "phone", "携帯", "mobile", "cell"])
    neg_ctx = any(t in best_txt for t in ["時", "時頃", "午前", "午後", "連絡方法"]) or any(
        t in attrs_blob for t in ["timeno", "h1", "h2"]
    )
    return bool(etype == "tel" or pos_attr or (pos_ctx and not neg_ctx))


def _passes_postal(ei: Dict[str, Any], best_txt: str) -> bool:
    attrs_blob = _attrs_blob(ei)
    pos_attr = any(
        t in attrs_blob for t in ["zip", "postal", "postcode", "zipcode", "郵便", "〒"]
    )
    pos_ctx = any(t in best_txt for t in ["郵便番号", "郵便", "〒", "postal", "zip"])
    neg = any(
        t in attrs_blob
        for t in [
            "captcha",
            "image_auth",
            "token",
            "otp",
            "verification",
            "confirm",
            "確認",
        ]
    )
    return bool((pos_attr or pos_ctx) and not neg)


def _passes_prefecture(ei: Dict[str, Any], best_txt: str) -> bool:
    tag = (ei.get("tag_name") or "").lower()
    attrs_blob = _attrs_blob(ei)
    if tag == "select":
        return True  # 都道府県は select 優遇（元実装準拠）
    # select でない場合は、強い都道府県語が必要
    pos_ctx = any(t in best_txt for t in ["都道府県", "prefecture"])
    pos_attr = "pref" in attrs_blob or "都道府県" in attrs_blob
    return bool(pos_ctx or pos_attr)


def passes_safeguard(
    field_name: str,
    best_score_details: Dict[str, Any],
    best_context: Optional[List],
    context_text_extractor,
    field_patterns: Dict[str, Any],  # 互換のため受け取るが未使用の場合あり
    settings: Dict[str, Any],  # 互換のため受け取る
) -> bool:
    """フィールド固有の採用ガードに合格するか。

    元の FieldMapper 実装の if/guard ロジックをそのまま移植し、
    True=採用可 / False=不採用 を返す。
    """
    ei = (best_score_details or {}).get("element_info", {})
    best_txt = _best_context_text(best_context, context_text_extractor)

    if field_name == "メールアドレス":
        return _passes_email(ei, best_txt)
    if field_name == "電話番号":
        return _passes_phone(ei, best_txt)
    if field_name == "郵便番号":
        return _passes_postal(ei, best_txt)
    if field_name == "都道府県":
        return _passes_prefecture(ei, best_txt)

    return True

