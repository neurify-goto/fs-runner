"""
Form Finder共通ユーティリティ関数群

フォーム探索処理で使用される共通機能を提供
"""

import logging
import re
import ipaddress
import unicodedata
from urllib.parse import urlparse
from typing import Optional

from form_sender.security.log_sanitizer import sanitize_for_log

logger = logging.getLogger(__name__)


def safe_log_info(record_id: str, message: str):
    """記録ID付きの安全なINFOログ出力"""
    logger.info(f"Company[{record_id}]: {message}")


def safe_log_error(record_id: str, message: str):
    """記録ID付きの安全なERRORログ出力"""
    logger.error(f"Company[{record_id}]: {message}")


def is_valid_form_url(url: str) -> bool:
    """
    form_URLの包括的妥当性検証（統一化版）
    
    全てのForm Finderコンポーネントで共通利用される
    form_URLの妥当性チェック関数
    
    Args:
        url: 検証するURL文字列
        
    Returns:
        bool: 妥当なform_URLの場合True、不正の場合False
    """
    if not url or not isinstance(url, str):
        return False
    
    url = url.strip()
    
    # 空文字列チェック
    if not url:
        return False
    
    # about:blank や about:srcdoc を除外
    if url.startswith('about:'):
        return False
    
    # httpまたはhttpsで始まらないURLを除外
    if not url.startswith(('http://', 'https://')):
        return False
    
    # 基本的な長さ制限
    if len(url) > 2048:
        return False
    
    # JavaScript URLを除外
    if url.startswith('javascript:'):
        return False
    
    # より厳密な妥当性チェック
    try:
        # 危険な文字をチェック
        dangerous_chars = ['<', '>', '"', '\'', '\n', '\r', '\t', '\x00']
        if any(char in url for char in dangerous_chars):
            return False
        
        # Unicodeドメイン攻撃対策
        try:
            normalized_url = unicodedata.normalize('NFC', url)
            if normalized_url != url:
                logger.warning(f"Unicode normalization detected in form_url: {sanitize_for_log(url)}")
                return False
        except Exception:
            return False
        
        # urllib.parseを使った包括的検証
        parsed = urlparse(url)
        
        # スキームチェック
        if not parsed.scheme or parsed.scheme not in ['http', 'https']:
            return False
        
        # ホスト名チェック
        if not parsed.netloc or len(parsed.netloc) < 3:
            return False
        
        # ポート分離
        hostname = parsed.netloc.split(':')[0]
        port = parsed.port
        
        # 不正ポート検証
        if port is not None:
            if not (1 <= port <= 65535) or port in [22, 23, 25, 110, 143, 993, 995]:  # 危険ポート除外
                return False
        
        # IP アドレス検証（完全版）
        try:
            ip = ipaddress.ip_address(hostname)
            # プライベートIP範囲の完全チェック
            if (ip.is_private or ip.is_loopback or ip.is_multicast or 
                ip.is_reserved or ip.is_unspecified or ip.is_link_local):
                return False
            # 特別なIP範囲
            if isinstance(ip, ipaddress.IPv4Address):
                # CGN範囲 (100.64.0.0/10), TEST-NET等
                if (ipaddress.IPv4Address('100.64.0.0') <= ip <= ipaddress.IPv4Address('100.127.255.255') or
                    ipaddress.IPv4Address('192.0.2.0') <= ip <= ipaddress.IPv4Address('192.0.2.255') or
                    ipaddress.IPv4Address('198.51.100.0') <= ip <= ipaddress.IPv4Address('198.51.100.255') or
                    ipaddress.IPv4Address('203.0.113.0') <= ip <= ipaddress.IPv4Address('203.0.113.255')):
                    return False
        except ValueError:
            # ドメイン名の場合
            # ローカルホスト等のチェック
            if hostname.lower() in ['localhost', 'local', '0.0.0.0', '[::]']:
                return False
            
            # ドメイン名の形式チェック（RFC準拠）
            if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$', hostname):
                return False
            
            # TLD検証
            if '.' not in hostname or len(hostname.split('.')[-1]) < 2:
                return False
        
        # パス・クエリパラメータの基本検証
        if parsed.path and len(parsed.path) > 1000:  # 長すぎるパス
            return False
        if parsed.query and len(parsed.query) > 1000:  # 長すぎるクエリ
            return False
        
        return True
        
    except Exception as e:
        logger.warning(f"form_url validation error: {sanitize_for_log(str(e))}")
        return False


def validate_company_url(url: str) -> bool:
    """
    企業URLの包括的形式検証（セキュリティ強化版）
    
    form_urlと同様の厳密な検証を企業URLに対しても実施
    """
    # form_urlと同じ検証を適用（企業URLも同じセキュリティ要件）
    return is_valid_form_url(url)


async def get_robust_page_url(page, fallback_url: Optional[str] = None, max_retries: int = 3) -> Optional[str]:
    """
    堅牢なページURL取得処理
    
    複数の手法を組み合わせて、最も信頼性の高いURLを取得します。
    Playwright APIを優先し、JavaScriptを補助的に使用。
    
    Args:
        page: PlaywrightのPageオブジェクト
        fallback_url: フォールバック用URL（通常はcompany_urlやlink_url）
        max_retries: 最大再試行回数
        
    Returns:
        Optional[str]: 取得されたURL、失敗時はNone
    """
    from playwright.async_api import Page
    
    if not isinstance(page, Page):
        logger.warning("Invalid page object provided to get_robust_page_url")
        return fallback_url if is_valid_form_url(fallback_url) else None
    
    # URL取得の階層的フォールバック処理
    url_candidates = []
    
    for attempt in range(max_retries):
        try:
            # 1. Playwright API（最も信頼性が高い）
            try:
                playwright_url = page.url
                if playwright_url and is_valid_form_url(playwright_url):
                    logger.debug(f"Playwright APIでURL取得成功: {playwright_url[:50]}...")
                    return playwright_url
                else:
                    logger.debug(f"Playwright API URL無効: {repr(playwright_url)}")
                    url_candidates.append(("playwright", playwright_url))
            except Exception as e:
                logger.debug(f"Playwright API URL取得エラー: {e}")
            
            # 2. JavaScript window.location.href（従来の方式）
            try:
                js_url = await page.evaluate("window.location.href")
                if js_url and is_valid_form_url(js_url):
                    logger.debug(f"JavaScript APIでURL取得成功: {js_url[:50]}...")
                    return js_url
                else:
                    logger.debug(f"JavaScript API URL無効: {repr(js_url)}")
                    url_candidates.append(("javascript", js_url))
            except Exception as e:
                logger.debug(f"JavaScript API URL取得エラー: {e}")
            
            # 3. JavaScript document.URL（代替方式）
            try:
                doc_url = await page.evaluate("document.URL")
                if doc_url and is_valid_form_url(doc_url):
                    logger.debug(f"Document URLでURL取得成功: {doc_url[:50]}...")
                    return doc_url
                else:
                    logger.debug(f"Document URL無効: {repr(doc_url)}")
                    url_candidates.append(("document", doc_url))
            except Exception as e:
                logger.debug(f"Document URL取得エラー: {e}")
            
            # 再試行の場合は少し待機
            if attempt < max_retries - 1:
                import asyncio
                await asyncio.sleep(0.5)
                logger.debug(f"URL取得再試行 {attempt + 2}/{max_retries}")
        
        except Exception as e:
            logger.warning(f"URL取得処理で予期しないエラー: {e}")
    
    # 4. フォールバックURL（外部から提供）
    if fallback_url and is_valid_form_url(fallback_url):
        logger.debug(f"フォールバックURLを使用: {fallback_url[:50]}...")
        return fallback_url
    
    # すべて失敗した場合の詳細ログ
    logger.warning("すべてのURL取得手法が失敗しました")
    if url_candidates:
        logger.debug("無効なURL候補:")
        for method, url in url_candidates:
            logger.debug(f"  {method}: {repr(url)[:50]}...")
    
    return None


def create_url_acquisition_summary(page_url: Optional[str], candidates: list, fallback_url: Optional[str]) -> dict:
    """
    URL取得プロセスのサマリーを作成（デバッグ・モニタリング用）
    
    Args:
        page_url: 取得されたページURL
        candidates: 試行されたURL候補のリスト
        fallback_url: 使用されたフォールバックURL
        
    Returns:
        dict: URL取得プロセスのサマリー情報
    """
    return {
        'final_url': page_url,
        'url_valid': is_valid_form_url(page_url) if page_url else False,
        'acquisition_success': page_url is not None and is_valid_form_url(page_url),
        'tried_methods': len(candidates),
        'fallback_used': page_url == fallback_url if fallback_url else False,
        'candidates_summary': [
            {
                'method': method,
                'url_valid': is_valid_form_url(url) if url else False,
                'url_preview': repr(url)[:30] if url else None
            }
            for method, url in candidates
        ]
    }