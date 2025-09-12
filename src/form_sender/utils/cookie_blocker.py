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
from typing import Iterable, Dict, Any, Optional, Tuple, List
from urllib.parse import urlparse

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


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _host_matches(host: str, patterns: Iterable[str]) -> bool:
    h = (host or "").lower()
    for p in patterns or []:
        p = (p or "").lower().lstrip(".")
        if not p:
            continue
        if h == p or h.endswith("." + p):
            return True
    return False


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
    strip_set_cookie: bool = False,
    resource_block_rules: Optional[Dict[str, bool]] = None,
    strip_set_cookie_third_party_only: bool = True,
    strip_set_cookie_domains: Optional[List[str]] = None,
    strip_set_cookie_exclude_domains: Optional[List[str]] = None,
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

    # 呼び出し時の main host（about:blank の可能性を考慮し、ハンドラ内で都度取得も行う）
    initial_main_url = getattr(page.main_frame, "url", "")

    async def _route_handler(route: Route):
        req = route.request
        r_type = req.resource_type
        url = req.url
        host = _hostname(url)
        # 動的に main host を評価（初期は about:blank の場合がある）
        main_url = getattr(page.main_frame, "url", "") or initial_main_url
        main_host = _hostname(main_url)

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
            # 限定条件の判定（既定: 第三者のみ）。exclude に該当するホストは除外
            try:
                if strip_set_cookie_exclude_domains and _host_matches(host, strip_set_cookie_exclude_domains):
                    raise RuntimeError("exclude-domain")
                should_strip = False
                if strip_set_cookie_domains:
                    should_strip = _host_matches(host, strip_set_cookie_domains)
                elif strip_set_cookie_third_party_only:
                    # main_host が未確定（about:blank等）の場合は安全側でストリップしない
                    should_strip = bool(main_host) and host and (host != main_host and not host.endswith("." + main_host))
                else:
                    should_strip = True
            except RuntimeError:
                should_strip = False

            if should_strip:
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
    """バナーを検出して Reject 系操作を試行（ノーバナー時は即時帰還）。

    変更点（性能改善）:
    - 既知セレクタは即時 `query_selector` のみで検知（クリック時だけ短いtimeout）。
    - テキストパターンは正規表現を一つに統合して1回探索。
    - クリック前に `count()` で存在確認し、見つからなければ待機しない。
    - iframe は優先度順（CMP/consentを含むURL優先）で最大数に制限、全体のタイムバジェットを強制。
    """
    if not enabled:
        return

    try:
        # 内部タイムバジェット（過大timeout防止）
        # クリック待機は控えめ、検索は極小。合計 ~1.5s 以内を目安。
        search_timeout_ms = min(max(50, int(timeout_ms * 0.2)), 400)
        click_timeout_ms = min(timeout_ms, 1200)
        total_budget_ms = 1500
        max_frames = 5

        start_ms = _now_ms()

        # 事前に結合正規表現を準備
        combined = _combined_reject_regex()

        # 1) Main frame: 既知セレクタ → まとめ正規表現（存在確認→クリック）
        if await _try_known_selectors(page, click_timeout_ms):
            return
        if await _try_role_regex(page, combined, search_timeout_ms, click_timeout_ms):
            return

        # 2) iframes: 優先度付きで上限まで探索（タイムバジェット超過で中止）
        frames = [f for f in page.frames if f != page.main_frame]
        frames = _prioritize_frames(frames)
        for f in frames[:max_frames]:
            if _elapsed_ms(start_ms) >= total_budget_ms:
                break
            if await _try_known_selectors(f, click_timeout_ms):
                return
            if await _try_role_regex(f, combined, search_timeout_ms, click_timeout_ms):
                return
    except Exception:
        # 失敗しても全体処理は続行
        pass


# ===== helpers =====

def _now_ms() -> int:
    try:
        import time
        return int(time.time() * 1000)
    except Exception:
        return 0


def _elapsed_ms(since_ms: int) -> int:
    return max(0, _now_ms() - since_ms)


def _combined_reject_regex():
    # 1回だけコンパイルしてキャッシュ
    try:
        if not hasattr(_combined_reject_regex, "_cache"):
            pat = "(?:" + "|".join(_REJECT_TEXTS) + ")"
            _combined_reject_regex._cache = re.compile(pat, re.I)
        return _combined_reject_regex._cache
    except Exception:
        return re.compile("|".join(_REJECT_TEXTS), re.I)


async def _try_known_selectors(scope: Page, click_timeout_ms: int) -> bool:
    try:
        for sel in _KNOWN_REJECT_SELECTORS:
            try:
                el = await scope.query_selector(sel)
                if el:
                    await el.click(timeout=click_timeout_ms)
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


async def _try_role_regex(scope: Page, regex, search_timeout_ms: int, click_timeout_ms: int) -> bool:
    try:
        loc = scope.get_by_role("button", name=regex)
        try:
            # 存在確認は超短時間で（見つかった時だけクリックに進む）
            cnt = await loc.count()
        except Exception:
            cnt = 0
        if cnt and cnt > 0:
            try:
                await loc.first.click(timeout=click_timeout_ms)
                return True
            except Exception:
                # クリック失敗時はスキップ（他候補に期待）
                return False
    except Exception:
        return False
    return False


def _prioritize_frames(frames: List[Page]):  # type: ignore[name-defined]
    # URLにCMP/consent/cookieキーワードを含むものを優先
    def score(f) -> int:
        try:
            u = (getattr(f, "url", "") or "").lower()
        except Exception:
            u = ""
        s = 0
        if _url_matches_any(u, CMP_HOST_PATTERNS):
            s += 2
        if any(k in u for k in ["consent", "cookie", "privacy", "gdpr"]):
            s += 1
        return -s  # sort ascending => higher score first

    try:
        return sorted(frames, key=score)
    except Exception:
        return frames
