"""
Cookie 同意バナー/トラッキングの汎用的ブロッカー

戦略（優先度順）:
1) ネットワーク層: 主要CMPスクリプトのブロック、Set-Cookieヘッダ除去
2) JS層       : document.cookie のブラックホール化（任意）
3) UI層       : Reject All / 必要なCookieのみ を自動クリック

設定は worker_config.json の browser.cookie_control.* を参照。
"""

from __future__ import annotations

import asyncio
import re
from typing import Iterable, Dict, Any, Optional, Tuple

from playwright.async_api import BrowserContext, Page, Route


# 主要CMPドメイン（過剰ブロックを避けつつ高頻度なもの中心）
CMP_HOST_PATTERNS: Tuple[str, ...] = (
    # OneTrust / CookieLaw
    "onetrust.com",
    "cdn.cookielaw.org",
    "cookielaw.org",
    # Cookiebot
    "cookiebot.com",
    "consent.cookiebot.com",
    "cookiebot.eu",
    # TrustArc / TRUSTe
    "trustarc.com",
    "truste.com",
    # Quantcast CMP / IAB TCF
    "quantcast.com",
    "consensu.org",
    "cmp.quantcast.com",
    # Usercentrics
    "usercentrics.eu",
    "usercentrics.com",
    # Osano
    "osano.com",
    "cdn.osano.com",
    # CookieYes
    "cookieyes.com",
    "cdn-cookieyes.com",
    # Iubenda
    "iubenda.com",
    "cdn.iubenda.com",
    # Axeptio
    "axept.io",
    "axeptio.eu",
)


def _url_matches_any(url: str, patterns: Iterable[str]) -> bool:
    u = (url or "").lower()
    return any(p in u for p in patterns)


def get_cookie_blackhole_script() -> str:
    """document.cookie を無害化する初期化スクリプト。検出耐性を考慮し最低限のみ。"""
    return (
        "Object.defineProperty(document, 'cookie', {\n"
        "  get: function() { return ''; },\n"
        "  set: function(value) { return true; },\n"
        "  configurable: true\n"
        "});\n"
    )


async def install_init_script(context: BrowserContext, enabled: bool) -> None:
    """コンテキストに cookie ブラックホールを注入（有効時のみ）。"""
    if not enabled:
        return
    try:
        await context.add_init_script(get_cookie_blackhole_script())
    except Exception:
        # 失敗しても黙って続行（サイト互換優先）
        pass


async def install_cookie_routes(
    page: Page,
    *,
    block_cmp_scripts: bool = True,
    strip_set_cookie: bool = True,
    resource_block_rules: Optional[Dict[str, bool]] = None,
) -> None:
    """ネットワークルーティングを設定。

    - CMPスクリプトのブロック（abort）
    - レスポンスの Set-Cookie ヘッダを除去（fulfill）
    - 既存のリソースブロック（画像/フォント/CSS）と共存
    """

    resource_block_rules = resource_block_rules or {}
    block_images = bool(resource_block_rules.get("images", False))
    block_fonts = bool(resource_block_rules.get("fonts", False))
    block_styles = bool(resource_block_rules.get("stylesheets", False))

    async def _route_handler(route: Route):
        req = route.request
        r_type = req.resource_type
        url = req.url

        # 1) 静的資源のブロック（既存ポリシーと一致）
        if (block_images and r_type in ("image", "media")) or \
           (block_fonts and r_type == "font") or \
           (block_styles and r_type == "stylesheet"):
            try:
                await route.abort()
                return
            except Exception:
                pass

        # 2) CMP/同意管理スクリプトのブロック
        if block_cmp_scripts and _url_matches_any(url, CMP_HOST_PATTERNS):
            try:
                await route.abort()
                return
            except Exception:
                pass

        # 3) Set-Cookie 除去（ドキュメント/XHR/Fetch に限定）
        if strip_set_cookie and r_type in ("document", "xhr", "fetch"):
            try:
                resp = await route.fetch()
                # ヘッダから Set-Cookie を除去
                headers = {k: v for k, v in resp.headers.items() if k.lower() != "set-cookie"}
                body = await resp.body()
                await route.fulfill(
                    status=resp.status,
                    headers=headers,
                    body=body,
                    content_type=resp.headers.get("content-type")
                )
                return
            except Exception:
                # フェッチ/フィルフィル失敗時は通常継続
                try:
                    await route.continue_()
                    return
                except Exception:
                    pass

        # 4) それ以外はそのまま
        try:
            await route.continue_()
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass

    try:
        await page.route("**/*", _route_handler)
    except Exception:
        # ルート設定に失敗しても致命ではない
        pass


_REJECT_TEXTS = [
    # 英語
    r"Reject All", r"Decline All", r"Reject", r"I do not accept",
    r"Only necessary", r"Necessary only",
    # 日本語
    r"すべて拒否", r"拒否する", r"同意しない", r"必須のみ", r"必要なCookieのみ",
    # 欧州主要言語（簡易）
    r"Alles ablehnen",  # DE
    r"Tout refuser",    # FR
    r"Rechazar todo",   # ES
    r"Rifiuta tutto",   # IT
]

_KNOWN_REJECT_SELECTORS = [
    # OneTrust
    "#onetrust-reject-all-handler",
    "button[aria-label='拒否する']",
    # Cookiebot
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",
    ".CybotCookiebotDialogBodyButtonDecline",
    # TrustArc
    "#truste-consent-required",
    "#truste-consent-button",
    # Usercentrics
    "button[data-testid='uc-customize-reject-all']",
    "button[data-testid='uc-reject-all']",
]


async def try_reject_banners(page: Page, enabled: bool = True, timeout_ms: int = 2000) -> None:
    """バナーを検出して Reject 系操作を試行（失敗しても黙って続行）。"""
    if not enabled:
        return
    try:
        # 1) 既知セレクタを素早く試す
        for sel in _KNOWN_REJECT_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click(timeout=timeout_ms)
                    return
            except Exception:
                continue

        # 2) ロール/テキストで幅広く探す
        for pat in _REJECT_TEXTS:
            try:
                locator = page.get_by_role("button", name=re.compile(pat, re.I))
                await locator.first.click(timeout=timeout_ms)
                return
            except Exception:
                continue

        # 3) iframe 内も軽く探索
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            for sel in _KNOWN_REJECT_SELECTORS:
                try:
                    el = await frame.query_selector(sel)
                    if el:
                        await el.click(timeout=timeout_ms)
                        return
                except Exception:
                    continue
            for pat in _REJECT_TEXTS:
                try:
                    locator = frame.get_by_role("button", name=re.compile(pat, re.I))
                    await locator.first.click(timeout=timeout_ms)
                    return
                except Exception:
                    continue
    except Exception:
        pass

