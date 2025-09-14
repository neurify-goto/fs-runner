#!/usr/bin/env python3
"""
3ステップフォーム探索エンジン（統合スコアリング版）

GitHub Actions環境向けに最適化された高度なフォーム探索システム。
form-sales-fumaのFormExplorerアルゴリズムに完全準拠した統合スコアリング方式を実装。
"""

import asyncio
import logging
import time
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from playwright.async_api import Page, Browser
from dataclasses import dataclass

from .form_detector import FormDetector
from .link_scorer import LinkScorer
from ..utils import is_valid_form_url, get_robust_page_url
from config.manager import get_form_explorer_config
from config.manager import get_form_finder_rules

logger = logging.getLogger(__name__)


@dataclass
class ExplorationContext:
    """探索コンテキスト情報"""
    browser: Browser
    start_time: float
    max_pages: int
    timeout: int
    min_score: int
    base_url: str
    record_id: int


@dataclass
class ExplorationResult:
    """探索結果情報"""
    form_url: Optional[str]
    pages_visited: int
    additional_links: Optional[List[Tuple[Dict[str, Any], int]]] = None


class FormExplorerConfig:
    """フォーム探索設定（設定ファイル対応）"""
    
    def __init__(self):
        """設定ファイルから設定を読み込み"""
        try:
            config = get_form_explorer_config()
            self.MAX_PAGES_PER_SITE = config["max_pages_per_site"]
            self.SITE_TIMEOUT = config["site_timeout"]
            self.MIN_LINK_SCORE = config["min_link_score"]
            self.POPUP_WAIT = config["popup_wait"]
            self.PAGE_LOAD_TIMEOUT = config["page_load_timeout"]
            self.NETWORK_IDLE_TIMEOUT = config["network_idle_timeout"]
            self.MAX_PAGES_PER_DEPTH = config["max_pages_per_depth"]
            self.MAX_ADDITIONAL_LINKS = config["max_additional_links"]
            self.MAX_DEPTH1_COLLECTION = config["max_depth1_collection"]
            self.DEFAULT_DOM_INDEX = config["default_dom_index"]
        except Exception as e:
            logger.warning(f"フォーム探索設定の読み込みに失敗、デフォルト値を使用: {e}")
            # フォールバック用のデフォルト設定（本家準拠）
            self.MAX_PAGES_PER_SITE = 6
            self.SITE_TIMEOUT = 90
            self.MIN_LINK_SCORE = 100
            self.POPUP_WAIT = 500
            self.PAGE_LOAD_TIMEOUT = 30000
            self.NETWORK_IDLE_TIMEOUT = 3000
            self.MAX_PAGES_PER_DEPTH = 6
            self.MAX_ADDITIONAL_LINKS = 1000
            self.MAX_DEPTH1_COLLECTION = 6
            self.DEFAULT_DOM_INDEX = 999999


class FormExplorer:
    """GitHub Actions対応3ステップフォーム探索エンジン（統合スコアリング版）"""
    
    def __init__(self):
        """初期化"""
        self.form_detector = FormDetector()
        self.link_scorer = LinkScorer()
        self.config = FormExplorerConfig()
        self._initialized = False
        self._neg_keywords_cache: Optional[List[str]] = None
    
    async def initialize(self):
        """探索エンジンの初期化"""
        if self._initialized:
            return
        
        try:
            # 必要に応じて初期化処理を追加
            self._initialized = True
            logger.debug("FormExplorer初期化完了")
        except Exception as e:
            logger.error(f"FormExplorer初期化エラー: {e}")
            raise

    def _get_negative_keywords(self) -> List[str]:
        """リンク/テキストの負キーワード（設定駆動）を取得（小文字化・キャッシュ付）

        P2対策: 設定ファイルの読み込みが成功しても、link_exclusions が欠落/空配列の
        場合は安全側のデフォルトにフォールバックする。
        """
        if self._neg_keywords_cache is not None:
            return self._neg_keywords_cache
        try:
            rules = get_form_finder_rules()
            link_ex = rules.get("link_exclusions", {}) if isinstance(rules, dict) else {}
            kws = [
                str(k).lower() for k in link_ex.get("exclude_if_text_or_url_contains_any", []) if k
            ]
            # セーフティ: 既知のgenericは混入していない想定だが、入っていても無害化
            generic = {"comment", "comments", "/comment/", "/comments/", "コメント"}
            # 文字列・トリム・最小長・generic排除
            filtered_base = [k.strip() for k in kws if isinstance(k, str)]
            filtered = [k for k in filtered_base if k and len(k) >= 2 and k not in generic]

            # コメント系の危険な一般語をさらに除去（ホワイトリストのみ許可）
            allowed_comment_tokens = {
                '#comment', '#respond', 'leave a reply', 'post comment', 'comment-form', 'commentform',
                'wp-comment', 'wp-comments'
            }
            safe_filtered = []
            for k in filtered:
                if 'comment' in k and k not in allowed_comment_tokens:
                    # 誤除外を防ぐため除外（例: 'document', 'comments policy' など）
                    continue
                safe_filtered.append(k)
            filtered = safe_filtered

            # 取得できたキーワードが空なら、安全側のデフォルトにフォールバック
            if not filtered:
                filtered = [
                    '#comment', '#respond', 'leave a reply', 'post comment', 'comment-form', 'commentform',
                    '採用', '求人', 'recruit', 'careers', 'job', 'entry', 'エントリー'
                ]
            self._neg_keywords_cache = filtered
        except Exception:
            # フォールバック（安全側に限定的）
            self._neg_keywords_cache = [
                '#comment', '#respond', 'leave a reply', 'post comment', 'comment-form', 'commentform',
                '採用', '求人', 'recruit', 'careers', 'job', 'entry', 'エントリー'
            ]
        return self._neg_keywords_cache

    async def _dismiss_overlays(self, page: Page):
        """ページ表示時のオーバーレイ/同意バナー/モーダルをできる範囲で閉じる。

        - 代表的なCookie/同意/ポリシー/ニュースレター系を対象
        - 失敗は無視（安全側）
        """
        try:
            # 既に実施済みならスキップ（ページ単位フラグ）
            try:
                already = await page.evaluate("window.__overlayDismissed === true")
                if already:
                    return
            except Exception:
                pass
            # 代表的なボタンテキスト/ラベル
            btn_texts = [
                '同意', '同意する', '許可', '承諾', 'OK', 'Ok', 'ok', 'はい', '閉じる', '閉じる', '閉', 'Dismiss',
                'Accept', 'I Agree', 'Agree', 'Allow', 'Continue', 'Got it', 'Close'
            ]
            # 代表的なセレクタ（Cookie/Consent/Modal）
            candidates = [
                '[id*="cookie" i]', '[class*="cookie" i]', '[id*="consent" i]', '[class*="consent" i]',
                '[id*="gdpr" i]', '[class*="gdpr" i]', '[id*="policy" i]', '[class*="policy" i]',
                '[role="dialog"]', '.modal', '.overlay', '.backdrop', '.cookie-consent'
            ]

            # ボタンテキストでクリック
            click_timeout = getattr(self.config, 'POPUP_WAIT', 500)
            for t in btn_texts:
                try:
                    loc = page.get_by_role('button', name=t)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=click_timeout)
                except Exception:
                    pass
                try:
                    loc2 = page.get_by_text(t, exact=False)
                    if await loc2.count() > 0:
                        await loc2.first.click(timeout=click_timeout)
                except Exception:
                    pass

            # 代表セレクタ内の閉じる/同意ボタン
            for sel in candidates:
                try:
                    box = page.locator(sel)
                    if await box.count() == 0:
                        continue
                    # 近傍の閉じる/同意ボタンをクリック
                    btn = box.locator('button, [role="button"], .close, .btn, .accept, .agree, .ok')
                    if await btn.count() > 0:
                        await btn.first.click(timeout=click_timeout)
                except Exception:
                    pass

            # Escで閉じれるモーダル対策
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass
            # フラグ設定
            try:
                await page.evaluate("window.__overlayDismissed = true")
            except Exception:
                pass
        except Exception:
            pass

    async def _open_hamburger_if_present(self, page: Page):
        """ハンバーガーメニューを開いた上でリンクを再収集しやすくする。"""
        try:
            selectors = [
                '[aria-controls][aria-expanded="false"]',
                '.hamburger', '.hamburger-button', '.menu-toggle', '.navbar-toggle', '.nav-toggle',
                '.drawer-toggle', '.sp-menu', '.mobile-menu-button', '.global-nav-toggle'
            ]
            for sel in selectors:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=getattr(self.config, 'POPUP_WAIT', 500))
                        await page.wait_for_timeout(max(100, int(getattr(self.config, 'POPUP_WAIT', 500) * 0.4)))
                        break
                except Exception:
                    continue
        except Exception:
            pass
    
    
    async def explore_site_for_forms(
        self,
        browser: Browser,
        company_url: str,
        record_id: int,
        max_pages: int = None,
        timeout: int = None,
        min_score: int = None
    ) -> Tuple[Optional[str], int, Optional[int]]:
        """
        3ステップフォーム探索を実行
        
        Args:
            browser: Playwrightブラウザインスタンス
            company_url: 探索対象企業のURL
            record_id: 企業のレコードID
            max_pages: 最大探索ページ数
            timeout: タイムアウト（秒）
            min_score: リンクの最小スコア
            
        Returns:
            Tuple[form_url, pages_visited, success_step]:
            - form_url: 発見されたフォームURL（未発見時はNone）
            - pages_visited: 訪問したページ数
            - success_step: フォーム発見ステップ番号（未発見時はNone）
        """
        start_time = time.time()
        pages_visited = 0
        
        # 設定値の適用
        max_pages = max_pages or self.config.MAX_PAGES_PER_SITE
        timeout = timeout or self.config.SITE_TIMEOUT
        min_score = min_score or self.config.MIN_LINK_SCORE
        
        try:
            logger.info(f"Company[{record_id}]: 3ステップフォーム探索開始")
            
            # 新しいサイト探索のためキャッシュクリア
            self.link_scorer.clear_cache()
            
            # STEP 1: トップページ初期化・動的コンテンツ展開
            logger.debug(f"Company[{record_id}]: STEP1開始 - トップページ初期化")
            page_data = await self._execute_step1(browser, company_url, record_id)
            if not page_data:
                logger.warning(f"Company[{record_id}]: STEP1失敗 - トップページアクセス不可")
                return None, pages_visited, None
            
            pages_visited += 1
            logger.debug(f"Company[{record_id}]: STEP1完了")
            
            # STEP 2: トップページ内フォーム探索
            logger.debug(f"Company[{record_id}]: STEP2開始 - トップページフォーム探索")
            form_url = await self._execute_step2(page_data, company_url, record_id)
            
            if form_url:
                logger.info(f"Company[{record_id}]: STEP2成功 - フォーム発見")
                await self._cleanup_page_data(page_data)
                return form_url, pages_visited, 2
            
            logger.debug(f"Company[{record_id}]: STEP2完了 - トップページフォーム未発見")
            
            # STEP 3: 統合スコアリング方式によるリンク探索（本家準拠）
            logger.debug(f"Company[{record_id}]: STEP3開始 - 統合スコアリング探索")
            form_url, step3_pages = await self._execute_step3_integrated(
                browser, page_data, company_url, record_id, start_time, 
                pages_visited, max_pages, timeout, min_score
            )
            pages_visited += step3_pages
            
            if form_url:
                logger.info(f"Company[{record_id}]: STEP3成功 - フォーム発見")
                await self._cleanup_page_data(page_data)
                return form_url, pages_visited, 3
            
            logger.info(f"Company[{record_id}]: 3ステップ探索完了 - フォーム未発見 ({pages_visited}ページ探索)")
            await self._cleanup_page_data(page_data)
            return None, pages_visited, None
            
        except Exception as e:
            logger.error(f"Company[{record_id}]: フォーム探索エラー - {str(e)}")
            return None, pages_visited, None
    
    async def _execute_step1(self, browser: Browser, company_url: str, record_id: int) -> Optional[Dict[str, Any]]:
        """STEP1: トップページ初期化・動的コンテンツ展開"""
        try:
            # ブラウザコンテキスト作成
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            
            page = await context.new_page()
            
            # ページナビゲーション（タイミング最適化）
            await page.goto(
                company_url,
                wait_until='networkidle',
                timeout=self.config.PAGE_LOAD_TIMEOUT
            )
            
            # 追加の安定化待機（JavaScript実行完了の確保）
            await page.wait_for_load_state('domcontentloaded')
            await page.wait_for_load_state('networkidle')
            
            # 動的コンテンツ展開待機
            await page.wait_for_timeout(self.config.POPUP_WAIT)
            
            # オーバーレイ/同意バナーのクローズ試行
            await self._dismiss_overlays(page)

            # フルスクロール実行（動的コンテンツ読み込み促進）
            await self._perform_full_scroll(page)
            
            # モバイルナビを開いてリンク露出を促す
            await self._open_hamburger_if_present(page)

            # フォーム検出前の最終確認待機（JavaScript実行環境の安定化）
            await page.wait_for_timeout(self.config.POPUP_WAIT)
            
            # ページコンテンツを取得
            page_content = await self._get_page_content(page)
            
            page_data = {
                'context': context,
                'page': page,
                'content': page_content,
                'base_url': company_url,
                'final_url': page.url,
                'initialized': True
            }
            
            logger.debug(f"Company[{record_id}]: STEP1成功 - コンテンツ取得完了")
            return page_data
            
        except Exception as e:
            logger.error(f"Company[{record_id}]: STEP1エラー - {str(e)}")
            # コンテキストクリーンアップ
            if 'context' in locals():
                try:
                    await context.close()
                except Exception as close_error:
                    logger.warning(f"コンテキストクローズエラー: {close_error}")
            return None
    
    async def _execute_step2(self, page_data: Dict[str, Any], company_url: str, record_id: int) -> Optional[str]:
        """STEP2: トップページ内フォーム探索"""
        try:
            page = page_data['page']
            html_content = page_data['content']['html_content']
            
            # フォーム検出・検証実行
            forms = await self.form_detector.find_and_validate_forms(page, html_content)
            
            if forms and len(forms) > 0:
                # 最優先フォームを選択
                best_form = forms[0]
                form_url = best_form.get('form_url', '')
                
                # 堅牢なURL取得とフォールバック処理の改善
                from ..utils import is_valid_form_url, get_robust_page_url
                
                # 階層的なフォールバック処理
                if not is_valid_form_url(form_url):
                    logger.debug(f"Company[{record_id}]: STEP2 - 無効なform_url検出: {repr(form_url)}")
                    
                    # Playwright APIを使って堅牢にページURLを取得
                    try:
                        robust_url = await get_robust_page_url(page, company_url)
                        if robust_url:
                            form_url = robust_url
                            logger.debug(f"Company[{record_id}]: STEP2 - 堅牢URL取得成功: {robust_url[:50]}...")
                        else:
                            # 最終手段としてcompany_urlをフォールバック
                            form_url = company_url if is_valid_form_url(company_url) else None
                            if form_url:
                                logger.debug(f"Company[{record_id}]: STEP2 - company_URLフォールバック: {company_url[:50]}...")
                            else:
                                logger.warning(f"Company[{record_id}]: STEP2 - すべてのURL取得手法が失敗")
                    except Exception as e:
                        logger.warning(f"Company[{record_id}]: STEP2 - 堅牢URL取得エラー: {e}")
                        form_url = company_url if is_valid_form_url(company_url) else None
                
                if form_url and is_valid_form_url(form_url):
                    logger.debug(f"Company[{record_id}]: STEP2 - 高品質フォーム発見数: {len(forms)}個, URL: {form_url[:50]}...")
                    return form_url
                else:
                    logger.debug(f"Company[{record_id}]: STEP2 - 有効なform_URLが取得できませんでした")

            # 追試: 同一ページ内 #contact などのアンカーにクリック/スクロールして再検出
            try:
                jumped = await self._try_inpage_contact_jump(page)
                if jumped:
                    html2 = await page.content()
                    forms2 = await self.form_detector.find_and_validate_forms(page, html2)
                    if forms2 and len(forms2) > 0:
                        best_form = forms2[0]
                        form_url2 = best_form.get('form_url', '')
                        if not is_valid_form_url(form_url2):
                            robust2 = await get_robust_page_url(page, company_url)
                            form_url2 = robust2 if is_valid_form_url(robust2) else (company_url if is_valid_form_url(company_url) else None)
                        if form_url2 and is_valid_form_url(form_url2):
                            logger.debug(f"Company[{record_id}]: STEP2 - #contact ジャンプ後にフォーム検出: {form_url2[:50]}...")
                            return form_url2
            except Exception:
                pass

            logger.debug(f"Company[{record_id}]: STEP2 - トップページフォーム未発見")
            return None
            
        except Exception as e:
            logger.error(f"Company[{record_id}]: STEP2エラー - {str(e)}")
            return None
    
    async def _execute_step3_integrated(
        self,
        browser: Browser,
        page_data: Dict[str, Any],
        company_url: str,
        record_id: int,
        start_time: float,
        current_pages: int,
        max_pages: int,
        timeout: int,
        min_score: int
    ) -> Tuple[Optional[str], int]:
        """STEP3: 統合スコアリング方式によるリンク探索（深度無関係スコア優先）"""
        try:
            logger.debug(f"Company[{record_id}]: 統合スコアリング探索開始（深度無関係スコア優先）")
            logger.debug(f"Company[{record_id}]: 探索設定 - 最大{max_pages}ページ, 最小スコア{min_score}点")
            
            # 1. 初期リンクの準備とスコアリング
            initial_links, valid_links_count = self._prepare_initial_links(
                page_data, company_url, min_score, record_id
            )
            
            # 1.5 初期リンクが0件なら、ここでcontact系フォールバックを試す（早期returnしない）
            if not initial_links:
                logger.debug(f"Company[{record_id}]: 初期リンク0件 -> contact候補フォールバックを試行")
                fb_added = self._add_contact_candidates_fallback(
                    initial_links,
                    page_data['content'].get('links', []),
                    company_url,
                    min_score,
                )
                logger.debug(f"Company[{record_id}]: contact候補フォールバック 追加={fb_added}")
            
            logger.debug(f"Company[{record_id}]: 統合探索用初期リスト準備完了 - {len(initial_links)}個のリンク")
            
            # 探索コンテキストの作成
            context = ExplorationContext(
                browser=browser,
                start_time=start_time,
                max_pages=max_pages,
                timeout=timeout,
                min_score=min_score,
                base_url=company_url,
                record_id=record_id
            )
            
        # 2. 統合探索実行（深度無関係スコア優先）
            form_url, pages_visited = await self._execute_unified_exploration(context, page_data, initial_links)
            
            if form_url:
                logger.info(f"Company[{record_id}]: 統合探索成功")
                return form_url, pages_visited
            
            logger.debug(f"Company[{record_id}]: 統合探索完了 - {pages_visited}ページ探索, フォーム未発見")
            # 最終フォールバック: contact候補へ直接遷移してフォーム検出
            try:
                raw_links = page_data['content'].get('links', [])
                cand_url = None
                keywords = ['お問い合わせ', 'お問合せ', '問い合わせ', 'toiawase', 'contact', 'inquiry']
                neg_kw = self._get_negative_keywords()
                for link in raw_links:
                    hay = ' '.join([
                        str(link.get('text','')),
                        str(link.get('title','')),
                        str(link.get('ariaLabel','')),
                        str(link.get('alt','')),
                        str(link.get('href','')),
                    ]).lower()
                    if any(k.lower() in hay for k in keywords) and not any(k.lower() in hay for k in neg_kw):
                        cand_url = link.get('href')
                        break
                if cand_url:
                    logger.debug(f"Company[{record_id}]: 最終フォールバックで {cand_url} を直接検証")
                    link_page = await page_data['context'].new_page()
                    try:
                        await link_page.goto(cand_url, wait_until='domcontentloaded', timeout=self.config.PAGE_LOAD_TIMEOUT)
                        await link_page.wait_for_load_state('domcontentloaded')
                        await link_page.wait_for_timeout(500)
                        page_content2 = await self._get_page_content(link_page)
                        forms = await self.form_detector.find_and_validate_forms(link_page, page_content2['html_content'])
                        if forms:
                            from ..utils import is_valid_form_url, get_robust_page_url
                            form_url2 = forms[0].get('form_url')
                            if not form_url2 or not is_valid_form_url(form_url2):
                                robust = await get_robust_page_url(link_page, cand_url)
                                form_url2 = robust or (cand_url if is_valid_form_url(cand_url) else None)
                            if form_url2:
                                return form_url2, pages_visited + 1
                        # フォーム未発見でも候補URL自体を返す
                        return cand_url, pages_visited + 1
                    finally:
                        try:
                            await link_page.close()
                        except Exception:
                            pass
            except Exception:
                pass
            return None, pages_visited
            
        except Exception as e:
            logger.error(f"Company[{record_id}]: 統合探索エラー - {str(e)}")
            return None, 0
    
    def _prepare_initial_links(
        self, 
        page_data: Dict[str, Any], 
        base_url: str, 
        min_score: int,
        record_id: int
    ) -> Tuple[List[Tuple[Dict[str, Any], int]], int]:
        """トップページからのリンク準備とスコアリング（本家準拠）"""
        # トップページのリンクを取得してスコアリング
        page_content = page_data['content']
        top_page_links = page_content.get('links', [])
        
        if not top_page_links:
            logger.debug(f"Company[{record_id}]: トップページにリンクなし")
            return [], 0
        
        # 統合リンクリスト（トップページリンク）
        all_links: List[Tuple[Dict[str, Any], int]] = []
        
        # トップページのリンクをスコアリングして追加
        logger.debug(f"Company[{record_id}]: トップページリンク前処理 - {len(top_page_links)}個の生リンクを検出")
        valid_top_links = self.link_scorer.filter_valid_links(top_page_links, base_url)
        logger.debug(f"Company[{record_id}]: 有効リンクフィルタ結果 - {len(valid_top_links)}/{len(top_page_links)}個が有効")
        scored_top_links = self.link_scorer.score_links(valid_top_links, base_url)
        
        for link_data, score in scored_top_links:
            score_int = self._ensure_score_int(score)
            if score_int >= min_score:
                link_data['_source_depth'] = 0  # トップページ由来
                all_links.append((link_data, score_int))

        valid_top_links_count = len([l for l in scored_top_links if l[1] >= min_score])
        logger.debug(f"Company[{record_id}]: トップページリンク - {len(scored_top_links)}個中{valid_top_links_count}個が条件満たす（{min_score}点以上）")

        # フォールバック: contact/inquiry系キーワードを強制優先リンクとして追加
        if not all_links:
            keywords = ['お問い合わせ', 'お問合せ', '問い合わせ', 'toiawase', 'contact', 'inquiry']
            neg_kw = self._get_negative_keywords()
            fallback_added = 0
            for link in valid_top_links:
                # text/attrs/href すべてを対象に包含判定
                hay_raw = ' '.join([
                    str(link.get('text','')),
                    str(link.get('title','')),
                    str(link.get('ariaLabel','')),
                    str(link.get('alt','')),
                    str(link.get('href','')),
                ])
                hay = hay_raw.lower()
                has_pos = any(k.lower() in hay for k in keywords)
                has_neg = any(k.lower() in hay for k in neg_kw)
                if has_pos and not has_neg:
                    score_boost = max(min_score + 500, 600)
                    lcopy = link.copy()
                    lcopy['_source_depth'] = 0
                    all_links.append((lcopy, score_boost))
                    fallback_added += 1
            if fallback_added > 0:
                logger.debug(f"Company[{record_id}]: フォールバックでcontact系リンクを{fallback_added}件追加")
        
        # スコア優先ソート（深度に関係なく最もスコアの高いリンクから探索）
        all_links.sort(key=lambda x: (
            -x[1],  # プライマリ: スコア（降順）
            x[0].get('_dom_index', self.config.DEFAULT_DOM_INDEX)  # セカンダリ: DOM順序（昇順、未設定は最後）
        ))
        
        # 詳細ログ（トップ5リンク）
        if valid_top_links_count > 0:
            top_5_links = all_links[:5]
            logger.debug(f"Company[{record_id}]: トップページの高スコアリンク上位5件:")
            for i, (link_data, score) in enumerate(top_5_links, 1):
                href = link_data.get('href', '')[:50]
                text = link_data.get('text', '')[:20]
                logger.debug(f"  {i}. {score}点: {text} -> {href}...")
        
        return all_links, valid_top_links_count
    
    def _ensure_score_int(self, score: Any) -> int:
        """スコアを整数型として確実に取得するヘルパーメソッド"""
        return int(score) if not isinstance(score, int) else score
    
    async def _execute_unified_exploration(
        self,
        context: ExplorationContext,
        page_data: Dict[str, Any],
        initial_links: List[Tuple[Dict[str, Any], int]]
    ) -> Tuple[Optional[str], int]:
        """統合探索メイン処理（深度無関係スコア優先）"""
        pages_visited = 0
        
        # 未探索リンクリスト（常にスコア順でソートされる）
        pending_links: List[Tuple[Dict[str, Any], int]] = initial_links.copy()
        
        # 正規化済み訪問済みURLセット（重複回避用）
        visited_normalized_urls: set = set()
        
        # ブラウザコンテキスト
        context_obj = page_data['context']
        
        logger.debug(f"Company[{context.record_id}]: 統合探索開始 - {len(pending_links)}個の初期リンク")

        # 初期リンクが空ならフォールバックでcontact候補を投入
        if not pending_links:
            added_fb = self._add_contact_candidates_fallback(
                pending_links,
                page_data['content'].get('links', []),
                context.base_url,
                context.min_score,
            )
            if added_fb > 0:
                logger.debug(f"Company[{context.record_id}]: 初期リンク空のためフォールバックで{added_fb}件投入")
        
        while pending_links and pages_visited < context.max_pages:
            # 1. スコア順でソート（毎回実行）
            pending_links.sort(key=lambda x: (
                -x[1],  # プライマリ: スコア（降順）
                x[0].get('_dom_index', self.config.DEFAULT_DOM_INDEX)  # セカンダリ: DOM順序（昇順）
            ))
            
            # 2. 最高スコアのリンクを取得
            link_data, score = pending_links.pop(0)
            link_url = link_data.get('href')
            
            if not link_url:
                continue
            
            # 3. 正規化URL重複チェック
            normalized_url = self._get_normalized_url(link_url)
            if normalized_url in visited_normalized_urls:
                logger.debug(f"Company[{context.record_id}]: 正規化URL重複スキップ")
                continue
            
            # 4. 制限チェック
            should_continue, reason = self._should_continue_exploration(
                score, context.start_time, pages_visited, context.min_score, context.timeout, context.max_pages
            )
            
            if not should_continue:
                logger.debug(f"Company[{context.record_id}]: 統合探索停止 - {reason}")
                break
            
            # 5. ページ訪問とフォーム検索
            link_page = None
            try:
                logger.debug(f"Company[{context.record_id}]: 統合探索 - [{pages_visited + 1}/{context.max_pages}] (スコア: {score}点)")
                
                # ページ訪問（タイミング最適化）
                link_page = await context_obj.new_page()
                await link_page.goto(link_url, wait_until='domcontentloaded', timeout=self.config.PAGE_LOAD_TIMEOUT)

                # 追加の安定化待機（JavaScript実行完了の確保）
                await link_page.wait_for_load_state('domcontentloaded')
                await link_page.wait_for_load_state('networkidle')
                await link_page.wait_for_timeout(getattr(self.config, 'NETWORK_IDLE_TIMEOUT', 1000))
                
                # オーバーレイ/同意バナーのクローズ試行
                await self._dismiss_overlays(link_page)

                # フォーム検出前の最終確認待機
                await link_page.wait_for_timeout(300)
                
                # 訪問済みマーク
                visited_normalized_urls.add(normalized_url)
                self.link_scorer.mark_url_visited(link_url)
                pages_visited += 1
                
                # ページコンテンツ取得
                page_content = await self._get_page_content(link_page)
                
                # フォーム検索
                forms = await self.form_detector.find_and_validate_forms(
                    link_page, page_content['html_content']
                )
                
                if forms:
                    form_url = forms[0].get('form_url', '')
                    original_form_url = form_url
                    
                    # 堅牢なURL取得とフォールバック処理の改善
                    from ..utils import is_valid_form_url, get_robust_page_url
                    
                    if not form_url or not is_valid_form_url(form_url):
                        logger.debug(f"Company[{context.record_id}]: 無効なform_url検出: {repr(original_form_url)}")
                        
                        # 階層的なフォールバック処理
                        # 1. 訪問中のページから堅牢にURL取得
                        try:
                            robust_url = await get_robust_page_url(link_page, link_url)
                            if robust_url:
                                form_url = robust_url
                                logger.debug(f"Company[{context.record_id}]: 堅牢URL取得成功: {robust_url[:50]}...")
                            else:
                                # 2. link_urlをフォールバック
                                if is_valid_form_url(link_url):
                                    form_url = link_url
                                    logger.debug(f"Company[{context.record_id}]: link_URLフォールバック: {link_url[:50]}...")
                                else:
                                    form_url = None
                                    logger.debug(f"Company[{context.record_id}]: すべてのURL取得手法が失敗")
                        except Exception as e:
                            logger.warning(f"Company[{context.record_id}]: 堅牢URL取得エラー: {e}")
                            # エラー時はlink_urlをフォールバック
                            form_url = link_url if is_valid_form_url(link_url) else None
                    
                    # 有効なform_urlが取得できた場合のみ成功とする
                    if form_url and is_valid_form_url(form_url):
                        logger.info(f"Company[{context.record_id}]: 統合探索成功 - フォーム発見, URL: {form_url[:50]}...")
                        return form_url, pages_visited
                    else:
                        logger.debug(f"Company[{context.record_id}]: 最終的にも有効URL取得不可: {repr(form_url)}")
                
                # 6. 新規リンク収集と統合
                new_links = page_content.get('links', [])
                if new_links:
                    new_links_added = await self._merge_new_links_to_pending(
                        new_links, pending_links, visited_normalized_urls, 
                        context.base_url, context.min_score, context.record_id
                    )
                    
                    if new_links_added > 0:
                        logger.debug(f"Company[{context.record_id}]: 新規リンク {new_links_added}個を探索リストに統合")
                # 追加フォールバック: 依然としてpendingが空のときcontact候補を投入
                if not pending_links:
                    added_fb2 = self._add_contact_candidates_fallback(
                        pending_links, new_links, context.base_url, context.min_score
                    )
                    if added_fb2 > 0:
                        logger.debug(f"Company[{context.record_id}]: フォールバックで{added_fb2}件のcontact候補を投入")
                
            except Exception as link_error:
                logger.warning(f"Company[{context.record_id}]: ページ探索エラー（継続） - {str(link_error)[:100]}")
            finally:
                # セキュリティ: 確実なリソースクリーンアップ
                if link_page:
                    try:
                        await link_page.close()
                    except Exception as close_error:
                        logger.warning(f"リンクページクローズエラー: {close_error}")
        
        logger.debug(f"Company[{context.record_id}]: 統合探索完了 - {pages_visited}ページ探索、フォーム未発見")
        return None, pages_visited

    async def _try_inpage_contact_jump(self, page: Page) -> bool:
        """同一ページ内アンカー（#contact/#inquiry 等）に軽くジャンプ/クリックする。

        Returns: 成功してページ内移動/表示変化が起きたと推定できれば True
        """
        try:
            # キーワードは設定/既存規則から供給（なければ既定値）
            try:
                from config.manager import get_form_finder_rules
                rules = get_form_finder_rules()
                rec = rules.get('recruitment_only_exclusion', {}) if isinstance(rules, dict) else {}
                kw = rec.get('allow_if_general_contact_keywords_any', ['お問い合わせ','お問合せ','問い合わせ','contact','inquiry','ご相談','連絡'])
            except Exception:
                kw = ['お問い合わせ','お問合せ','問い合わせ','contact','inquiry','toiawase']

            return await page.evaluate("""
                (KEYS) => {
                    const keys = (KEYS || []).map(s => String(s || '').toLowerCase());
                    const isContactText = (s) => keys.some(k => (s||'').toLowerCase().includes(k));
                    const anchors = Array.from(document.querySelectorAll('a[href^="#"], [role="button"][href^="#"]'));
                    let tried = 0;
                    for (const a of anchors) {
                        const href = (a.getAttribute('href')||'').toLowerCase();
                        const txt = (a.textContent||'') + ' ' + (a.getAttribute('aria-label')||'') + ' ' + (a.getAttribute('title')||'');
                        if (!href) continue;
                        if (!isContactText(txt) && !isContactText(href)) continue;
                        tried++;
                        try { a.click(); } catch(e) { /* noop */ }
                        const id = href.replace('#','');
                        if (id) {
                            const target = document.getElementById(id);
                            if (target) {
                                target.scrollIntoView({behavior: 'instant', block: 'start'});
                            }
                        }
                        return true;
                    }
                    return false;
                }
            """, kw)
        except Exception:
            return False

    def _add_contact_candidates_fallback(
        self,
        pending_links: List[Tuple[Dict[str, Any], int]],
        raw_links: List[Dict[str, Any]],
        base_url: str,
        min_score: int,
    ) -> int:
        """contact/inquiry系の候補リンクを単純規則で抽出してpendingに投入"""
        if not raw_links:
            return 0
        try:
            keywords = ['お問い合わせ', 'お問合せ', '問い合わせ', 'toiawase', 'contact', 'inquiry']
            neg_kw = self._get_negative_keywords()

            added = 0
            for link in raw_links:
                hay = ' '.join([
                    str(link.get('text','')),
                    str(link.get('title','')),
                    str(link.get('ariaLabel','')),
                    str(link.get('alt','')),
                    str(link.get('href','')),
                ]).lower()
                if any(k.lower() in hay for k in keywords) and not any(k.lower() in hay for k in neg_kw):
                    href = link.get('href')
                    if not href:
                        continue
                    # 重複チェック（正規化）
                    normalized = self.link_scorer._normalize_url_for_cache(href)
                    if not normalized:
                        continue
                    if any(self.link_scorer._normalize_url_for_cache(l[0].get('href','')) == normalized for l in pending_links):
                        continue
                    score_boost = max(min_score + 500, 600)
                    lcopy = link.copy()
                    lcopy['_source_depth'] = 0
                    pending_links.append((lcopy, score_boost))
                    added += 1
            # スコア順に
            pending_links.sort(key=lambda x: (-x[1], x[0].get('_dom_index', self.config.DEFAULT_DOM_INDEX)))
            return added
        except Exception:
            return 0
    
    async def _merge_new_links_to_pending(
        self,
        new_links: List[Dict[str, Any]],
        pending_links: List[Tuple[Dict[str, Any], int]],
        visited_normalized_urls: set,
        base_url: str,
        min_score: int,
        record_id: int
    ) -> int:
        """新規リンクを探索リストに統合（重複除外）"""
        added_count = 0
        
        # 有効リンクをフィルタリング
        valid_links = self.link_scorer.filter_valid_links(new_links, base_url)
        
        if not valid_links:
            return 0
        
        # スコアリング実行
        scored_links = self.link_scorer.score_links(valid_links, base_url)
        
        # 既存のpending_linksの正規化URLセットを作成（重複チェック用）
        existing_pending_urls = set()
        for link_data, _ in pending_links:
            href = link_data.get('href')
            if href:
                normalized = self._get_normalized_url(href)
                if normalized:
                    existing_pending_urls.add(normalized)
        
        # 新規リンクを統合
        for link_data, score in scored_links:
            if score < min_score:
                continue
            
            href = link_data.get('href')
            if not href:
                continue
            
            normalized_url = self._get_normalized_url(href)
            if not normalized_url:
                continue
            
            # 重複チェック
            if normalized_url in visited_normalized_urls or normalized_url in existing_pending_urls:
                continue
            
            # 新規リンクを追加
            pending_links.append((link_data, score))
            existing_pending_urls.add(normalized_url)
            added_count += 1
        
        return added_count
    
    def _get_normalized_url(self, url: str) -> Optional[str]:
        """URL正規化（重複チェック用）"""
        if not url:
            return None
        
        return self.link_scorer._normalize_url_for_cache(url)
    
    def _should_continue_exploration(
        self, 
        score: int, 
        start_time: float, 
        total_pages: int,
        min_score: int,
        timeout: int,
        max_pages: int
    ) -> Tuple[bool, str]:
        """探索継続可能性判定（本家準拠）"""
        # スコア不足チェック
        if score < min_score:
            return False, f"スコア不足: {score}点 < {min_score}点"
        
        # タイムアウトチェック
        elapsed_time = time.time() - start_time
        if elapsed_time > timeout:
            return False, f"タイムアウト: {elapsed_time:.1f}秒/{timeout}秒"
        
        # タイムアウト近い警告（90%経過）
        if elapsed_time > timeout * 0.9:
            logger.warning(f"タイムアウト近い: 残り{timeout - elapsed_time:.1f}秒")
        
        # ページ数制限チェック
        if total_pages >= max_pages:
            return False, f"ページ上限: {total_pages}/{max_pages}ページ"
        
        return True, ""
    
    async def _perform_full_scroll(self, page: Page):
        """フルスクロール実行（動的コンテンツ読み込み促進）"""
        try:
            await page.evaluate("""
                async () => {
                    const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
                    const scrollStep = window.innerHeight / 2;
                    
                    for (let y = 0; y < document.body.scrollHeight; y += scrollStep) {
                        window.scrollTo(0, y);
                        await delay(100);
                    }
                    
                    window.scrollTo(0, 0);
                }
            """)
        except Exception as e:
            logger.debug(f"フルスクロールエラー: {e}")
    
    async def _get_page_content(self, page: Page) -> Dict[str, Any]:
        """ページコンテンツ取得"""
        try:
            # HTML取得
            html_content = await page.content()
            
            # リンク抽出
            links = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    
                    // 1) 標準アンカー
                    document.querySelectorAll('a[href]').forEach((link, index) => {
                        try {
                            const href = link.href || '';
                            if (!href || !(href.startsWith('http://') || href.startsWith('https://'))) return;
                            if (seen.has(href)) return;

                            const text = (link.textContent || '').trim();
                            const ariaLabel = link.getAttribute('aria-label') || '';
                            const title = link.getAttribute('title') || '';
                            let alt = '';
                            try {
                                const img = link.querySelector('img[alt]');
                                if (img && img.getAttribute('alt')) alt = img.getAttribute('alt');
                            } catch (_) {}

                            const structural = link.closest('header, footer, nav, main');
                            const parentTag = (structural ? structural.tagName : (link.parentElement ? link.parentElement.tagName : '')).toLowerCase();
                            const parentClass = (structural ? structural.className : (link.parentElement ? link.parentElement.className : '')) || '';

                            results.push({
                                href,
                                text,
                                alt,
                                ariaLabel,
                                title,
                                id: link.id || '',
                                className: link.className || '',
                                parentTag,
                                parentClass: String(parentClass).toLowerCase(),
                                _dom_index: index
                            });
                            seen.add(href);
                        } catch (_) {}
                    });

                    // 2) onclick で location.href や window.open を使う要素（a/button/role=button）
                    const clickables = Array.from(document.querySelectorAll('a[onclick], button[onclick], [role="button"][onclick]'));
                    clickables.forEach((el, idx) => {
                        try {
                            const onclick = el.getAttribute('onclick') || '';
                            if (!onclick) return;
                            const patterns = [
                                /location\.href\s*=\s*['\"]([^'\"]+)['\"]/i,
                                /window\.location\s*=\s*['\"]([^'\"]+)['\"]/i,
                                /window\.open\(\s*['\"]([^'\"]+)['\"]/i
                            ];
                            let target = '';
                            for (const re of patterns) {
                                const m = onclick.match(re);
                                if (m && m[1]) { target = m[1]; break; }
                            }
                            if (!target) return;

                            // 絶対URL化
                            let href = '';
                            try { href = new URL(target, document.baseURI).href; } catch(e) { href = ''; }
                            if (!href || !(href.startsWith('http://') || href.startsWith('https://'))) return;
                            if (seen.has(href)) return;

                            const text = (el.textContent || '').trim();
                            const ariaLabel = el.getAttribute('aria-label') || '';
                            const title = el.getAttribute('title') || '';
                            let alt = '';
                            try {
                                const img = el.querySelector('img[alt]');
                                if (img && img.getAttribute('alt')) alt = img.getAttribute('alt');
                            } catch (_) {}
                            const structural = el.closest('header, footer, nav, main');
                            const parentTag = (structural ? structural.tagName : (el.parentElement ? el.parentElement.tagName : '')).toLowerCase();
                            const parentClass = (structural ? structural.className : (el.parentElement ? el.parentElement.className : '')) || '';

                            results.push({
                                href,
                                text,
                                alt,
                                ariaLabel,
                                title,
                                id: el.id || '',
                                className: el.className || '',
                                parentTag,
                                parentClass: String(parentClass).toLowerCase(),
                                _dom_index: 100000 + idx // 後からでも安定
                            });
                            seen.add(href);
                        } catch(_) {}
                    });

                    // 3) data-* / SPAルータ属性に URL を持つクリック可能要素（contact系の拾い漏れ救済）
                    const dataCandidates = Array.from(document.querySelectorAll('[data-href], [data-url], [data-target], [data-route], [data-path], [routerlink], [to]'));
                    dataCandidates.forEach((el, idx) => {
                        try {
                            const cand = el.getAttribute('data-href') || el.getAttribute('data-url') || el.getAttribute('data-target') || el.getAttribute('data-route') || el.getAttribute('data-path') || el.getAttribute('routerlink') || el.getAttribute('to') || '';
                            if (!cand) return;
                            let href = '';
                            try { href = new URL(cand, document.baseURI).href; } catch(e) { href = ''; }
                            if (!href || !(href.startsWith('http://') || href.startsWith('https://'))) return;
                            if (seen.has(href)) return;

                            const text = (el.textContent || '').trim();
                            const ariaLabel = el.getAttribute('aria-label') || '';
                            const title = el.getAttribute('title') || '';
                            let alt = '';
                            try {
                                const img = el.querySelector('img[alt]');
                                if (img && img.getAttribute('alt')) alt = img.getAttribute('alt');
                            } catch (_) {}
                            const structural = el.closest('header, footer, nav, main');
                            const parentTag = (structural ? structural.tagName : (el.parentElement ? el.parentElement.tagName : '')).toLowerCase();
                            const parentClass = (structural ? structural.className : (el.parentElement ? el.parentElement.className : '')) || '';

                            results.push({
                                href,
                                text,
                                alt,
                                ariaLabel,
                                title,
                                id: el.id || '',
                                className: el.className || '',
                                parentTag,
                                parentClass: String(parentClass).toLowerCase(),
                                _dom_index: 200000 + idx
                            });
                            seen.add(href);
                        } catch(_) {}
                    });

                    return results;
                }
            """)
            
            return {
                'html_content': html_content,
                'links': links
            }
            
        except Exception as e:
            logger.error(f"ページコンテンツ取得エラー: {e}")
            return {
                'html_content': '',
                'links': []
            }
    
    async def _cleanup_page_data(self, page_data: Dict[str, Any]):
        """ページデータクリーンアップ"""
        try:
            if page_data and page_data.get('context'):
                await page_data['context'].close()
        except Exception as e:
            logger.debug(f"ページクリーンアップエラー: {e}")
