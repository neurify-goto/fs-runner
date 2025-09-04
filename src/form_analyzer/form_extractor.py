
import logging
import re
from typing import List, Dict, Any

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger(__name__)


class FormExtractor:
    """HTMLからのフォーム要素抽出ロジックをカプセル化するクラス"""

    def extract(self, html_content: str) -> List[Dict[str, Any]]:
        """シンプルなフォーム要素抽出 - 全<form>内をそのまま取得"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            forms = soup.find_all('form')
            
            logger.info(f"検出されたフォーム数: {len(forms)}")
            
            if not forms:
                logger.info("フォーム要素が見つかりません")
                return []
            
            # 全フォームの中身を抽出
            form_contents = []
            for i, form in enumerate(forms):
                try:
                    form_inner_html = ''.join(str(child) for child in form.children)
                    logger.debug(f"フォーム{i+1}の内容抽出完了: {len(form_inner_html)}文字")
                    form_contents.append(form_inner_html)
                except Exception as form_error:
                    logger.warning(f"フォーム{i+1}の抽出中にエラー: {form_error}")
                    continue
            
            if not form_contents:
                logger.warning("有効なフォーム内容が抽出できませんでした")
                return []
            
            # 全フォームを結合
            combined_form_content = '\n'.join(form_contents)
            logger.info(f"全フォーム結合完了: {len(combined_form_content)}文字 ({len(form_contents)}個のフォーム)")
            
            # PromptGeneratorが期待する形式で返す（中身はそのままのHTML）
            return [{'raw_form_content': combined_form_content}]
            
        except Exception as e:
            logger.error(f"フォーム要素抽出エラー: {e}")
            return []

