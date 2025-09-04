
import logging
import re
from typing import List, Tuple

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger(__name__)


class ProhibitionDetector:
    """営業禁止文言の検出ロジックをカプセル化するクラス"""

    def __init__(self):
        """営業禁止検出器の初期化"""
        self.EXCLUSION_PATTERNS = [
            "営業日", "営業時間", "営業所", "営業部", "営業課", "営業担当", "営業マン", "営業員", "営業職",
            "営業実績", "営業成績", "営業利益", "営業収益", "営業報告", "営業会議", "営業戦略", "営業方針",
            "営業ノウハウ", "営業スキル", "営業力", "営業中", "営業再開", "営業停止", "営業休止",
            "営業開始", "営業終了", "営業年数", "営業経験", "営業歴", "営業拠点", "営業店舗", "営業エリア",
            "営業地域", "営業範囲", "営業区域", "営業車", "営業車両", "営業用", "営業向け", "営業秘密",
            "営業機密", "営業情報", "営業データ", "営業資料", "営業ツール", "営業支援", "営業システム",
            "営業管理", "営業統計", "営業分析", "営業指標", "営業目標", "営業計画", "営業予算", "営業費用",
            "営業コスト", "営業効率", "営業生産性", "営業品質", "営業サービス", "営業対応", "営業窓口",
            "営業チーム", "営業組織", "営業体制", "営業強化", "営業拡大", "営業促進", "営業推進",
            "営業改善", "営業革新", "営業改革", "営業最適化", "営業効果", "営業結果", "営業成果",
            "営業業績", "営業実態", "営業状況", "営業環境", "営業市場", "営業競争", "営業優位",
            "営業価値", "営業価格", "営業単価", "営業金額", "営業売上", "営業収入", "営業損益",
            "営業黒字", "営業赤字", "営業キャッシュフロー",

            # 詐欺防止・セキュリティ関連（正当な注意喚起）
            "なりすまし", "詐欺", "偽サイト", "フィッシング", "悪質", "不審", "偽装", "模倣",
            "違法", "不正", "注意喚起", "警戒", "被害", "トラブル", "セキュリティ",

            # サービス案内・顧客対応関連（正当なサービス説明）
            "お客様", "カスタマー", "サポート", "ヘルプ", "サービス", "お問い合わせ窓口",
            "相談窓口", "受付窓口", "案内", "説明", "ガイド", "マニュアル", "手順", "方法",
            "利用方法", "使用方法", "操作方法", "設定方法",

            # プライバシー・法務関連（正当な規約・方針）
            "個人情報", "プライバシー", "プライバシーポリシー", "個人情報保護", "データ保護",
            "利用規約", "サービス利用規約", "約款", "規約", "方針", "ポリシー", "ガイドライン",
            "法的", "法律", "法令", "規則", "条例", "コンプライアンス",

            # 通常業務・運営関連（正当な業務説明）
            "運営", "管理", "システム", "メンテナンス", "更新", "改善", "品質", "向上",
            "サービス向上", "利便性", "機能", "特徴", "メリット", "効果", "実績"
        ]

        self.PROHIBITION_KEYWORDS = [
            # === 営業目的系 ===
            "営業目的", "営業を目的", "営業による", "営業のため", "営業に関する",
            "営業活動", "営業行為", "営業案内", "営業電話", "営業メール", "営業連絡", "営業訪問",
            # === セールス系 ===
            "セールス目的", "セールスを目的", "セールスのため", "セールスに関する",
            "セールス活動", "セールス行為", "セールス案内", "セールス電話", "セールスメール",
            "セールス連絡", "セールス訪問",
            # === 販売系 ===
            "販売目的", "販売を目的", "販売のため", "販売に関する",
            "販売活動", "販売行為",
            # === 勧誘系 ===
            "勧誘目的", "勧誘を目的", "勧誘による", "勧誘のため", "勧誘に関する",
            "勧誘活動", "勧誘行為", "勧誘案内", "勧誘電話", "勧誘メール", "勧誘連絡",
            # === 宣伝・広告系 ===
            "宣伝目的", "宣伝を目的", "宣伝のための", "宣伝に関する",
            "宣伝活動", "宣伝行為", "広告目的", "広告宣伝", "PR目的", "プロモーション目的",
            # === 売り込み系 ===
            "売り込み", "売込",
            # === 商業・ビジネス系 ===
            "商業目的", "商業利用", "商業的利用", "ビジネス目的", "ビジネス利用", "営利目的", "営利利用",

            # === 迷惑行為系 ===
            "迷惑行為", "迷惑電話", "スパム", "spam", "SPAM",
        ]

        self.compiled_patterns = self._build_prohibition_patterns()
        self._pattern_cache = {}

    def _build_prohibition_patterns(self):
        """営業禁止パターンを構築"""
        exclusion_patterns = self._get_exclusion_patterns()

        SALES_KEYWORDS = "営業|セールス|勧誘|販売"
        CONTACT_KEYWORDS = "問い合わせ|お問い合わせ|連絡|ご連絡|メール|電話|訪問"
        PROHIBITION_KEYWORDS = "お断り|断り|遠慮|禁止"
        DECLINE_KEYWORDS = "できません|いたしかねます|しておりません|お受けしておりません"

        patterns = []

        sales_with_exclusion = f"営業(?!{exclusion_patterns})"

        patterns.extend([
            f"{sales_with_exclusion}.*?(?:{CONTACT_KEYWORDS}).*?(?:{PROHIBITION_KEYWORDS})",
            f"(?:{PROHIBITION_KEYWORDS}).*?{sales_with_exclusion}.*?(?:{CONTACT_KEYWORDS})",
            f"セールス.*?(?:{CONTACT_KEYWORDS}).*?(?:{PROHIBITION_KEYWORDS})",
            f"(?:{PROHIBITION_KEYWORDS}).*?セールス.*?(?:{CONTACT_KEYWORDS})",
            f"勧誘.*?(?:{CONTACT_KEYWORDS}).*?(?:{PROHIBITION_KEYWORDS})",
            f"(?:{PROHIBITION_KEYWORDS}).*?勧誘.*?(?:{CONTACT_KEYWORDS})",
            f"販売.*?(?:{CONTACT_KEYWORDS}).*?(?:{PROHIBITION_KEYWORDS})",
            f"(?:{PROHIBITION_KEYWORDS}).*?販売.*?(?:{CONTACT_KEYWORDS})",
        ])

        patterns.extend([
            f"売り?込み.*?(?:{PROHIBITION_KEYWORDS})",
            f"(?:{PROHIBITION_KEYWORDS}).*?売り?込み",
        ])

        patterns.extend([
            f"{sales_with_exclusion}活動.*?(?:お受け|対応).*?(?:{DECLINE_KEYWORDS})",
            f"セールス.*?(?:お受け|対応).*?(?:{DECLINE_KEYWORDS})",
            f"勧誘.*?(?:お受け|対応).*?(?:{DECLINE_KEYWORDS})",
            f"販売.*?(?:お受け|対応).*?(?:{DECLINE_KEYWORDS})",
        ])

        sales_keywords_with_exclusion = f"(?:{sales_with_exclusion}|セールス|勧誘|販売)"
        patterns.extend([
            f"{sales_keywords_with_exclusion}(?:電話|メール|連絡).*?(?:{PROHIBITION_KEYWORDS}|お受けしておりません)",
            f"(?:{PROHIBITION_KEYWORDS}|お受けしておりません).*?{sales_keywords_with_exclusion}(?:電話|メール|連絡)",
        ])

        commercial_keywords = f"(?:{sales_with_exclusion}|セールス|勧誘|販売|商業|営利)"
        patterns.extend([
            f"{commercial_keywords}.*?目的.*?(?:{PROHIBITION_KEYWORDS}|お受けしておりません)",
            f"(?:{PROHIBITION_KEYWORDS}|お受けしておりません).*?{commercial_keywords}.*?目的",
        ])

        patterns.extend([
            f"迷惑.*?(?:電話|連絡|行為).*?(?:{PROHIBITION_KEYWORDS})",
            f"(?:{PROHIBITION_KEYWORDS}).*?迷惑.*?(?:電話|連絡|行為)",
        ])

        compiled_patterns = []
        for pattern in patterns:
            try:
                compiled_patterns.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"正規表現パターンのコンパイルに失敗: {pattern} - {e}")

        return compiled_patterns

    def _get_exclusion_patterns(self) -> str:
        """「営業」の除外パターンを返す"""
        time_related = "日|時間|中|再開|停止|休止|開始|終了|年数|経験|歴"
        org_related = "所|部|課|担当|マン|員|職|窓口|チーム|組織|体制"
        location_related = "拠点|店舗|エリア|地域|範囲|区域"
        asset_related = "車|車両|用|向け"
        data_related = "秘密|機密|情報|データ|資料|ツール|支援|システム|管理|統計|分析"
        metrics_related = "実績|成績|利益|収益|報告|会議|指標|目標|計画|予算|費用|コスト|効率|生産性|品質"
        strategy_related = "戦略|方針|ノウハウ|スキル|力|サービス|対応|強化|拡大|促進|推進|改善|革新|改革|最適化"
        result_related = "効果|結果|成果|業績|実態|状況|環境|市場|競争|優位"
        financial_related = "価値|価格|単価|金額|売上|収入|損益|黒字|赤字|キャッシュフロー"

        return f"{time_related}|{org_related}|{location_related}|{asset_related}|{data_related}|{metrics_related}|{strategy_related}|{result_related}|{financial_related}"

    def detect(self, html_content: str) -> Tuple[bool, List[str]]:
        """営業禁止文言の検出"""
        if not html_content:
            return False, []

        try:
            detected_texts = self._detect_context_texts(html_content)
            return len(detected_texts) > 0, detected_texts
        except Exception as e:
            logger.error(f"HTML解析エラー: {e}")
            return False, []

    def _detect_context_texts(self, html_content: str) -> List[str]:
        """営業禁止文言を含む文脈テキストを抽出"""
        if not html_content:
            return []

        cleaned_text = self._clean_html_content_for_text_extraction(html_content)
        sentences = self._split_into_sentences(cleaned_text)
        prohibition_texts = set()

        logger.debug(f"文章に分割: {len(sentences)}個の文章を処理")

        for i, sentence in enumerate(sentences):
            sentence = sentence.strip()
            if len(sentence) < 10:
                continue

            logger.debug(f"文章{i}: '{sentence[:100]}...'")

            for keyword in self.PROHIBITION_KEYWORDS:
                if keyword in sentence:
                    logger.debug(f"キーワード '{keyword}' を検出")
                    if not self._should_exclude_keyword(sentence, keyword):
                        logger.info(f"営業禁止文言検出（キーワード）: '{keyword}' in '{sentence[:50]}...'")
                        prohibition_texts.add(sentence)
                        break
                    else:
                        logger.debug(f"除外パターンにより除外: '{keyword}'")
            
            for pattern in self.compiled_patterns:
                match = pattern.search(sentence)
                if match:
                    matched_text = match.group(0)
                    logger.debug(f"パターンマッチ: '{matched_text}'")
                    if not self._should_exclude_pattern(sentence, matched_text):
                        logger.info(f"営業禁止文言検出（パターン）: '{matched_text}' in '{sentence[:50]}...'")
                        prohibition_texts.add(sentence)
                        break
                    else:
                        logger.debug(f"除外パターンにより除外: '{matched_text}'")

        return self._filter_prohibition_texts(list(prohibition_texts))

    def _clean_html_content_for_text_extraction(self, html_content: str) -> str:
        """テキスト抽出用のHTMLクリーニング"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            for tag_name in ['script', 'style', 'noscript']:
                for tag in soup.find_all(tag_name):
                    tag.decompose()
            for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
                comment.extract()
            text_content = soup.get_text(separator=' ', strip=True)
            return re.sub(r'\s+', ' ', text_content)
        except Exception as e:
            logger.warning(f"HTMLクリーニングエラー: {e} - 元のHTMLを返します")
            return html_content

    def _split_into_sentences(self, text: str) -> List[str]:
        """テキストを文章に分割"""
        if not text:
            return []
        sentence_delimiters = r'[。！？\n\r]+'
        sentences = re.split(sentence_delimiters, text)
        return [s.strip() for s in sentences if len(s.strip()) >= 10]

    def _filter_prohibition_texts(self, texts: List[str]) -> List[str]:
        """営業禁止文言テキストを品質でフィルタリング"""
        if not texts:
            return []
        filtered = [text for text in texts if self._is_high_quality_prohibition_text(text)]
        filtered.sort(key=len, reverse=True)
        return self._remove_duplicate_texts(filtered)

    def _is_high_quality_prohibition_text(self, text: str) -> bool:
        """営業禁止文言テキストの品質をチェック"""
        if not text or len(text) < 5 or len(text) > 500:
            return False
        meaningless_patterns = [r'^[\s\d\-_=+*#@\[\]\(\)]+$', r'^[a-zA-Z\s]+$', r'^\d+$']
        if any(re.match(p, text) for p in meaningless_patterns):
            return False
        has_keyword = any(keyword in text for keyword in self.PROHIBITION_KEYWORDS)
        if not has_keyword:
            has_keyword = any(pattern.search(text) for pattern in self.compiled_patterns)
        return has_keyword

    def _remove_duplicate_texts(self, texts: List[str]) -> List[str]:
        """重複や包含関係のあるテキストを除去"""
        if not texts:
            return []
        unique_texts = []
        for current_text in texts:
            is_duplicate = False
            for existing_text in unique_texts:
                if current_text in existing_text or existing_text in current_text:
                    is_duplicate = True
                    break
                if self._calculate_text_similarity(current_text, existing_text) > 0.8:
                    is_duplicate = True
                    break
            if not is_duplicate:
                unique_texts.append(current_text)
        return unique_texts

    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """2つのテキスト間の類似度を計算"""
        if not text1 or not text2:
            return 0.0
        set1, set2 = set(text1), set(text2)
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        return intersection / union if union > 0 else 0.0

    def _should_exclude_keyword(self, text: str, keyword: str) -> bool:
        """キーワードが除外パターンに該当するかチェック"""
        if "営業" not in keyword:
            return False
        for exclusion in self.EXCLUSION_PATTERNS:
            if exclusion in text:
                if not self._has_other_prohibition_keywords(text, exclusion):
                    return True
        return False

    def _should_exclude_pattern(self, text: str, matched_pattern: str) -> bool:
        """正規表現パターンが除外対象かチェック"""
        if "営業" not in matched_pattern:
            return False
        for exclusion in self.EXCLUSION_PATTERNS:
            if exclusion in text:
                if not self._has_other_prohibition_keywords(text, exclusion):
                    return True
        return False

    def _has_other_prohibition_keywords(self, text: str, exclusion_keyword: str) -> bool:
        """除外キーワード以外に営業禁止文言が含まれているかチェック"""
        if not text:
            return False
        temp_text = text.replace(exclusion_keyword, "")
        if any(keyword in temp_text for keyword in self.PROHIBITION_KEYWORDS):
            return True
        if any(pattern.search(temp_text) for pattern in self.compiled_patterns):
            return True
        return False
