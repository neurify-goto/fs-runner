import json
from src.form_sender.utils.privacy_consent_handler import PrivacyConsentHandler


def _cfg():
    return {
        "keywords": {
            "must": ["同意", "consent", "agree"],
            "context": ["個人情報", "プライバシ", "privacy", "policy", "terms", "規約"],
            "negative": ["メルマガ", "newsletter", "配信", "案内"],
        },
        "proximity_px": 600,
    }


def test_score_positive_privacy_agree():
    text = "個人情報の取り扱いに同意します（プライバシーポリシーに同意）"
    score = PrivacyConsentHandler._score(text, distance=120, proximity_px=600, cfg=_cfg())
    assert score > 2.5  # mustヒット＋contextヒット＋近接の加点


def test_score_negative_newsletter():
    text = "メルマガ配信に同意する"
    score = PrivacyConsentHandler._score(text, distance=10, proximity_px=600, cfg=_cfg())
    assert score == 0.0  # negativeワードで除外


def test_score_english_privacy():
    text = "I agree to the privacy policy and terms"
    score = PrivacyConsentHandler._score(text, distance=50, proximity_px=600, cfg=_cfg())
    assert score > 2.0

