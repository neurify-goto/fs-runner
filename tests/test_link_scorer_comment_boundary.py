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


def test_comment_phrases_should_be_excluded():
    scorer = LinkScorer()
    assert scorer._is_excluded_link({"href": "/post", "text": "Leave a Reply"}) is True
    assert scorer._is_excluded_link({"href": "/post", "text": "Post Comment"}) is True

