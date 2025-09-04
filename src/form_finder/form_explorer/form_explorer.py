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
from config.manager import get_form_explorer_config

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
            
            # フルスクロール実行（動的コンテンツ読み込み促進）
            await self._perform_full_scroll(page)
            
            # フォーム検出前の最終確認待機（JavaScript実行環境の安定化）
            await page.wait_for_timeout(500)
            
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
            
            if not initial_links:
                logger.debug(f"Company[{record_id}]: 統合探索終了 - 有効リンクなし")
                return None, 0
            
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
            form_url, pages_visited = await self._execute_unified_exploration(
                context, page_data, initial_links
            )
            
            if form_url:
                logger.info(f"Company[{record_id}]: 統合探索成功")
                return form_url, pages_visited
            
            logger.debug(f"Company[{record_id}]: 統合探索完了 - {pages_visited}ページ探索, フォーム未発見")
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
                await link_page.goto(link_url, wait_until='networkidle', timeout=self.config.PAGE_LOAD_TIMEOUT)
                
                # 追加の安定化待機（JavaScript実行完了の確保）
                await link_page.wait_for_load_state('domcontentloaded')
                await link_page.wait_for_load_state('networkidle')
                await link_page.wait_for_timeout(1000)
                
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
                    const links = [];
                    document.querySelectorAll('a[href]').forEach((link, index) => {
                        const href = link.href;
                        const text = link.textContent ? link.textContent.trim() : '';
                        if (href && text && href.startsWith('http')) {
                            links.push({
                                href: href,
                                text: text,
                                dom_index: index
                            });
                        }
                    });
                    return links;
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