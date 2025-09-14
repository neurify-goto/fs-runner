import os
import sys
import types

# src/ をパスに追加（CIと一致）
sys.path.insert(0, os.path.abspath("src"))

# Playwright の軽量スタブ（重依存を避ける）
if "playwright.async_api" not in sys.modules:
    pw = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")

    class Page:  # ダミー
        pass

    async_api.Page = Page
    pw.async_api = async_api
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = async_api

from form_finder.form_explorer.form_detector import FormDetector


def test_contact_comments_field_should_not_be_excluded():
    det = FormDetector()
    form_data = {
        "formId": "",
        "formClass": "",
        "surroundingText": "Contact Comments",
        "buttons": [{"text": "Send"}],
        "inputs": [
            {"placeholder": "Your message", "name": "message", "id": "message"},
        ],
    }
    # 問い合わせ語 'Contact' と generic 'Comments' が共存 → very-strong が無いので除外しない
    assert det._is_comment_form(form_data) is False


def test_leave_a_reply_should_be_excluded_even_with_contact():
    det = FormDetector()
    form_data = {
        "formId": "",
        "formClass": "",
        "surroundingText": "Contact — Leave a Reply",
        "buttons": [{"text": "Post Comment"}],
        "inputs": [],
    }
    # very-strong フレーズが存在するため、問い合わせ語があっても除外
    assert det._is_comment_form(form_data) is True

