import os
import sys
import importlib

# src/ をパスに追加（CIと一致）
sys.path.insert(0, os.path.abspath("src"))


def test_dynamic_exclusions_are_applied_for_general_tokens(monkeypatch):
    # link_exclusions に login / download を登録
    import form_finder.form_explorer.link_scorer as ls

    def fake_rules():
        return {
            "link_exclusions": {
                "exclude_if_text_or_url_contains_any": [
                    "login", "ログイン", "download", "ダウンロード",
                    # generic が混入しても影響しないこと
                    "comment", "comments",
                ]
            }
        }

    import config.manager as cm
    monkeypatch.setattr(cm, "get_form_finder_rules", fake_rules)
    importlib.reload(ls)

    from form_finder.form_explorer.link_scorer import LinkScorer

    scorer = LinkScorer()
    base_url = "https://example.com"

    # 問い合わせ語が共存しても一般負キーワードは除外（ホワイトリスト対象外）
    assert scorer._is_excluded_link({"href": f"{base_url}/download", "text": "Contact / Download"}) is True
    assert scorer._is_excluded_link({"href": f"{base_url}/login", "text": "お問い合わせ"}) is True

    # generic comment は除外トリガにならない
    assert scorer._is_excluded_link({"href": f"{base_url}/post", "text": "Comments"}) is False


def test_whitelist_allows_recruitment_only_tokens(monkeypatch):
    # link_exclusions に careers を含める（採用系）
    import form_finder.form_explorer.link_scorer as ls

    def fake_rules():
        return {
            "link_exclusions": {
                "exclude_if_text_or_url_contains_any": ["careers", "jobs"]
            },
            "recruitment_only_exclusion": {
                "allow_if_general_contact_keywords_any": ["contact", "お問い合わせ"]
            }
        }

    import config.manager as cm
    monkeypatch.setattr(cm, "get_form_finder_rules", fake_rules)
    importlib.reload(ls)
    from form_finder.form_explorer.link_scorer import LinkScorer

    scorer = LinkScorer()

    # 問い合わせ語ホワイトリストにより採用系は許可
    assert scorer._is_excluded_link({"href": "/careers", "text": "Contact Careers"}) is False
    # 問い合わせ語が無い場合は除外
    assert scorer._is_excluded_link({"href": "/jobs", "text": "Jobs"}) is True

