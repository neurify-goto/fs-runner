"""FieldMapper のフィールド固有ガードを分離（振る舞い不変）。

各フィールド（メール/電話/郵便番号/都道府県）に対し、
要素属性やコンテキストに基づいて『採用してよいか』を判定する。

Public API:
 - passes_safeguard(field_name, best_score_details, best_context, context_text_extractor, field_patterns, settings) -> bool
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _passes_kana_like(field_name: str, ei: Dict[str, Any], best_txt: str) -> bool:
    """カナ/ひらがな系フィールドの安全ガード。

    汎用方針:
    - 属性(name/id/class/placeholder) もしくはラベル/見出し(best_txt)のいずれかに
      カナ/ふりがな指標が存在しない場合は不採用。
    - 明確な非対象語（性別/sex/gender）が含まれる場合は不採用。

    これにより、必須ブーストにより type=text の汎用入力へ誤って割当てられる
    事象を抑止する。
    """
    attrs = _attrs_blob(ei)
    neg_tokens = ["性別", "sex", "gender"]
    if any(t in attrs for t in neg_tokens) or any(t in best_txt for t in neg_tokens):
        return False

    kana_tokens = [
        "kana",
        "katakana",
        "furigana",
        "カナ",
        "カタカナ",
        "フリガナ",
        "ふりがな",
        # 氏名カナで用いられやすい表記
        "セイ",
        "メイ",
    ]
    hira_tokens = [
        "hiragana",
        "ひらがな",
    ]

    # フィールド種別に応じた指標セット
    if field_name in {"統合氏名カナ", "姓カナ", "名カナ"}:
        indicators = kana_tokens
    elif field_name in {"姓ひらがな", "名ひらがな"}:
        # ふりがな（カタカナ）のケースも現実には混在するため、
        # ひらがな専用トークンに加えて『ふりがな』も許容
        indicators = hira_tokens + ["ふりがな", "フリガナ"]
    else:
        return True  # 対象外フィールド

    has_indicator = any(t.lower() in attrs for t in indicators) or any(
        t.lower() in best_txt for t in indicators
    )
    return bool(has_indicator)


def _best_context_text(best_context: Optional[List], ctx_extractor) -> str:
    try:
        return (ctx_extractor.get_best_context_text(best_context) or "").lower()
    except Exception:
        return ""


def _attrs_blob(ei: Dict[str, Any]) -> str:
    try:
        return " ".join(
            [
                str(ei.get("name") or ""),
                str(ei.get("id") or ""),
                str(ei.get("class") or ""),
                str(ei.get("placeholder") or ""),
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
    # 属性: tel/phone に加え、日本語の『電話』『携帯』も許可（type=text な和名属性でも通す）
    pos_attr = any(t in attrs_blob for t in ["tel", "phone", "電話", "携帯"])
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
    """都道府県フィールドの安全ガード。

    ポイント:
    - 無条件許可はしない。
    - select であっても、属性/ラベルに都道府県系トークンが無ければ不許可（オプション検証は候補フィルタ側で実施）。
    - 非select の場合は、属性/コンテキストに強いトークンが必要。
    """
    tag = (ei.get("tag_name") or "").lower()
    attrs_blob = _attrs_blob(ei)

    # 属性/ラベルに『都道府県』、または 'pref' 'prefecture' が含まれることを要求
    pos_attr = ("都道府県" in attrs_blob) or ("prefecture" in attrs_blob) or ("pref" in attrs_blob)
    pos_ctx = any(t in best_txt for t in ["都道府県", "prefecture"])

    if tag == "select":
        return bool(pos_attr or pos_ctx)
    # 非selectはより厳格（属性または強いラベル）
    return bool(pos_attr or pos_ctx)

def _passes_message(ei: Dict[str, Any], best_txt: str) -> bool:
    """お問い合わせ本文テキストエリアの安全ガード。

    - 住所系の強いトークン（microformats や address/住所 等）を含む場合は不許可
    - 可能であれば『お問い合わせ/内容/メッセージ』系の文脈を優先
    """
    attrs = _attrs_blob(ei)
    # 住所系・microformatsトークン
    address_like = [
        "住所", "address", "addr", "street", "city", "prefecture", "都道府県", "市区町村",
        "p-region", "p-locality", "p-street-address", "p-extended-address",
    ]
    if any(t in attrs for t in address_like) or any(t in best_txt for t in address_like):
        # メッセージ系の強い指標が同時にある場合のみ許容
        msg_tokens = ["お問い合わせ", "メッセージ", "本文", "内容", "message", "inquiry", "contact"]
        has_msg = any(t in best_txt for t in msg_tokens) or any(t in attrs for t in msg_tokens)
        if not has_msg:
            return False
    return True


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
    if field_name == "お問い合わせ本文":
        return _passes_message(ei, best_txt)
    if field_name == "役職":
        # 役職/職位/position/job title のいずれかの指標が属性/ラベルに必要
        attrs = _attrs_blob(ei)
        pos_tokens = [
            "役職", "職位", "position", "job title", "title", "役割", "ポジション"
        ]
        neg_ctx_tokens = ["知ったきっかけ", "きっかけ", "how did you hear", "referrer"]
        if any(t in attrs for t in pos_tokens) or any(t in best_txt for t in pos_tokens):
            if not any(t in best_txt for t in neg_ctx_tokens):
                return True
        return False

    # カナ/ひらがな系の安全ガード
    if field_name in {"統合氏名カナ", "姓カナ", "名カナ", "姓ひらがな", "名ひらがな"}:
        return _passes_kana_like(field_name, ei, best_txt)

    return True
