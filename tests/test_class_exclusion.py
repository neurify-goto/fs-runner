import sys

sys.path.append('src')

from form_sender.analyzer.element_scorer import ElementScorer


def make_elem_info(class_value: str):
    return {
        'name': '',
        'id': '',
        'class': class_value,
        'placeholder': ''
    }


def test_class_exclusion_hyphen_and_underscore_security_tokens():
    scorer = ElementScorer()
    fp = {'exclude_patterns': ['password', 'verification', 'auth']}

    # ハイフン/アンダースコア連結でも除外されるべき
    assert scorer._is_excluded_element(make_elem_info('user-password input'), fp) is True
    assert scorer._is_excluded_element(make_elem_info('email_verification field'), fp) is True
    assert scorer._is_excluded_element(make_elem_info('user-auth-input'), fp) is True


def test_class_exclusion_should_not_block_last_first_name():
    scorer = ElementScorer()
    # 『name』のような短い汎用語では last-name を誤除外しない
    fp = {'exclude_patterns': ['name']}
    assert scorer._is_excluded_element(make_elem_info('input-last-name entry-component'), fp) is False
    assert scorer._is_excluded_element(make_elem_info('input-first-name entry-component'), fp) is False

