from src.form_sender.analyzer.split_field_detector import (
    SplitFieldDetector, SplitFieldGroup, SplitPattern
)


def _client_data():
    return {
        'client': {
            'phone_1': '03', 'phone_2': '1234', 'phone_3': '5678',
            'postal_code_1': '123', 'postal_code_2': '4567',
            'address_1': '東京都', 'address_2': '渋谷区', 'address_3': '渋谷', 'address_4': '1-2-3', 'address_5': '渋谷ビル501',
        }
    }


def test_single_phone_value_combined():
    d = SplitFieldDetector()
    g = SplitFieldGroup(
        pattern=SplitPattern.PHONE_3_SPLIT,
        field_type='phone',
        fields=[{'field_name': '電話番号'}],
        confidence=1.0,
        sequence_valid=True,
        description='',
        input_strategy='combine',
        strategy_confidence=1.0,
        strategy_reason='',
    )
    a = d.generate_field_assignments([g], _client_data())
    assert a['電話番号'] == '0312345678'


def test_single_postal_value_combined():
    d = SplitFieldDetector()
    g = SplitFieldGroup(
        pattern=SplitPattern.POSTAL_2_SPLIT,
        field_type='postal_code',
        fields=[{'field_name': '郵便番号'}],
        confidence=1.0,
        sequence_valid=True,
        description='',
        input_strategy='combine',
        strategy_confidence=1.0,
        strategy_reason='',
    )
    a = d.generate_field_assignments([g], _client_data())
    assert a['郵便番号'] == '1234567'


def test_single_address_value_combined():
    d = SplitFieldDetector()
    g = SplitFieldGroup(
        pattern=SplitPattern.ADDRESS_2_SPLIT,
        field_type='address',
        fields=[{'field_name': '住所'}],
        confidence=1.0,
        sequence_valid=True,
        description='',
        input_strategy='combine',
        strategy_confidence=1.0,
        strategy_reason='',
    )
    a = d.generate_field_assignments([g], _client_data())
    assert a['住所'].startswith('東京都渋谷区渋谷1-2-3')

