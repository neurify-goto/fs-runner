import os
import sys

# src/ をパスに追加
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


def test_form_with_school_keyword_is_invalid():
    detector = FormDetector()
    form_data = {
        "inputs": _base_inputs(),
        "buttons": _base_buttons(),
        "surroundingText": "学校 関係の問い合わせはこちら",
    }
    assert detector._validate_form_quality(form_data, "<html></html>") is False


def test_form_without_school_keyword_is_valid():
    detector = FormDetector()
    form_data = {
        "inputs": _base_inputs(),
        "buttons": _base_buttons(),
        "surroundingText": "お問い合わせは下記フォームより送信してください",
    }
    assert detector._validate_form_quality(form_data, "<html></html>") is True

