"""
企業詳細情報抽出モジュール

HTMLから企業詳細情報を抽出する機能を提供
"""

import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional, Callable
from urllib.parse import urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class DetailExtractor:
    """企業詳細情報抽出クラス"""
    
    # 単位変換定数
    THOUSAND_TO_YEN_RATIO = 1000  # 千円 → 円
    HUNDRED_MILLION_TO_MILLION_RATIO = 100  # 億円 → 百万円
    THOUSAND_TO_MILLION_RATIO = 1000  # 千円 → 百万円

    def __init__(self):
        self._load_extraction_settings()
        self._compile_regex_patterns()

    def _load_extraction_settings(self):
        """抽出設定の読み込み"""
        # フィールドアイコンの設定
        self.field_icons = {
            "representative": "account_box",
            "capital": "work_outline",
            "employee_count": "group",
            "established": "create",
            "tel": "call",
            "company_url": "image_search",
            "closing_month": "done",
            "average_age": "share",
            "average_salary": "vertical_align_center",
        }

        # バリデーション設定
        self.validation_config = {
            "min_established_year": 1800,
            "max_month": 12,
            "min_month": 1,
            "legal_entity_number_length": 13,
            "postal_code_pattern": r"^\d{3}-\d{4}$",
        }

        # 正規化設定
        self.normalization_config = {
            "capital_units": ["百万円", "千円", "万円", "円", "億円"],
            "employee_count_suffix": "人",
            "invalid_values": ["-", "", "None", "null", "N/A"],
        }

        logger.info("抽出設定を初期化しました")

    def _compile_regex_patterns(self):
        """正規表現パターンの事前コンパイル"""
        self.regex_patterns = {
            "national_id": re.compile(r"法人番号:(\d+)"),
            "postal_code": re.compile(r"〒(\d{3}-\d{4})"),
            "established_date": re.compile(r"(\d{4})年(\d{1,2})月"),
            "established_year": re.compile(r"(\d{4})年"),
            "established_slash_date": re.compile(r"(\d{4})[/-](\d{1,2})"),
            "closing_month": re.compile(r"(\d+)月"),
            "age": re.compile(r"(\d+(?:\.\d+)?)歳"),
            "salary_thousand": re.compile(r"([0-9,]+)千円"),
            "salary_general": re.compile(r"([0-9,]+)"),
            "capital_million": re.compile(r"([0-9,]+)百万円"),
            "capital_hundred_million": re.compile(r"([0-9,]+(?:\.\d+)?)億円"),
            "capital_thousand": re.compile(r"([0-9,]+)千円"),
            "capital_general": re.compile(r"([0-9,]+(?:\.\d+)?)"),
            "employee_count": re.compile(r"(\d+(?:\.\d+)?)"),
        }
        logger.info("正規表現パターンをコンパイルしました")

    def extract_company_details(self, html_content: str, url: str) -> Dict[str, Any]:
        """HTMLから企業詳細情報を抽出"""
        soup = BeautifulSoup(html_content, "html.parser")

        detail_data = {
            "national_id": None,
            "postal_code": None,
            "company_url": None,
            "tel": None,
            "closing_month": None,
            "average_age": None,
            "average_salary": None,
            "representative": None,
            "capital": None,
            "employee_count": None,
            "established_year": None,
            "established_month": None,
        }

        try:
            # national_id: 法人番号から抽出
            national_id_element = soup.find("p", string=lambda text: text and "法人番号:" in text)
            if national_id_element:
                national_id_match = self.regex_patterns["national_id"].search(national_id_element.get_text())
                if national_id_match:
                    detail_data["national_id"] = national_id_match.group(1)

            # postal_code: 住所から抽出
            address_elements = soup.find_all(string=lambda text: text and "本店(登記)所在地：〒" in text)
            if address_elements:
                for address_text in address_elements:
                    postal_match = self.regex_patterns["postal_code"].search(address_text)
                    if postal_match:
                        detail_data["postal_code"] = postal_match.group(1)
                        break
            else:
                # 代替手段：全体HTMLから郵便番号パターンを検索
                full_text = soup.get_text()
                postal_match = self.regex_patterns["postal_code"].search(full_text)
                if postal_match:
                    detail_data["postal_code"] = postal_match.group(1)

            # tel: 代表電話
            detail_data["tel"] = self._extract_field_value(soup, self.field_icons["tel"], "代表電話")

            # company_url: 企業URL
            detail_data["company_url"] = self._extract_url_field(soup, self.field_icons["company_url"], "企業URL")

            # closing_month: 決算月
            detail_data["closing_month"] = self._extract_field_value(
                soup, self.field_icons["closing_month"], "決算月", self._process_closing_month
            )

            # average_age: 平均年齢
            detail_data["average_age"] = self._extract_field_value(
                soup, self.field_icons["average_age"], "平均年齢", self._process_average_age
            )

            # average_salary: 平均年収
            detail_data["average_salary"] = self._extract_field_value(
                soup, self.field_icons["average_salary"], "平均年収", self._process_average_salary
            )

            # representative: 代表者
            detail_data["representative"] = self._extract_field_value(soup, self.field_icons["representative"], "代表者")

            # capital: 資本金
            detail_data["capital"] = self._extract_field_value(
                soup, self.field_icons["capital"], "資本金", self._process_capital
            )

            # employee_count: 従業員数
            detail_data["employee_count"] = self._extract_field_value(
                soup, self.field_icons["employee_count"], "従業員数", self._process_employee_count
            )

            # established_year, established_month: 設立年月
            established_text = self._extract_field_value(soup, self.field_icons["established"], "設立年月日")
            if established_text:
                year, month = self._process_established_date(established_text)
                detail_data["established_year"] = year
                detail_data["established_month"] = month

            # データの検証とログ出力
            extracted_fields = [k for k, v in detail_data.items() if v is not None]
            logger.info(f"抽出成功フィールド: {extracted_fields}")

            return detail_data

        except Exception as e:
            logger.error(f"詳細情報抽出でエラー: {e}")
            return detail_data

    def _validate_established_date(self, year: int, month: int) -> bool:
        """設立年月日の検証"""
        current_year = datetime.now().year
        min_year = self.validation_config["min_established_year"]
        min_month = self.validation_config["min_month"]
        max_month = self.validation_config["max_month"]
        return min_year <= year <= current_year and min_month <= month <= max_month

    def _validate_established_year(self, year: int) -> bool:
        """設立年の検証"""
        current_year = datetime.now().year
        min_year = self.validation_config["min_established_year"]
        return min_year <= year <= current_year

    def _extract_field_value(self, soup: BeautifulSoup, icon_name: str, field_label: str, processor: Optional[Callable[[str], Any]] = None) -> Any:
        """汎用フィールド値抽出メソッド
        
        Args:
            soup: BeautifulSoupオブジェクト
            icon_name: Material Iconsのアイコン名
            field_label: フィールドのラベル名（除外用）
            processor: 値の後処理関数（オプション）
            
        Returns:
            抽出された値、または None
        """
        icon = soup.find("i", {"class": "material-icons"}, string=icon_name)
        if not icon:
            logger.debug(f"{field_label}のアイコンが見つからない")
            return None
        
        field_container = icon.find_parent("div", class_=lambda x: x and "md:w-[50%]" in (x or []))
        if not field_container:
            logger.debug(f"{field_label}のコンテナ要素が見つからない")
            return None
        
        all_p_elements = field_container.find_all("p")
        for p_elem in all_p_elements:
            # アイコン要素を含むpタグをスキップ（ラベル行）
            if p_elem.find("i", {"class": "material-icons"}):
                continue
            
            text = p_elem.get_text(strip=True)
            if text and text != field_label and text not in self.normalization_config["invalid_values"]:
                if processor:
                    return processor(text)
                return text
        
        logger.debug(f"{field_label}の値が空またはハイフン")
        return None

    def _extract_url_field(self, soup: BeautifulSoup, icon_name: str, field_label: str) -> Optional[str]:
        """URL専用フィールド抽出メソッド"""
        icon = soup.find("i", {"class": "material-icons"}, string=icon_name)
        if not icon:
            logger.debug(f"{field_label}のアイコンが見つからない")
            return None
        
        field_container = icon.find_parent("div", class_=lambda x: x and "md:w-[50%]" in (x or []))
        if not field_container:
            logger.debug(f"{field_label}のコンテナ要素が見つからない")
            return None
        
        url_element = field_container.find("a")
        if url_element and url_element.get("href"):
            href = url_element.get("href")
            if self._validate_url(href):
                return href
        
        logger.debug(f"{field_label}の有効なURLが見つからない")
        return None

    def _validate_url(self, url: str) -> bool:
        """URL検証の強化版"""
        try:
            if not url.startswith(("https://", "http://")):
                return False
            
            parsed = urlparse(url)
            return bool(parsed.scheme and parsed.netloc)
        except Exception as e:
            logger.warning(f"URL検証エラー: {e}")
            return False

    def _process_closing_month(self, text: str) -> Optional[int]:
        """決算月処理関数"""
        match = self.regex_patterns["closing_month"].search(text)
        if match:
            month_num = int(match.group(1))
            if 1 <= month_num <= 12:
                return month_num
        return None

    def _process_average_age(self, text: str) -> Optional[float]:
        """平均年齢処理関数"""
        match = self.regex_patterns["age"].search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
        return None

    def _process_average_salary(self, text: str) -> Optional[int]:
        """平均年収処理関数"""
        # 千円単位のパターンを先にチェック
        match = self.regex_patterns["salary_thousand"].search(text)
        if match:
            try:
                salary_thousands = int(match.group(1).replace(",", ""))
                return salary_thousands * self.THOUSAND_TO_YEN_RATIO
            except ValueError:
                pass
        
        # 通常の数値パターン
        match = self.regex_patterns["salary_general"].search(text)
        if match:
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:
                pass
        
        return None
    
    def _process_established_date(self, text: str) -> tuple[Optional[int], Optional[int]]:
        """設立年月の処理（年と月のタプルを返却）"""
        try:
            # 年月形式（「2006年01月」）
            year_month_match = self.regex_patterns["established_date"].search(text)
            if year_month_match:
                year = int(year_month_match.group(1))
                month = int(year_month_match.group(2))
                if self._validate_established_date(year, month):
                    return year, month
                else:
                    logger.warning(f"設立年月の値が範囲外: 年={year}, 月={month}")
            
            # 年のみ形式（「2006年」）
            year_only_match = self.regex_patterns["established_year"].search(text)
            if year_only_match:
                year = int(year_only_match.group(1))
                if self._validate_established_year(year):
                    return year, None
            
            # スラッシュ区切りやハイフン区切り（「2006/01」「2006-01」）
            slash_date_match = self.regex_patterns["established_slash_date"].search(text)
            if slash_date_match:
                year = int(slash_date_match.group(1))
                month = int(slash_date_match.group(2))
                if self._validate_established_date(year, month):
                    return year, month
        
        except (ValueError, AttributeError) as e:
            logger.warning(f"設立年月日の解析エラー: {text} - {e}")
        
        return None, None

    def _process_capital(self, text: str) -> Optional[int]:
        """資本金処理関数（百万円単位で返却）"""
        # 百万円単位のパターンを先にチェック
        if "百万円" in text:
            match = self.regex_patterns["capital_million"].search(text)
            if match:
                try:
                    amount_str = match.group(1).replace(",", "")
                    amount_int = int(amount_str)
                    logger.debug(f"資本金正規化成功（百万円単位）: {text} -> {amount_int}")
                    return amount_int
                except ValueError:
                    pass
        elif "億円" in text:
            match = self.regex_patterns["capital_hundred_million"].search(text)
            if match:
                try:
                    amount_str = match.group(1).replace(",", "")
                    amount_float = float(amount_str)
                    amount_int = round(amount_float * self.HUNDRED_MILLION_TO_MILLION_RATIO)
                    logger.debug(f"資本金正規化成功（億円→百万円）: {text} -> {amount_int}")
                    return amount_int
                except ValueError:
                    pass
        elif "千円" in text:
            match = self.regex_patterns["capital_thousand"].search(text)
            if match:
                try:
                    amount_str = match.group(1).replace(",", "")
                    amount_int = int(amount_str)
                    amount_millions = amount_int / self.THOUSAND_TO_MILLION_RATIO
                    if amount_millions >= 1:
                        amount_int = round(amount_millions)
                        logger.debug(f"資本金正規化成功（千円→百万円）: {text} -> {amount_int}")
                        return amount_int
                    else:
                        logger.debug(f"資本金が1百万円未満のため0に正規化: {text}")
                        return 0
                except ValueError:
                    pass
        else:
            # 単位が含まれない場合は通常の数値として処理
            match = self.regex_patterns["capital_general"].search(text)
            if match:
                try:
                    amount_str = match.group(1).replace(",", "")
                    if "." in amount_str:
                        amount_str = amount_str.split(".")[0]
                    amount_int = int(amount_str)
                    logger.debug(f"資本金正規化成功（単位なし）: {text} -> {amount_int}")
                    return amount_int
                except ValueError:
                    pass
        
        logger.debug(f"資本金パターン未一致、None返却: {text}")
        return None

    def _process_employee_count(self, text: str) -> Optional[int]:
        """従業員数処理関数"""
        normalized_text = text.replace(",", "")
        match = self.regex_patterns["employee_count"].search(normalized_text)
        if match:
            try:
                count_str = match.group(1)
                if "." in count_str:
                    count_str = count_str.split(".")[0]
                count_int = int(count_str)
                logger.debug(f"従業員数正規化成功: {text} -> {count_int}")
                return count_int
            except ValueError:
                pass
        
        logger.debug(f"従業員数パターン未一致、None返却: {text}")
        return None
