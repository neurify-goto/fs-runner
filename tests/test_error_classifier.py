import pytest

from src.form_sender.utils.error_classifier import ErrorClassifier


@pytest.mark.parametrize(
    "message,expected",
    [
        ("HTTP 429 Too Many Requests", "RATE_LIMIT"),
        ("DDoS protection by Cloudflare. Just a moment...", "WAF_CHALLENGE"),
        ("Access Denied. Akamai Reference #18.5dc51102.169", "WAF_CHALLENGE"),
        ("CSRF token mismatch or invalid", "CSRF_ERROR"),
        ("メールアドレスの形式が正しくありません", "VALIDATION_FORMAT"),
        ("必須項目を入力してください", "MAPPING"),
        ("net::ERR_NAME_NOT_RESOLVED while navigating", "DNS_ERROR"),
        ("SSL: CERTIFICATE_VERIFY_FAILED", "TLS_ERROR"),
        ("Timeout 30000ms exceeded during navigation", "TIMEOUT"),
        ("Final submit button not found on confirmation page", "SUBMIT_BUTTON_NOT_FOUND"),
        ("element is not visible and has zero size", "ELEMENT_NOT_INTERACTABLE"),
    ],
)
def test_classify_form_submission_error(message, expected):
    code = ErrorClassifier.classify_form_submission_error(
        error_message=message,
        has_url_change=False,
        page_content=message,  # 一部のケースはページ本文に依存
        submit_selector=".btn-submit",
    )
    assert code == expected

def test_mapping_when_selector_missing_but_required_text_present():
    code = ErrorClassifier.classify_form_submission_error(
        error_message="",
        has_url_change=False,
        page_content="必須項目を入力してください",
        submit_selector="",  # セレクタ欠落
    )
    assert code == "MAPPING"

def test_validation_format_from_error_message_without_selector():
    code = ErrorClassifier.classify_form_submission_error(
        error_message="Invalid email format",
        has_url_change=False,
        page_content="",
        submit_selector="",  # セレクタ欠落
    )
    assert code == "VALIDATION_FORMAT"


def test_classify_detail_http_status():
    detail = ErrorClassifier.classify_detail(
        error_message="",
        http_status=403,
        page_content="DDoS protection by Cloudflare",
    )
    assert detail["code"] in {"WAF_CHALLENGE", "ACCESS"}
    assert isinstance(detail["retryable"], bool)
    assert "category" in detail


def test_classify_detail_confidence_scores():
    detail = ErrorClassifier.classify_detail(error_message="DNS lookup failed")
    assert 0.0 <= detail["confidence"] <= 1.0


def test_external_config_patterns_rate_limit():
    # config/error_classification.json に定義済み: "throttled" -> RATE_LIMIT
    code = ErrorClassifier.classify_form_submission_error(
        error_message="Request throttled due to rate limiting",
        has_url_change=False,
        page_content="",
        submit_selector=".btn",
    )
    assert code == "RATE_LIMIT"
