from src.form_sender.analyzer.field_patterns import FieldPatterns


def test_prefecture_exclude_patterns_are_comprehensive():
    fp = FieldPatterns()
    p = fp.get_pattern('都道府県')
    excludes = set(p.get('exclude_patterns', []))
    # 住所構成要素のうち都道府県以外が除外に含まれていること
    expected = {
        'address', 'addr', 'street', 'building', 'apartment', 'room', '号室',
        'address1', 'address_1', 'address2', 'address_2', 'address3', 'address_3',
        'address4', 'address_4', 'address5', 'address_5',
        'city', 'ward', '区', '市', '町', '村', '丁目', '番地',
    }
    assert expected.issubset(excludes)

