"""
営業禁止文言検出システム

フォームマッピングとは独立して、ページ全体から営業禁止文言を検出する機能
フォーム要素の境界制限を受けない独立したモジュール
"""

import asyncio
import logging
import re
from typing import Dict, List, Any, Optional
from playwright.async_api import Page, Locator
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProhibitionMatch:
    """営業禁止文言マッチ情報"""
    text: str                   # マッチした文言
    position: str              # 位置情報
    confidence: float          # 信頼度
    context: str               # 周辺文脈


class SalesProhibitionDetector:
    """営業禁止文言検出メインクラス"""
    
    def __init__(self, page_or_frame):
        """
        Args:
            page_or_frame: PlaywrightのPageまたはFrameオブジェクト
        """
        self.page = page_or_frame
        
        # 営業禁止文言パターン（既存の定義を継承・拡張）
        self.prohibition_patterns = {
            '直接的な営業禁止': [
                '営業のお電話はお断り', '営業電話お断り', '営業電話はお断り',
                '営業メールお断り', '営業活動はお断り', '営業目的でのご連絡はお断り',
                '営業・勧誘はお断り', '勧誘のお電話はお断り', '勧誘電話お断り',
                '売り込み電話お断り', '売り込みはお断り', 'セールス電話お断り',
                'テレアポお断り', '営業お断り', '勧誘お断り'
            ],
            
            '間接的な営業禁止': [
                '商品・サービスの売り込み', '商品の売り込み', 'サービスの売り込み',
                '宣伝目的での', '広告目的での', 'PR目的での',
                '商品のご紹介', 'サービスのご紹介', '商材の紹介',
                'ご提案のお電話', 'セールスのお電話', '営業のご連絡'
            ],
            
            '条件付き制限': [
                'お客様以外からのお問い合わせはご遠慮', '関係者以外のお問い合わせ',
                '同業者からのお問い合わせ', '競合他社からのお問い合わせ',
                'イタズラ目的でのお問い合わせ', 'いたずら目的でのお問い合わせ'
            ]
        }
        
        # 検出設定
        self.settings = {
            'search_elements': [
                'body', 'main', 'div', 'p', 'span', 'section', 'article',
                'form', 'fieldset', 'legend', 'label', 'small', 'em', 'strong'
            ],
            'max_text_length': 500,     # 1つのテキストの最大長
            'min_match_length': 5,      # 最小マッチ長
            'context_chars': 50,        # 前後の文脈文字数
            'confidence_threshold': 0.6 # 信頼度閾値
        }
        
        logger.info("SalesProhibitionDetector initialized")
    
    async def detect_prohibition_text(self) -> Dict[str, Any]:
        """
        ページ全体から営業禁止文言を検出
        
        Returns:
            Dict[str, Any]: 検出結果
            {
                'has_prohibition': bool,
                'matches': List[ProhibitionMatch],
                'prohibition_level': str,  # 'strict', 'moderate', 'mild', 'none'
                'summary': Dict[str, Any]
            }
        """
        logger.info("Starting sales prohibition text detection")
        
        try:
            # ページ全体のテキスト収集
            text_contents = await self._collect_page_text()
            
            # パターンマッチング実行
            matches = self._match_prohibition_patterns(text_contents)
            
            # 検出結果の評価
            result = self._evaluate_prohibition_level(matches)
            
            logger.info(f"Prohibition detection completed: {result['prohibition_level']} level with {len(matches)} matches")
            return result
            
        except Exception as e:
            logger.error(f"Error in prohibition text detection: {e}")
            return {
                'has_prohibition': False,
                'matches': [],
                'prohibition_level': 'none',
                'summary': {'error': str(e)}
            }
    
    async def _collect_page_text(self) -> List[Dict[str, Any]]:
        """ページ全体からテキストを収集"""
        text_contents = []
        
        try:
            for element_type in self.settings['search_elements']:
                elements = await self.page.locator(element_type).all()
                
                for element in elements[:50]:  # 要素数制限
                    try:
                        text = await element.text_content()
                        if not text or len(text.strip()) < self.settings['min_match_length']:
                            continue
                        
                        text = text.strip()[:self.settings['max_text_length']]
                        
                        # 要素の位置情報も取得（可能な場合）
                        try:
                            bounding_box = await element.bounding_box()
                            position_info = f"{element_type}_{bounding_box['x']:.0f}_{bounding_box['y']:.0f}" if bounding_box else element_type
                        except:
                            position_info = element_type
                        
                        text_contents.append({
                            'text': text,
                            'element_type': element_type,
                            'position': position_info
                        })
                        
                    except Exception as e:
                        logger.debug(f"Error extracting text from {element_type}: {e}")
                        continue
            
            logger.info(f"Collected text from {len(text_contents)} elements")
            return text_contents
            
        except Exception as e:
            logger.error(f"Error collecting page text: {e}")
            return []
    
    def _match_prohibition_patterns(self, text_contents: List[Dict[str, Any]]) -> List[ProhibitionMatch]:
        """営業禁止パターンのマッチング実行"""
        matches = []
        
        for text_data in text_contents:
            text = text_data['text'].lower()
            position = text_data['position']
            element_type = text_data['element_type']
            
            for category, patterns in self.prohibition_patterns.items():
                for pattern in patterns:
                    if pattern.lower() in text:
                        # 前後の文脈を取得
                        match_index = text.find(pattern.lower())
                        context_start = max(0, match_index - self.settings['context_chars'])
                        context_end = min(len(text), match_index + len(pattern) + self.settings['context_chars'])
                        context = text[context_start:context_end]
                        
                        # 信頼度計算
                        confidence = self._calculate_match_confidence(pattern, text, element_type, category)
                        
                        if confidence >= self.settings['confidence_threshold']:
                            matches.append(ProhibitionMatch(
                                text=pattern,
                                position=position,
                                confidence=confidence,
                                context=context
                            ))
        
        # 重複除去と信頼度でソート
        unique_matches = self._deduplicate_matches(matches)
        return sorted(unique_matches, key=lambda x: x.confidence, reverse=True)
    
    def _calculate_match_confidence(self, pattern: str, text: str, element_type: str, category: str) -> float:
        """マッチの信頼度を計算"""
        base_confidence = 0.5
        
        # カテゴリ別の基本信頼度
        category_weights = {
            '直接的な営業禁止': 0.9,
            '間接的な営業禁止': 0.7,
            '条件付き制限': 0.6
        }
        base_confidence = category_weights.get(category, 0.5)
        
        # 要素タイプによる重み付け
        element_weights = {
            'form': 1.0,      # フォーム内は最重要
            'p': 0.9,         # 段落
            'div': 0.8,       # 汎用コンテナ
            'span': 0.7,      # インライン
            'small': 0.9,     # 注意書きによく使用
            'em': 0.8,        # 強調
            'strong': 0.8     # 強調
        }
        element_weight = element_weights.get(element_type, 0.6)
        
        # パターンの完全性チェック
        pattern_match = 1.0 if pattern.lower() in text else 0.0
        
        # 周辺キーワードによる補正
        boost_keywords = ['お問い合わせ', '注意', '注意事項', 'ご注意', '禁止', 'お断り']
        boost = 0.1 * sum(1 for keyword in boost_keywords if keyword in text)
        
        final_confidence = min(1.0, base_confidence * element_weight * pattern_match + boost)
        return final_confidence
    
    def _deduplicate_matches(self, matches: List[ProhibitionMatch]) -> List[ProhibitionMatch]:
        """重複マッチを除去"""
        if not matches:
            return matches
        
        unique_matches = []
        seen_texts = set()
        
        for match in matches:
            # 類似テキストの重複除去（簡易版）
            text_key = match.text.lower().replace(' ', '').replace('　', '')
            if text_key not in seen_texts:
                seen_texts.add(text_key)
                unique_matches.append(match)
        
        return unique_matches
    
    def _evaluate_prohibition_level(self, matches: List[ProhibitionMatch]) -> Dict[str, Any]:
        """検出結果の評価とレベル判定"""
        if not matches:
            return {
                'has_prohibition': False,
                'matches': [],
                'prohibition_level': 'none',
                'summary': {
                    'total_matches': 0,
                    'max_confidence': 0.0,
                    'categories_found': []
                }
            }
        
        # 統計計算
        max_confidence = max(match.confidence for match in matches)
        avg_confidence = sum(match.confidence for match in matches) / len(matches)
        
        # カテゴリ分析
        categories_found = set()
        direct_prohibition_count = 0
        
        for match in matches:
            for category, patterns in self.prohibition_patterns.items():
                if match.text in patterns:
                    categories_found.add(category)
                    if category == '直接的な営業禁止':
                        direct_prohibition_count += 1
                    break
        
        # レベル判定
        if direct_prohibition_count >= 2 or max_confidence >= 0.9:
            prohibition_level = 'strict'
        elif direct_prohibition_count >= 1 or max_confidence >= 0.8:
            prohibition_level = 'moderate'
        elif len(matches) >= 2 or max_confidence >= 0.7:
            prohibition_level = 'mild'
        else:
            prohibition_level = 'weak'
        
        return {
            'has_prohibition': True,
            'matches': matches,
            'prohibition_level': prohibition_level,
            'summary': {
                'total_matches': len(matches),
                'max_confidence': max_confidence,
                'avg_confidence': avg_confidence,
                'direct_prohibition_count': direct_prohibition_count,
                'categories_found': list(categories_found)
            }
        }
    
    def get_prohibition_summary(self, detection_result: Dict[str, Any]) -> str:
        """営業禁止文言検出結果のサマリーを生成"""
        if not detection_result.get('has_prohibition', False):
            return "営業禁止文言は検出されませんでした"
        
        level = detection_result.get('prohibition_level', 'unknown')
        matches_count = len(detection_result.get('matches', []))
        
        level_descriptions = {
            'strict': '厳格な営業禁止',
            'moderate': '中程度の営業禁止',
            'mild': '軽度の営業禁止',
            'weak': '弱い営業禁止'
        }
        
        level_desc = level_descriptions.get(level, level)
        return f"{level_desc}文言が検出されました（{matches_count}件）"