#!/usr/bin/env python3
"""
GitHub Actions対応リンクスコアラー

既存の高度なLinkScorerクラスから必要な機能を抽出し、
GitHub Actions環境に最適化したバージョン。
"""

import logging
import re
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class LinkScorer:
    """リンクスコアリングクラス（GitHub Actions対応版）"""
    
    def __init__(self):
        """初期化"""
        self._score_cache = {}  # URL -> スコアのキャッシュ
        self.visited_urls = set()  # 訪問済みURLトラッカー
        
        # 設定ファイル由来の除外キーワード（採用/応募系など）
        try:
            from config.manager import get_form_finder_rules
            from form_sender.security.log_sanitizer import sanitize_for_log  # noqa: F401  # for logging only

            rules = get_form_finder_rules()
            link_exclusions = rules.get("link_exclusions", {}) if isinstance(rules, dict) else {}
            self.excluded_link_keywords: List[str] = [
                str(k) for k in link_exclusions.get(
                    "exclude_if_text_or_url_contains_any",
                    [
                        # 日本語
                        "採用", "求人", "応募", "募集", "エントリー", "新卒", "中途", "キャリア",
                        # 英語系
                        "recruit", "recruitment", "career", "careers", "job", "jobs", "employment",
                        # コメント系（強シグナルのみ）
                        "comment-form", "commentform", "wp-comment", "wp-comments",
                        "#comment", "#respond", "leave a reply", "post comment",
                        "コメントを送信", "コメント投稿", "コメントする",
                    ],
                )
                if k
            ]
            # 事前正規化（高速化）
            self._excluded_kw_norm: List[str] = [s.lower() for s in self.excluded_link_keywords if s]

            # 問い合わせ系ホワイトリスト（URLやテキストにrecruit等を含んでも許可）
            rec = rules.get("recruitment_only_exclusion", {}) if isinstance(rules, dict) else {}
            self.general_contact_whitelist_keywords: List[str] = [
                str(k) for k in rec.get(
                    "allow_if_general_contact_keywords_any",
                    ["お問い合わせ", "お問合せ", "問い合わせ", "contact", "inquiry", "ご相談", "連絡"],
                ) if k
            ]
            self._general_kw_norm: List[str] = [s.lower() for s in self.general_contact_whitelist_keywords if s]
            # コメント系境界パターン（部分一致の誤検出を防止: document など）
            try:
                comment_tokens_attr = [
                    "comment-form", "commentform", "wp-comment", "wp-comments",
                ]
                # 単語境界/非英数/アンダースコア/ハイフンを境界として許可
                self._comment_specific_patterns = [
                    re.compile(rf"(^|[\s\W_]){re.escape(tok)}($|[\s\W_])", re.IGNORECASE)
                    for tok in comment_tokens_attr
                ]
            except re.error:
                self._comment_specific_patterns = []
        except Exception:
            # フォールバック（安全側）
            self.excluded_link_keywords = [
                "採用", "求人", "応募", "募集", "エントリー", "recruit", "careers", "job",
                "comment-form", "commentform", "wp-comment", "wp-comments",
                "#comment", "#respond", "leave a reply", "post comment",
                "コメントを送信", "コメント投稿", "コメントする",
            ]
            self._excluded_kw_norm = [s.lower() for s in self.excluded_link_keywords]
            try:
                comment_tokens_attr = [
                    "comment-form", "commentform", "wp-comment", "wp-comments",
                ]
                self._comment_specific_patterns = [
                    re.compile(rf"(^|[\s\W_]){re.escape(tok)}($|[\s\W_])", re.IGNORECASE)
                    for tok in comment_tokens_attr
                ]
            except re.error:
                self._comment_specific_patterns = []
            self._general_kw_norm = [s.lower() for s in ["お問い合わせ", "お問合せ", "問い合わせ", "contact", "inquiry"]]
        
        # スコア設定値（ハードコード）
        self.score_premium_text = 300
        self.score_high_priority_text = 200
        self.score_medium_priority_text = 100
        self.score_low_priority_text = 50
        self.score_negative_text = -100
        self.score_positive_url = 150
        self.score_negative_file = -200
        self.score_negative_scheme = -500
        self.score_external_link = -50
        self.score_html_attribute = 100
        self.score_header_position = 50
        self.score_main_position = 20
        self.min_link_score = 100
        
        # テキスト内容によるスコア（4段階ランク分け）
        # 最優先級（300点）
        self.premium_text_keywords = [
            "お問い合わせ", "お問合せ", "御問合せ", "問い合わせ", "問合わせ", "お問い合せ",
            "問合せ", "お問合わせ", "toiawase", "といあわせ",
            "Contact", "Contact Us", "お問い合わせフォーム", "Contact Form",
            "ContactUs", "contact-us", "お問合せフォーム",
            "連絡", "ご連絡", "お問い合わせください", "Kontakt",
            "聯絡我們", "联系我们"
        ]
        
        # 高優先度（200点）
        self.high_priority_text_keywords = [
            "ご相談", "相談", "そうだん", "コンタクト", "ご依頼", "依頼", "いらい",
            "ご相談窓口", "お客様相談室", "Get in Touch", "Reach Out",
            "お客様窓口", "営業窓口", "商談", "打ち合わせ", "うちあわせ",
            "Consultation", "Consult", "コンサルタント", "コンサル",
            "ミーティング", "meeting", "面談", "デモ", "Demo", "デモンストレーション",
            "トライアル", "Trial", "無料体験", "商品相談", "サービス相談", "製品相談",
            "導入相談", "導入検討", "購入相談", "個別相談", "オンライン相談", "無料相談",
            "営業へのお問い合わせ", "Sales Contact"
        ]
        
        # 中優先度（100点）
        self.medium_priority_text_keywords = [
            "資料請求", "見積", "お見積り", "お見積もり", "見積もり", "みつもり",
            "Request", "Quote", "Estimate", "価格相談", "料金相談", "りょうきん",
            "お見積り依頼", "見積依頼", "価格問い合わせ", "料金問い合わせ",
            "資料ダウンロード", "パンフレット", "カタログ", "提案依頼", "RFP",
            "訪問依頼", "来社依頼", "詳細を聞く", "詳しく聞く",
            "価格のお問い合わせ", "料金のお問い合わせ", "Inquiry Form", "Business Inquiry"
        ]
        
        # 低優先度（50点）
        self.low_priority_text_keywords = [
            "サポート", "ヘルプ", "質問", "FAQ問い合わせ", "よくある質問", "しつもん",
            "Support", "Help", "Question", "Inquiry", "Inquiry Form",
            "お客様サポート", "技術サポート", "カスタマーサポート", "ユーザーサポート",
            "フィードバック", "Feedback", "ご意見", "ご要望", "いけん", "ようぼう",
            "アンケート", "Survey", "申し込み", "もうしこみ", "Apply", "Application"
        ]
        
        # 負のキーワード
        self.negative_text_keywords = [
            "採用", "求人", "応募", "Recruit", "Career", 
            "プライバシーポリシー", "Privacy Policy", "サイトマップ",
            "会員登録", "Registration", "Sign up", "メールマガジン", "メルマガ", "Newsletter",
            "ダウンロード", "Download", "ログイン", "Login", "Sign in",
            "パスワード", "Password"
        ]
        
        # URL パターン
        self.positive_url_patterns = [
            "/contact", "/inquiry", "/form", "/mail", "/support", "/ask", "/contact-us",
            "/toiawase", "/contact-form", "/contactus", "/contact_us",
            "/support", "/help", "/question", "/inquiry-form",
            "/consult", "/consultation", "/demo", "/trial", "/meeting", "/appointment",
            "/estimate", "/quote", "/business-inquiry", "/sales-contact"
        ]
        
        self.negative_url_extensions = [
            ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg",
            ".zip", ".rar", ".tar", ".gz", ".docx", ".xlsx", ".csv"
        ]
        
        self.negative_url_schemes = [
            "tel:", "mailto:", "javascript:", "#"
        ]
        
        # HTML属性キーワード
        self.positive_html_keywords = [
            "contact", "inquiry", "form", "gnav-contact", "btn-contact", "cta",
            "consultation", "consult", "demo", "trial", "estimate", "quote",
            "business-form", "inquiry-form"
        ]
        
        # ナビゲーション要素
        self.navigation_class_keywords = [
            "menu", "navigation", "nav", "navbar", "header", "footer", 
            "dropdown", "hamburger", "mobile-menu", "sidebar"
        ]
        self.navigation_id_keywords = [
            "menu", "navigation", "nav", "navbar", "header", "footer",
            "main-menu", "primary-menu", "top-menu", "contact-menu"
        ]
        
        # contact関連の親要素クラス名キーワード
        self.contact_parent_class_keywords = [
            "email", "mail", "contact", "inquiry", "toiawase", "btn-contact",
            "cta", "call-to-action", "contact-btn", "contact-button",
            "email-btn", "mail-btn", "inquiry-btn", "consultation", "demo", "trial",
            "business-inquiry", "sales-contact", "form-wrapper", "form-container",
            "modal-contact", "popup-contact"
        ]
        
        # DOM順序管理（本家準拠新機能）
        self.dom_index_counter = 0  # DOM順序カウンター

    def _is_excluded_link(self, link: Dict[str, Any]) -> bool:
        """設定ベースのNGキーワードに該当するリンクを除外。

        - 一般問い合わせ語が含まれる場合は許可（ホワイトリスト）
        - コメント系は境界性の高いトークン（#comment、comment-form など）のみに限定
        - 採用系は部分一致でも安全のため従来通り
        """
        try:
            text = (link.get("text") or "").lower()
            title = (link.get("title") or "").lower()
            aria = (link.get("ariaLabel") or "").lower()
            alt = (link.get("alt") or "").lower()
            href = (link.get("href") or "").lower()
            haystack_norm = " ".join([text, title, aria, alt, href])

            # 一般的な問い合わせキーワードが含まれていればホワイトリストで許可
            if any(k in haystack_norm for k in self._general_kw_norm):
                return False

            # コメント系の厳密判定
            from urllib.parse import urlparse
            parsed = urlparse(href)
            frag = (parsed.fragment or "").lower()
            path = (parsed.path or "").lower()

            # #comment / #respond などのフラグメント（厳密一致）
            if frag in {"comment", "comments", "respond"}:
                return True

            # /comment/ /comments/ などのパスセグメント（単語境界扱い）
            if re.search(r"(^|/)(comment|comments)(/|$)", path):
                return True

            # 属性風のトークン（境界考慮）
            if any(p.search(haystack_norm) for p in getattr(self, "_comment_specific_patterns", [])):
                return True

            # フレーズ系（安全性が高いので部分一致でOK）
            phrase_tokens = [
                "leave a reply", "post comment", "コメントを送信", "コメント投稿", "コメントする",
            ]
            if any(tok in haystack_norm for tok in phrase_tokens):
                return True

            # 採用/応募などは従来通り（部分一致）
            recruitment_tokens = [
                "採用", "求人", "応募", "募集", "エントリー", "recruit", "recruitment", "career", "careers", "job", "jobs", "employment"
            ]
            if any(tok in haystack_norm for tok in recruitment_tokens):
                return True

            return False
        except Exception:
            # 例外時は保守的に除外しない
            return False

    def score_links(self, links: List[Dict[str, Any]], base_url: str) -> List[Tuple[Dict[str, Any], int]]:
        """リンクリストをスコアリングして優先順位付き結果を返す"""
        scored_links = []
        cache_hits = 0
        
        logger.debug(f"リンクスコアリング開始: {len(links)}個のリンクを処理")
        
        for i, link in enumerate(links):
            href = link.get('href', '')
            text = link.get('text', '')
            
            # URL正規化してキャッシュをチェック
            cache_key = self._make_cache_key(link)
            if cache_key and cache_key in self._score_cache:
                score = self._score_cache[cache_key]
                cache_hits += 1
                logger.debug(f"キャッシュからスコア取得: [{i+1}] {score}点 - '{text}' -> {href}")
            else:
                score = self._calculate_link_score(link, base_url)
                if cache_key:
                    self._score_cache[cache_key] = score
            
            if score >= self.min_link_score:
                # DOM順序を保持するためインデックスを追加
                link_with_index = link.copy()
                link_with_index['_dom_index'] = i
                scored_links.append((link_with_index, score))
                logger.debug(f"高スコアリンク採用: [{i+1}] {score}点 - '{text}' -> {href}")
            else:
                logger.debug(f"低スコアでリンクを除外: [{i+1}] {score}点 - '{text}' -> {href}")
        
        # 安定ソート：スコア（降順）→DOM順序（昇順）
        scored_links.sort(key=lambda x: (-x[1], x[0].get('_dom_index', 0)))
        
        logger.debug(f"スコアリング完了: {len(scored_links)}/{len(links)}個のリンクが条件を満たす")
        if cache_hits > 0:
            logger.debug(f"キャッシュ効率: {cache_hits}/{len(links)}個のリンクがキャッシュヒット")
        
        return scored_links

    def _calculate_link_score(self, link: Dict[str, Any], base_url: str) -> int:
        """個別リンクのスコアを計算"""
        try:
            score = 0
            href = link.get('href', '')
            text = link.get('text', '').strip()
            alt_text = link.get('alt', '').strip()
            aria_label = link.get('ariaLabel', '').strip()
            title_text = link.get('title', '').strip()
            link_id = link.get('id', '').lower()
            link_class = link.get('className', '').lower()
            parent_tag = link.get('parentTag', '').lower()
            parent_class = link.get('parentClass', '').lower()
            
            # 基本的なURL検証
            if not href or not isinstance(href, str):
                return -1000
            
            clean_url = href.strip()
            
            # 各スコアコンポーネントを計算
            text_score = self._score_text_content(text)
            score += text_score
            
            alt_score = 0
            if alt_text:
                alt_score = self._score_text_content(alt_text)
                score += alt_score
            
            aria_score = 0
            if aria_label:
                aria_score = self._score_text_content(aria_label)
                score += aria_score
            
            title_score = 0
            if title_text:
                title_score = self._score_text_content(title_text)
                score += title_score
            
            url_score = self._score_url_pattern(clean_url, base_url)
            score += url_score
            
            attr_score = self._score_html_attributes(link_id, link_class)
            score += attr_score
            
            nav_score = self._score_navigation_elements(link_id, link_class)
            score += nav_score
            
            parent_score = self._score_parent_class_elements(parent_class)
            score += parent_score
            
            position_score = self._score_html_position(parent_tag)
            score += position_score
            
            logger.debug(f"リンクスコア詳細: {score}点 - '{text}'")
            logger.debug(f"  テキスト: {text_score}点, alt: {alt_score}点, aria: {aria_score}点, title: {title_score}点")
            logger.debug(f"  URL: {url_score}点, 属性: {attr_score}点, ナビ: {nav_score}点, 親: {parent_score}点, 位置: {position_score}点")
            
            return score
            
        except Exception as e:
            logger.error(f"リンクスコア計算エラー: {e}")
            return -1000

    def _score_text_content(self, text: str) -> int:
        """テキスト内容によるスコアリング（4段階ランク分け対応）"""
        if not text:
            return 0
        
        text_lower = text.lower()
        score = 0
        
        # 最優先級キーワード（300点）
        for keyword in self.premium_text_keywords:
            if keyword.lower() in text_lower:
                score += self.score_premium_text
                logger.debug(f"最優先級テキストマッチ: '{keyword}' in '{text}' (+{self.score_premium_text}点)")
                break
        
        # 高優先度キーワード（200点）- 最優先級でマッチしていない場合のみ
        if score == 0:
            for keyword in self.high_priority_text_keywords:
                if keyword.lower() in text_lower:
                    score += self.score_high_priority_text
                    logger.debug(f"高優先度テキストマッチ: '{keyword}' in '{text}' (+{self.score_high_priority_text}点)")
                    break
        
        # 中優先度キーワード（100点）- 上位でマッチしていない場合のみ
        if score == 0:
            for keyword in self.medium_priority_text_keywords:
                if keyword.lower() in text_lower:
                    score += self.score_medium_priority_text
                    logger.debug(f"中優先度テキストマッチ: '{keyword}' in '{text}' (+{self.score_medium_priority_text}点)")
                    break
        
        # 低優先度キーワード（50点）- 上位でマッチしていない場合のみ
        if score == 0:
            for keyword in self.low_priority_text_keywords:
                if keyword.lower() in text_lower:
                    score += self.score_low_priority_text
                    logger.debug(f"低優先度テキストマッチ: '{keyword}' in '{text}' (+{self.score_low_priority_text}点)")
                    break
        
        # 負のキーワード - 他のスコアに関係なく適用
        for keyword in self.negative_text_keywords:
            if keyword.lower() in text_lower:
                original_score = score
                score -= abs(self.score_negative_text)
                logger.debug(f"負テキストマッチ: '{keyword}' in '{text}' ({original_score} -> {score}点)")
                break
        
        return max(0, score)

    def _score_url_pattern(self, url: str, base_url: str) -> int:
        """URLパターンによるスコアリング"""
        if not url:
            return -500
        
        score = 0
        url_lower = url.lower()
        
        # 外部ドメインチェック
        if self._is_external_link_basic(url, base_url):
            score -= abs(self.score_external_link)
            logger.debug("外部リンク")
        
        # 正のURLパターン
        for pattern in self.positive_url_patterns:
            if pattern in url_lower:
                score += self.score_positive_url
                logger.debug(f"正URLパターンマッチ: '{pattern}'")
                break
        
        # 負のファイル拡張子
        for extension in self.negative_url_extensions:
            if url_lower.endswith(extension):
                score -= abs(self.score_negative_file)
                logger.debug(f"負ファイル拡張子: '{extension}'")
                break
        
        # 負のスキーム
        for scheme in self.negative_url_schemes:
            if url_lower.startswith(scheme):
                score -= abs(self.score_negative_scheme)
                logger.debug(f"負スキーム: '{scheme}'")
                break
        
        return score

    def _score_html_attributes(self, link_id: str, link_class: str) -> int:
        """HTML属性によるスコアリング"""
        score = 0
        
        for keyword in self.positive_html_keywords:
            if keyword in link_id or keyword in link_class:
                score += self.score_html_attribute
                logger.debug(f"正HTML属性マッチ: '{keyword}' in id='{link_id}' class='{link_class}'")
                break
        
        return score

    def _score_navigation_elements(self, link_id: str, link_class: str) -> int:
        """ナビゲーション要素による追加スコアリング"""
        score = 0
        
        # ID属性チェック
        for keyword in self.navigation_id_keywords:
            if keyword in link_id:
                score += 100
                logger.debug(f"ナビゲーションID発見: '{keyword}' in id='{link_id}'")
                break
        
        # クラス属性チェック
        for keyword in self.navigation_class_keywords:
            if keyword in link_class:
                score += 80
                logger.debug(f"ナビゲーションクラス発見: '{keyword}' in class='{link_class}'")
                break
        
        return score

    def _score_parent_class_elements(self, parent_class: str) -> int:
        """親要素のクラス名による追加スコアリング"""
        score = 0
        
        for keyword in self.contact_parent_class_keywords:
            if keyword in parent_class:
                score += 150
                logger.debug(f"contact関連親クラス発見: '{keyword}' in class='{parent_class}' (+150点)")
                break
        
        return score

    def _score_html_position(self, parent_tag: str) -> int:
        """HTML上の場所によるスコアリング"""
        score = 0
        
        if parent_tag in ['header', 'footer', 'nav']:
            score += self.score_header_position
            logger.debug(f"ヘッダー系要素内: {parent_tag}")
        elif parent_tag in ['main']:
            score += self.score_main_position
            logger.debug(f"メイン要素内: {parent_tag}")
        
        return score

    def filter_valid_links(self, links: List[Dict[str, Any]], base_url: str) -> List[Dict[str, Any]]:
        """有効なリンクのみをフィルタリング"""
        valid_links = []
        
        for link in links:
            href = link.get('href', '')
            
            # 基本的なURL検証のみ
            if href and isinstance(href, str) and len(href.strip()) > 0:
                # 採用/応募系などの除外リンクをスキップ
                if self._is_excluded_link(link):
                    try:
                        from form_sender.security.log_sanitizer import sanitize_for_log

                        logger.debug(
                            f"除外リンク（採用/応募系）: text={sanitize_for_log(str(link.get('text', '')))} href={sanitize_for_log(href)}"
                        )
                    except Exception:
                        logger.debug("除外リンク（採用/応募系）をスキップ")
                    continue

                # 既に訪問済みかチェック（正規化して比較）
                normalized_href = self._normalize_url_for_cache(href)
                if normalized_href and normalized_href not in self.visited_urls:
                    link['href'] = href.strip()
                    valid_links.append(link)
                else:
                    logger.debug("既に訪問済みのURLをスキップ")
        
        logger.debug(f"{len(valid_links)}/{len(links)} 個のリンクが有効")
        return valid_links

    def _is_external_link_basic(self, url: str, base_url: str) -> bool:
        """基本的な外部リンクチェック"""
        try:
            url_domain = urlparse(url).netloc.lower()
            base_domain = urlparse(base_url).netloc.lower()

            # www の有無を無視
            url_clean = url_domain.replace('www.', '')
            base_clean = base_domain.replace('www.', '')

            # 同一ドメイン or サブドメインは内部扱い
            if not url_clean or not base_clean:
                return False
            if url_clean == base_clean:
                return False
            if url_clean.endswith('.' + base_clean):
                return False

            return True
        except Exception:
            return False

    def _normalize_url_for_cache(self, url: str) -> str:
        """キャッシュ用のURL正規化"""
        if not url:
            return ""
        
        # 末尾スラッシュの統一
        normalized = url.rstrip('/')
        
        # フラグメント（#以降）の除去
        if '#' in normalized:
            normalized = normalized.split('#')[0]
        
        # クエリパラメータの除去
        if '?' in normalized:
            normalized = normalized.split('?')[0]
        
        return normalized.lower()

    def _make_cache_key(self, link: Dict[str, Any]) -> str:
        """リンクキャッシュキー生成（URLだけでなくテキスト/属性も考慮）"""
        try:
            url_key = self._normalize_url_for_cache(link.get('href', ''))
            if not url_key:
                return ''
            parts = [
                url_key,
                (link.get('text') or '').strip().lower(),
                (link.get('title') or '').strip().lower(),
                (link.get('ariaLabel') or '').strip().lower(),
                (link.get('alt') or '').strip().lower(),
                (link.get('className') or '').strip().lower(),
                (link.get('id') or '').strip().lower(),
                (link.get('parentClass') or '').strip().lower(),
            ]
            return '|'.join(parts)
        except Exception:
            return url_key
    
    def clear_cache(self):
        """スコアキャッシュをクリア（新しいサイト探索時に使用）"""
        self._score_cache.clear()
        self.visited_urls.clear()
        logger.debug("リンクスコアラーキャッシュをクリアしました")
    
    def mark_url_visited(self, url: str):
        """URLを訪問済みとしてマーク"""
        normalized_url = self._normalize_url_for_cache(url)
        if normalized_url:
            self.visited_urls.add(normalized_url)
    
    def filter_and_score_links(self, raw_links: List[Dict[str, Any]], base_url: str, min_score: int = None) -> List[tuple]:
        """リンクのフィルタリングとスコアリングを一括処理
        
        Args:
            raw_links: 生のリンクリスト
            base_url: ベースURL
            min_score: 最小スコア閾値
            
        Returns:
            List[(link_data, score)]: スコア付きリンクのリスト（スコア降順）
        """
        min_score = min_score or self.min_link_score
        
        # 1. 有効リンクをフィルタ
        valid_links = self.filter_valid_links(raw_links, base_url)
        
        if not valid_links:
            return []
        
        # 2. スコアリング実行
        scored_links = self.score_links(valid_links, base_url)
        
        # 3. 最小スコア以上のリンクのみ抽出
        filtered_links = [(link, score) for link, score in scored_links if score >= min_score]
        
        logger.debug(f"リンク処理結果: {len(raw_links)}→{len(valid_links)}→{len(filtered_links)} (閾値:{min_score}点)")
        
        return filtered_links

    def add_visited_url(self, url: str):
        """訪問済みURLを追加"""
        normalized_url = self._normalize_url_for_cache(url)
        if normalized_url:
            self.visited_urls.add(normalized_url)

    def is_visited(self, url: str) -> bool:
        """URLが訪問済みかチェック"""
        normalized_url = self._normalize_url_for_cache(url)
        return normalized_url in self.visited_urls
