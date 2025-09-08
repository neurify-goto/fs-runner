#!/usr/bin/env python3
"""
軽量ユニットテスト: メール救済の確認欄除外ロジック

GUI/ネットワーク不要。label(dom_label)に確認語が含まれる場合に除外されることを検証。
"""

def contains_confirm(text: str, tokens) -> bool:
    text = (text or "").lower()
    return any(t in text for t in tokens)


def test_dom_label_confirmation_excluded():
    confirm_tokens = {"confirm", "confirmation", "確認用", "再入力", "もう一度", "再度", "mail2", "re_mail", "re-email", "re-mail", "email2"}
    best_txt = "メールアドレス"
    attrs = "name=f5 class=input-text"
    dom_label = "メールアドレス（確認用）"  # ← ここにのみ確認語が含まれるケース
    full_blob = (best_txt + " " + attrs + " " + dom_label).lower()
    assert contains_confirm(full_blob, {t.lower() for t in confirm_tokens})


if __name__ == "__main__":
    test_dom_label_confirmation_excluded()
    print("OK: confirmation token detected in dom_label")

