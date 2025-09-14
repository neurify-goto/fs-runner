import os
import sys

# src/ をパスに追加（CIと一致）
sys.path.insert(0, os.path.abspath("src"))

from form_finder.form_explorer.link_scorer import LinkScorer


def test_document_request_should_not_be_excluded():
    scorer = LinkScorer()
    link = {"href": "https://example.com/document-request", "text": "Document Request"}
    assert scorer._is_excluded_link(link) is False


def test_comment_fragment_and_path_should_be_excluded():
    scorer = LinkScorer()
    assert scorer._is_excluded_link({"href": "https://example.com/post#comment", "text": "Go"}) is True
    assert scorer._is_excluded_link({"href": "https://example.com/comments/", "text": "Comments"}) is True
    # サフィックス付アンカーも除外（WordPress等の一般的形式）
    assert scorer._is_excluded_link({"href": "https://example.com/post#comment-5", "text": "Go"}) is True
    assert scorer._is_excluded_link({"href": "https://example.com/post#respond-123", "text": "Reply"}) is True


def test_comment_phrases_should_be_excluded():
    scorer = LinkScorer()
    assert scorer._is_excluded_link({"href": "/post", "text": "Leave a Reply"}) is True
    assert scorer._is_excluded_link({"href": "/post", "text": "Post Comment"}) is True


def test_comment_phrases_with_contact_should_still_be_excluded():
    scorer = LinkScorer()
    # 問い合わせ語が共存してもコメント系が優先されて除外されるべき
    link = {"href": "/post", "text": "Contact — Leave a Reply"}
    assert scorer._is_excluded_link(link) is True


def test_comment_fragment_with_contact_should_still_be_excluded():
    scorer = LinkScorer()
    link = {"href": "https://example.com/post#comment", "text": "Contact"}
    assert scorer._is_excluded_link(link) is True
