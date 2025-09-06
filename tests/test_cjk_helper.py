import pytest

from src.form_sender.analyzer.element_scorer import ElementScorer


def test_has_cjk_true_for_kanji_katakana():
    scorer = ElementScorer()
    assert scorer._has_cjk("東京都") is True
    assert scorer._has_cjk("カタカナ") is True


def test_has_cjk_false_for_ascii():
    scorer = ElementScorer()
    assert scorer._has_cjk("email") is False

