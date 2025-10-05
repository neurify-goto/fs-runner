import copy

import pytest

from form_sender.config_validation import (
    ClientConfigValidationError,
    clear_config_cache,
    transform_client_config,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_config_cache()
    yield
    clear_config_cache()


def _minimal_config():
    return {
        "targeting_id": 101,
        "client_id": 501,
        "active": "true",
        "client": {
            "company_name": "テスト株式会社",
            "company_name_kana": "テストカブシキガイシャ",
            "form_sender_name": "営業太郎",
            "last_name": "営業",
            "first_name": "太郎",
            "last_name_kana": "エイギョウ",
            "first_name_kana": "タロウ",
            "last_name_hiragana": "えいぎょう",
            "first_name_hiragana": "たろう",
            "position": "マネージャー",
            "gender": "male",
            "email_1": "example@example.com",
            "email_2": "example+2@example.com",
            "postal_code_1": "123",
            "postal_code_2": "4567",
            "address_1": "東京都",
            "address_2": "千代田区",
            "address_3": "丸の内",
            "address_4": "1-1-1",
            "phone_1": "03",
            "phone_2": "1234",
            "phone_3": "5678",
            "department": "営業部",
            "website_url": "https://example.com",
            "address_5": "ビル10F",
        },
        "targeting": {
            "id": 101,
            "subject": "お問い合わせ",
            "message": "よろしくお願いいたします。",
            "targeting_sql": "select * from companies",
            "ng_companies": "",
            "max_daily_sends": 50,
            "send_start_time": "09:00",
            "send_end_time": "18:00",
            "send_days_of_week": [0, 1, 2, 3, 4],
        },
    }


def test_transform_client_config_normalizes_and_caches():
    config = _minimal_config()
    transformed = transform_client_config(config)
    assert transformed["active"] is True
    # cache returns same object instance for identical payload
    transformed_again = transform_client_config(copy.deepcopy(config))
    assert transformed_again is transformed


def test_transform_client_config_normalizes_active_flag():
    config = _minimal_config()
    config["active"] = "False"
    transformed = transform_client_config(config)
    assert transformed["active"] is False


def test_transform_client_config_missing_client_field_raises():
    config = _minimal_config()
    del config["client"]["company_name"]
    with pytest.raises(ClientConfigValidationError):
        transform_client_config(config)


def test_transform_client_config_allows_optional_blanks():
    config = _minimal_config()
    blanks = ["email_2", "postal_code_2", "address_4", "phone_2", "phone_3"]
    for key in blanks:
        config["client"][key] = ""
    transformed = transform_client_config(config)
    assert transformed["client"]["email_2"] == ""


def test_transform_client_config_invalid_time_format_raises():
    config = _minimal_config()
    config["targeting"]["send_start_time"] = "9AM"
    with pytest.raises(ClientConfigValidationError):
        transform_client_config(config)
