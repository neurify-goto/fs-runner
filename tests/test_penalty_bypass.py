import sys

sys.path.append('src')

from form_sender.analyzer.element_scorer import ElementScorer


def test_penalty_bypass_only_for_whitelisted_fields():
    scorer = ElementScorer()

    # class満点かつホワイトリスト（メールアドレス） → バイパスTrue
    sd_ok = {'score_breakdown': {'class': scorer.SCORE_WEIGHTS['class'], 'name': 0, 'id': 0, 'placeholder': 0, 'context': 0}}
    assert scorer._should_bypass_generic_text_penalty('メールアドレス', sd_ok) is True

    # class満点でも非ホワイトリスト → バイパスFalse
    assert scorer._should_bypass_generic_text_penalty('役職', sd_ok) is False

    # class不足 → バイパスFalse
    sd_low = {'score_breakdown': {'class': scorer.SCORE_WEIGHTS['class'] - 5, 'name': 0, 'id': 0, 'placeholder': 0, 'context': 0}}
    assert scorer._should_bypass_generic_text_penalty('メールアドレス', sd_low) is False

