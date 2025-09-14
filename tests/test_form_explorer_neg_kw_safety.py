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

    class Browser:  # ダミー
        pass

    async_api.Page = Page
    async_api.Browser = Browser
    pw.async_api = async_api
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = async_api

import importlib


def test_negative_keywords_fallback_when_empty(monkeypatch):
    # link_exclusions が空配列
    import form_finder.form_explorer.form_explorer as fe
    import config.manager as cm

    def fake_rules():
        return {"link_exclusions": {"exclude_if_text_or_url_contains_any": []}}

    # reload 前に config.manager 側を差し替え、再インポート時の再束縛でも偽関数が適用されるようにする
    monkeypatch.setattr(cm, "get_form_finder_rules", fake_rules)
    importlib.reload(fe)

    explorer = fe.FormExplorer()
    neg = explorer._get_negative_keywords()
    assert neg  # 非空
    assert "comment" not in neg  # genericは含まれない
    assert any(k in neg for k in ["#comment", "comment-form", "commentform"])  # 強シグナルは残る


def test_negative_keywords_sanitizes_generic_comment(monkeypatch):
    # generic 'comment' が設定に含まれても除去
    import form_finder.form_explorer.form_explorer as fe
    import config.manager as cm

    def fake_rules():
        return {
            "link_exclusions": {
                "exclude_if_text_or_url_contains_any": ["comment", "comments", "/comment/", "採用", "求人"]
            }
        }

    # reload 前に config.manager 側を差し替え
    monkeypatch.setattr(cm, "get_form_finder_rules", fake_rules)
    importlib.reload(fe)

    explorer = fe.FormExplorer()
    neg = explorer._get_negative_keywords()
    assert "comment" not in neg and "comments" not in neg and "/comment/" not in neg
    assert "採用" in neg and "求人" in neg
