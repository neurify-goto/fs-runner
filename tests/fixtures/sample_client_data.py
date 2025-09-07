"""
公開可能な擬似クライアント・ターゲティングデータ

マッピング関連のテスト（例: tests/test_field_mapping_analyzer.py）で使用する。
実データや実在企業情報は含めない。
"""

from typing import Dict, Any

# Targeting（必要最小限）
TARGETING_DATA: Dict[str, Any] = {
    "id": 999001,
    "client_id": 999,
    "subject": "お問い合わせ（テスト）",
    "message": (
        "こちらはテスト送信ではありません。\n"
        "フォーム解析アルゴリズムの動作検証用メッセージです。\n"
        "実在の企業・個人情報は含まれていません。"
    ),
    "max_daily_sends": 10,
    "send_start_time": "00:00",
    "send_end_time": "23:59",
    "send_days_of_week": [0, 1, 2, 3, 4, 5, 6],
}


# Client（フォーム入力に使われうるフィールドをカバー）
CLIENT_DATA: Dict[str, Any] = {
    "id": 999,
    # 会社情報
    "company_name": "サンプル株式会社",
    "company_name_kana": "サンプルカブシキガイシャ",
    "website_url": "https://example.com",
    "department": "営業部",

    # 担当者情報
    "form_sender_name": "山田太郎",
    "last_name": "山田",
    "first_name": "太郎",
    "last_name_kana": "ヤマダ",
    "first_name_kana": "タロウ",
    "last_name_hiragana": "やまだ",
    "first_name_hiragana": "たろう",
    "position": "営業担当",
    "gender": "男性",

    # 連絡先
    "email_1": "taro.yamada",
    "email_2": "example.com",
    "phone_1": "03",
    "phone_2": "1234",
    "phone_3": "5678",

    # 住所
    "postal_code_1": "100",
    "postal_code_2": "0001",
    "address_1": "東京都",
    "address_2": "千代田区",
    "address_3": "千代田",
    "address_4": "１ー１ー１",
    "address_5": "テストビル３階",
}


def create_test_client_config(company_id=None):
    """マッピング用の公開可能テスト設定（2シート構造）。

    company_id は無視（実データ参照禁止）。
    戻り値の構造は実行系と互換に保つ。
    """

    return {
        "client_id": CLIENT_DATA["id"],
        "active": True,
        "client": {
            # 会社情報
            "company_name": CLIENT_DATA["company_name"],
            "company_name_kana": CLIENT_DATA["company_name_kana"],
            "website_url": CLIENT_DATA["website_url"],
            "department": CLIENT_DATA.get("department", ""),

            # 担当者
            "form_sender_name": CLIENT_DATA["form_sender_name"],
            "last_name": CLIENT_DATA["last_name"],
            "first_name": CLIENT_DATA["first_name"],
            "last_name_kana": CLIENT_DATA["last_name_kana"],
            "first_name_kana": CLIENT_DATA["first_name_kana"],
            "last_name_hiragana": CLIENT_DATA["last_name_hiragana"],
            "first_name_hiragana": CLIENT_DATA["first_name_hiragana"],
            "position": CLIENT_DATA["position"],
            "gender": CLIENT_DATA["gender"],

            # 連絡先
            "email_1": CLIENT_DATA["email_1"],
            "email_2": CLIENT_DATA["email_2"],
            "phone_1": CLIENT_DATA["phone_1"],
            "phone_2": CLIENT_DATA["phone_2"],
            "phone_3": CLIENT_DATA["phone_3"],

            # 住所
            "postal_code_1": CLIENT_DATA["postal_code_1"],
            "postal_code_2": CLIENT_DATA["postal_code_2"],
            "address_1": CLIENT_DATA["address_1"],
            "address_2": CLIENT_DATA["address_2"],
            "address_3": CLIENT_DATA["address_3"],
            "address_4": CLIENT_DATA["address_4"],
            "address_5": CLIENT_DATA["address_5"],
        },
        "targeting": {
            "subject": TARGETING_DATA["subject"],
            "message": TARGETING_DATA["message"],
            "max_daily_sends": TARGETING_DATA["max_daily_sends"],
            "send_start_time": TARGETING_DATA["send_start_time"],
            "send_end_time": TARGETING_DATA["send_end_time"],
            "send_days_of_week": TARGETING_DATA["send_days_of_week"],
        },
    }

