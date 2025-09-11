import os
import sys

# src/ をパスに追加（CIと一致）
sys.path.insert(0, os.path.abspath("src"))

from form_finder.form_explorer.link_scorer import LinkScorer


def test_filter_valid_links_excludes_recruitment_pages():
    scorer = LinkScorer()

    base_url = "https://example.com"
    links = [
        {"href": "https://example.com/contact", "text": "お問い合わせ"},
        {"href": "https://example.com/recruit", "text": "採用情報"},
        {"href": "https://example.com/careers", "text": "Careers"},
        {"href": "https://example.com/jobs", "text": "Jobs"},
        {"href": "https://example.com/entry", "text": "エントリー"},
    ]

    valid = scorer.filter_valid_links(links, base_url)

    # 採用/応募系のURLは除外され、contactのみ残る想定
    hrefs = [l["href"] for l in valid]
    assert "https://example.com/contact" in hrefs
    assert "https://example.com/recruit" not in hrefs
    assert "https://example.com/careers" not in hrefs
    assert "https://example.com/jobs" not in hrefs
    assert "https://example.com/entry" not in hrefs

