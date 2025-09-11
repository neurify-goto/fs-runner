import pytest
import os
import sys

# テスト実行時に src/ をパスに追加（CIと一致させる）
sys.path.insert(0, os.path.abspath("src"))

from form_finder.form_explorer.form_detector import FormDetector


def _base_inputs():
    return [
        {
            "type": "text",
            "tagName": "input",
            "name": "name",
            "id": "name",
            "placeholder": "お名前",
        }
    ]


def _base_buttons():
    return [
        {
            "type": "submit",
            "text": "送信",
            "className": "btn",
        }
    ]


def test_is_recruitment_only_form_excludes_when_only_recruitment_terms():
    detector = FormDetector()
    form_data = {
        "inputs": _base_inputs(),
        "buttons": _base_buttons(),
        "surroundingText": "学歴 と 経歴 をご入力ください",
    }
    # 直接判定
    assert detector._is_recruitment_only_form(form_data) is True

    # 品質チェックでも除外される（前提条件は満たしている）
    assert detector._validate_form_quality(form_data, "<html></html>") is False


def test_is_recruitment_only_form_allows_when_contact_keywords_present():
    detector = FormDetector()
    form_data = {
        "inputs": _base_inputs(),
        "buttons": _base_buttons(),
        # 採用系 + 問い合わせ系が混在 → 許可
        "surroundingText": "お問い合わせ（学歴の記載は任意）",
    }
    assert detector._is_recruitment_only_form(form_data) is False
    # 品質チェックは通過（送信ボタンと入力あり）
    assert detector._validate_form_quality(form_data, "<html></html>") is True


def test_is_recruitment_only_form_allows_when_contact_is_uppercase_english():
    detector = FormDetector()
    form_data = {
        "inputs": _base_inputs(),
        "buttons": _base_buttons(),
        # 英語UIで大文字 CONTACT を含む（兼用扱いで許可）
        "surroundingText": "Please CONTACT us. 学歴 の記載は任意です.",
    }
    assert detector._is_recruitment_only_form(form_data) is False
    assert detector._validate_form_quality(form_data, "<html></html>") is True


def test_is_recruitment_only_form_allows_when_contact_is_capitalized():
    detector = FormDetector()
    form_data = {
        "inputs": _base_inputs(),
        "buttons": _base_buttons(),
        # Contact（先頭大文字）も許容対象
        "surroundingText": "For Contact or Inquiry, 学歴 は不要です.",
    }
    assert detector._is_recruitment_only_form(form_data) is False
    assert detector._validate_form_quality(form_data, "<html></html>") is True
