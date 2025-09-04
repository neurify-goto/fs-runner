#!/usr/bin/env python3
"""
ElementScorer._contains_token_with_boundary の日本語対応に関する回帰テスト

pytest 依存を避けるため unittest で実装。
"""

import unittest
import sys
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root))

from src.form_sender.analyzer.element_scorer import ElementScorer


class TestBoundaryDetectionJa(unittest.TestCase):
    def setUp(self):
        self.scorer = ElementScorer()

    def t(self, text: str, token: str) -> bool:
        return self.scorer._contains_token_with_boundary(text, token)

    def test_japanese_compounds_should_match(self):
        self.assertTrue(self.t("ご担当者氏名", "氏名"))
        self.assertTrue(self.t("お問い合わせ者のお名前", "名前"))
        self.assertTrue(self.t("保護者の氏名", "氏名"))

    def test_fullwidth_boundaries(self):
        # 全角括弧やスペースを境界とみなす
        self.assertTrue(self.t("お名前（必須）", "名前"))
        self.assertTrue(self.t("氏名　入力欄", "氏名"))  # 全角スペース

    def test_ascii_boundaries(self):
        self.assertTrue(self.t("名前 入力", "名前"))
        self.assertTrue(self.t("氏名_input", "氏名"))

    def test_unsafe_partial_tokens(self):
        # 『名』は部分一致を許容しない（マンション名など）
        self.assertFalse(self.t("マンション名", "名"))
        self.assertFalse(self.t("施設名", "名"))

    def test_safe_single_char_last_name(self):
        # 『姓』は『姓名』を拾えるように例外的に許容
        self.assertTrue(self.t("姓名", "姓"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
