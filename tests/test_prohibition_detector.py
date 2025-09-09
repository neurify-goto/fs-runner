#!/usr/bin/env python3
"""
営業禁止文言検出テストツール（単発実行）

- Supabase から処理対象を1件取得（--company-id 指定可）
- Playwright で対象URLへアクセスし、Cookie同意処理と動的待機（HubSpot等）を実施
- 実行系と同等の検出器（SalesProhibitionDetector + ProhibitionDetector）で営業禁止文言を検出
- 実行系と同じ評価基準（config/worker_config.json の early_abort しきい値）で判定を出力
- マッピングや実送信は一切行わない
- ページソース（必要に応じてiframe結合）と検出結果JSONを test_results/ 配下に保存
"""

import argparse
import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Optional BeautifulSoup (required-fields抽出ほどは使わないが、iframe結合の整形に利用)
try:
    from bs4 import BeautifulSoup  # type: ignore

    HAS_BEAUTIFULSOUP = True
except ImportError:
    HAS_BEAUTIFULSOUP = False

# プロジェクトパスを import path に追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root))

from playwright.async_api import async_playwright, Browser, Page
from supabase import create_client

# 実行系と同じ検出器
from form_sender.analyzer.sales_prohibition_detector import SalesProhibitionDetector
from form_sender.utils.cookie_handler import CookieConsentHandler
from config.manager import get_worker_config
from dataclasses import is_dataclass, asdict


# --- Logging (quiet デフォルト + サニタイズ) ---
logger = logging.getLogger(__name__)


class SanitizingFormatter(logging.Formatter):
    """LogSanitizer を用いて出力直前にメッセージをサニタイズするフォーマッタ"""

    def __init__(self, fmt: str = "%(asctime)s - %(levelname)s - %(message)s", datefmt: Optional[str] = None):
        super().__init__(fmt=fmt, datefmt=datefmt)
        try:
            from form_sender.security.log_sanitizer import LogSanitizer

            self._sanitizer = LogSanitizer()
        except Exception:
            self._sanitizer = None

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        rendered = super().format(record)
        if self._sanitizer:
            try:
                return self._sanitizer.sanitize_string(rendered)
            except Exception:
                return rendered
        return rendered


class SummaryOnlyFilter(logging.Filter):
    """quietモードで出力をサマリ中心に制御するフィルタ。"""

    def __init__(self, quiet: bool = True):
        super().__init__()
        self.quiet = quiet

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if not self.quiet:
            return True
        if record.levelno >= logging.ERROR:
            return True
        if bool(getattr(record, "summary", False)):
            return True
        # INFO/DEBUG はquietでは表示しない
        return False


def configure_logging(verbose: bool = False, debug: bool = False) -> None:
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.INFO)
    quiet = not (verbose or debug)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SanitizingFormatter("%(asctime)s - %(levelname)s - %(message)s"))
    if quiet:
        handler.addFilter(SummaryOnlyFilter(quiet=True))

    root.addHandler(handler)
    root.setLevel(level)


class ProhibitionDetectionTester:
    """営業禁止文言検出の単発テスト実行クラス"""

    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context = None
        self.page: Optional[Page] = None
        self.supabase_client = None
        self._initialized = False

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = project_root / "test_results" / f"prohibition_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = str(out_dir)

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.cleanup()
        return False

    async def initialize(self):
        if self._initialized:
            return
        await self._initialize_supabase()
        await self._initialize_browser()
        self._initialized = True
        logger.info("✅ ProhibitionDetectionTester initialized")

    async def cleanup(self):
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

    async def _initialize_supabase(self):
        try:
            from dotenv import load_dotenv

            load_dotenv(override=True)
            url = os.getenv("SUPABASE_URL")
            key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            if not url or not key:
                raise RuntimeError("Supabase credentials not found in .env")
            self.supabase_client = create_client(url, key)
        except Exception as e:
            logger.error(f"Supabase initialization failed: {e}")
            raise

    # --- helpers (DRY) ---
    def _build_browser_extra_args(self, engine: str, headless: bool) -> List[str]:
        extra_args: List[str] = []
        if engine == "chromium":
            is_linux = sys.platform.startswith("linux")
            extra_args = ["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            if is_linux:
                extra_args += ["--no-sandbox", "--disable-setuid-sandbox"]
            if headless:
                extra_args += ["--disable-gpu"]
        return extra_args

    async def _install_popup_hooks(self):
        if not self.context:
            return
        await self.context.add_init_script(
            """
            (() => { try {
              const noop = () => false;
              Object.defineProperty(window, 'close', { value: noop, configurable: true });
              const _open = window.open;
              Object.defineProperty(window, 'open', { value: (url, target, features) => {
                try { window.location.href = url; } catch {}
                return window;
              }, configurable: true });
            } catch {} })();
            """
        )

    async def _install_network_routing(self):
        if not self.context:
            return
        async def handle_route(route):
            r = route.request
            rtype = r.resource_type
            url = r.url.lower()
            if rtype in ["image", "media", "font", "manifest", "other"]:
                await route.abort()
                return
            if rtype == "stylesheet":
                await route.abort()
                return
            if rtype == "script":
                trackers = [
                    "googletagmanager.com",
                    "google-analytics.com",
                    "doubleclick.net",
                    "googlesyndication.com",
                    "facebook.net",
                    "connect.facebook.net",
                    "hotjar.com",
                    "mixpanel.com",
                    "amplitude.com",
                ]
                if any(k in url for k in trackers):
                    await route.abort()
                    return
            await route.continue_()
        await self.context.route("**/*", handle_route)

    async def _initialize_browser(self):
        try:
            self.playwright = await async_playwright().start()
            headless = True  # 常にヘッドレス（運用方針）

            engine = os.getenv("PLAYWRIGHT_ENGINE", "chromium").lower()
            engine_map = {
                "chromium": self.playwright.chromium,
                "webkit": self.playwright.webkit,
                "firefox": self.playwright.firefox,
            }
            launcher = engine_map.get(engine, self.playwright.chromium)
            if engine not in engine_map:
                logger.warning(f"Unknown PLAYWRIGHT_ENGINE='{engine}', falling back to chromium")

            extra_args = self._build_browser_extra_args(engine, headless)

            launch_kwargs = dict(headless=headless, args=extra_args)
            if engine == "chromium":
                try:
                    self.browser = await launcher.launch(channel="chrome", **launch_kwargs)
                except Exception:
                    self.browser = await launcher.launch(**launch_kwargs)
            else:
                self.browser = await launcher.launch(**launch_kwargs)

            self.context = await self.browser.new_context(
                ignore_https_errors=True,
                java_script_enabled=True,
                bypass_csp=True,
                viewport={"width": 1366, "height": 900},
            )

            await self._install_popup_hooks()
            await self._install_network_routing()

            self.context.set_default_navigation_timeout(30000)
            self.page = await self.context.new_page()
            self.page.on("close", lambda: logger.debug("Active page closed (event)"))
        except Exception as e:
            logger.error(f"Browser initialization failed: {e}")
            raise

    async def _recreate_page(self):
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self.context is None:
                return
            await self._install_popup_hooks()
            await self.context.set_extra_http_headers(
                {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            )
            self.context.set_default_navigation_timeout(30000)
            await self._install_network_routing()
            self.page = await self.context.new_page()
            self.page.on("close", lambda: logger.debug("Active page closed (event)"))
        except Exception as e:
            logger.debug(f"recreate page failed: {e}")

    async def fetch_test_form_url(self, company_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """テスト用の企業1件を取得（ID指定 or しきい値ランダム探索）"""
        try:
            cfg_path = project_root / "config" / "test_field_mapping.json"
            defaults = {"min_company_id": 1, "max_company_id": 536156, "max_retries": 10, "form_url_scheme": "http%"}
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
            except FileNotFoundError:
                logger.info("Config not found: config/test_field_mapping.json (using defaults)")
                cfg = defaults
            except Exception as e:
                logger.warning(f"Failed to load config/test_field_mapping.json: {e} (using defaults)")
                cfg = defaults

            if company_id:
                resp = (
                    self.supabase_client.table("companies")
                    .select("id, company_name, form_url, instruction_json, company_url")
                    .eq("id", company_id)
                    .neq("form_url", None)
                    .ilike("form_url", "http%")
                    .limit(1)
                    .execute()
                )
                if not resp.data:
                    logger.error(f"Company with ID {company_id} not found or has no http form_url")
                    return None
                company = resp.data[0]
                logger.info("✅ Specific company selected", extra={"summary": True})
            else:
                min_id = int(cfg.get("min_company_id", 1))
                max_id = int(cfg.get("max_company_id", 536156))
                max_retries = int(cfg.get("max_retries", 10))
                scheme = str(cfg.get("form_url_scheme", "http%"))
                if min_id < 1:
                    min_id = 1
                if max_id < min_id:
                    max_id = min_id
                company = None
                for attempt in range(1, max_retries + 1):
                    threshold = random.randint(min_id, max_id)
                    logger.info(f"Attempt {attempt}/{max_retries} with threshold >= {threshold}")
                    resp = (
                        self.supabase_client.table("companies")
                        .select("id, company_name, form_url, instruction_json, company_url")
                        .neq("form_url", None)
                        .ilike("form_url", scheme)
                        .gte("id", threshold)
                        .order("id", desc=False)
                        .limit(1)
                        .execute()
                    )
                    if resp.data:
                        company = resp.data[0]
                        break
                if not company:
                    logger.error("Failed to find any company with http form_url after retries")
                    return None
                logger.info("✅ Test company selected via threshold search", extra={"summary": True})

            logger.info(f"🎯 Target company_id: {company['id']}", extra={"summary": True})
            # LogSanitizer に委譲（CIでは自動マスクされる）
            try:
                logger.info(f"   Company: {company.get('company_name', '')}")
                logger.info(f"   Form URL: {company.get('form_url', '')}")
            except Exception:
                pass
            return company
        except Exception as e:
            logger.error(f"Failed to fetch test form URL: {e}")
            return None

    async def _goto_with_popup_recovery(self, url: str) -> None:
        popup_captured: List[Page] = []

        def _on_popup(p):
            try:
                popup_captured.append(p)
                logger.debug("Popup captured during navigation")
            except Exception:
                pass

        assert self.page is not None
        self.page.once("popup", _on_popup)

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:
            if "has been closed" in str(e) and popup_captured:
                candidate = popup_captured[-1]
                try:
                    is_closed = bool(candidate.is_closed())
                except Exception:
                    is_closed = False
                if not is_closed:
                    self.page = candidate
                    logger.info("Detected self-close -> switched to popup page")
                else:
                    logger.info("Captured popup already closed. Recreating page and retrying...")
                    await self._recreate_page()
                    assert self.page is not None
                    await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            elif "has been closed" in str(e):
                logger.info("Detected unexpected page close. Recreating page and retrying once...")
                await self._recreate_page()
                assert self.page is not None
                await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            else:
                logger.error(f"Goto error: {type(e).__name__}: {e}")
                raise

    async def _stabilize_after_navigation(self) -> None:
        assert self.page is not None
        await asyncio.sleep(0.5)
        await CookieConsentHandler.handle(self.page)

    async def _detect_initial_form_and_hubspot(self) -> Tuple[int, bool]:
        assert self.page is not None
        form_count = await self.page.evaluate("document.querySelectorAll('form').length")
        has_hs = await self.page.evaluate(
            """
            () => {
              const scripts = Array.from(document.querySelectorAll('script'));
              return scripts.some(s => s.src && (s.src.includes('hsforms.net') || s.src.includes('hubspot')));
            }
            """
        )
        logger.info(f"📋 Initial forms={form_count}, hubspot_script={bool(has_hs)}")
        return int(form_count), bool(has_hs)

    async def _wait_for_dynamic_content(self, max_wait: int = 15) -> bool:
        assert self.page is not None
        logger.info("🔄 Waiting for dynamic content...")
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # 軽量JS確認（フォーム or 入力増加）
        try:
            final_check = await self.page.evaluate(
                """
                () => {
                  const forms = document.querySelectorAll('form').length;
                  const inputs = document.querySelectorAll('input').length;
                  const textareas = document.querySelectorAll('textarea').length;
                  const selects = document.querySelectorAll('select').length;
                  return {forms, inputs, textareas, selects};
                }
                """
            )
            total = final_check["forms"] + final_check["inputs"] + final_check["textareas"] + final_check["selects"]
            if total > 0:
                logger.info(f"✅ Elements present after wait: forms={final_check['forms']}, inputs={final_check['inputs']}")
                return True
        except Exception:
            pass

        # スクロールフォールバック
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            await self.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(1)
            forms = await self.page.evaluate("document.querySelectorAll('form').length")
            if int(forms or 0) > 0:
                return True
        except Exception:
            pass
        return False

    async def _analyze_iframes(self) -> Tuple[str, Optional[Any]]:
        """iframeからフォームがありそうなフレームを抽出（保存用HTMLも収集）"""
        assert self.page is not None
        iframe_contents: List[str] = []
        target_frame = None
        try:
            for frame in self.page.frames:
                if frame == self.page.main_frame:
                    continue
                try:
                    content = await frame.content()
                    forms = await frame.query_selector_all("form")
                    has_forms = len(forms) > 0
                    if HAS_BEAUTIFULSOUP:
                        soup = BeautifulSoup(content, "html.parser")
                        only_forms = soup.find_all("form")
                        if only_forms:
                            iframe_contents.append("\n".join(str(f) for f in only_forms))
                    else:
                        if "<form" in content.lower():
                            iframe_contents.append(content)
                    if has_forms and not target_frame:
                        target_frame = frame
                except Exception:
                    continue
        except Exception:
            pass
        return ("\n\n".join(iframe_contents), target_frame)

    async def _extract_page_source_with_iframes(self, form_count: int) -> Tuple[str, Optional[Any]]:
        """ページソースを保存用に抽出。必要ならiframeも結合し、ターゲットフレームも返す。"""
        assert self.page is not None
        page_source = await self.page.content()

        need_iframe = False
        if form_count == 0:
            need_iframe = True
        else:
            try:
                inputs = await self.page.evaluate("document.querySelectorAll('input, textarea, select').length")
                if int(inputs or 0) == 0:
                    need_iframe = True
            except Exception:
                pass

        target_frame = None
        if need_iframe:
            iframe_content, target_frame = await self._analyze_iframes()
            if iframe_content:
                page_source += "\n\n" + iframe_content

        return page_source, target_frame

    async def _save_page_source(self, content: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.temp_dir, f"page_source_{ts}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"📄 Page source saved: {path}", extra={"summary": True})
        return path

    def _parse_detection_result(self, sp: Dict[str, Any]) -> Tuple[str, str, float, int]:
        if not isinstance(sp, dict):
            return "none", "none", 0.0, 0
        level = (sp.get("prohibition_level") or sp.get("detection_method") or "detected")
        level_l = str(level).lower()
        conf_level = str(sp.get("confidence_level") or "none").lower()
        try:
            conf_score = float(sp.get("confidence_score") or 0.0)
        except Exception:
            conf_score = 0.0
        if conf_score < 0:
            conf_score = 0.0
        if conf_score > 100:
            conf_score = 100.0
        matches = sp.get("matches")
        try:
            matches_count = int(sp.get("summary", {}).get("total_matches"))
        except Exception:
            matches_count = len(matches) if isinstance(matches, (list, tuple)) else 0
        return level_l, conf_level, conf_score, matches_count

    def _load_thresholds(self) -> Tuple[str, str, float, int]:
        try:
            det = (get_worker_config() or {}).get("detectors", {}).get("prohibition", {})
            lvl_min = str(det.get("early_abort", {}).get("min_level", "moderate")).lower()
            conf_lvl_min = str(det.get("early_abort", {}).get("min_confidence_level", "high")).lower()
            score_min = float(det.get("early_abort", {}).get("min_score", 80))
            matches_min = int(det.get("early_abort", {}).get("min_matches", 2))
            return lvl_min, conf_lvl_min, score_min, matches_min
        except Exception:
            return "moderate", "high", 80.0, 2

    def _evaluate_against_thresholds(self, parsed: Tuple[str, str, float, int], thresholds: Tuple[str, str, float, int]) -> Tuple[bool, Dict[str, Any]]:
        """検出結果を early_abort しきい値で評価

        注意:
        - 検出器は 'prohibition_level' に 'none'|'mild'|'moderate'|'strict' を返す。
          これまで 'none' が未知レベルとして 'moderate' と同等に扱われ、偽陽性の中断が発生していた。
          'none' は 'weak' 未満として扱う必要がある。
        - 未知レベルは最小とみなす（中断根拠としない）。
        """
        level_l, conf_level, conf_score, matches_count = parsed
        lvl_min, conf_lvl_min, score_min, matches_min = thresholds

        # レベル順位（小さいほど弱い）
        order = {"none": -1, "weak": 0, "mild": 1, "moderate": 2, "strict": 3}
        lvl_min_idx = order.get(lvl_min, 2)
        level_idx = order.get(level_l, -1)  # 未知/none は最小とする

        should_abort = False

        # 1) レベルがしきい値以上
        if level_idx >= lvl_min_idx and level_idx >= 0:
            should_abort = True
        # 2) 信頼度レベル一致 or スコア到達
        elif (conf_level or "").lower() == (conf_lvl_min or "").lower() or conf_score >= score_min:
            should_abort = True
        # 3) マッチ件数到達
        elif matches_count >= matches_min:
            should_abort = True

        summary = {
            "level": level_l,
            "confidence_level": conf_level or None,
            "confidence_score": conf_score if conf_score > 0 else None,
            "matches_count": matches_count,
        }
        return should_abort, summary

    def _evaluate_prohibition_detection(self, sp: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """実行系(Worker)と同等の基準で早期中断要否を評価"""
        parsed = self._parse_detection_result(sp)
        thresholds = self._load_thresholds()
        return self._evaluate_against_thresholds(parsed, thresholds)

    async def run_single_test(self, company_id: Optional[int] = None) -> bool:
        try:
            company = await self.fetch_test_form_url(company_id)
            if not company:
                return False

            assert self.page is not None
            url = company["form_url"]
            logger.info("Starting prohibition detection...", extra={"summary": True})
            logger.info("Target URL: ***URL_REDACTED***")

            await self._goto_with_popup_recovery(url)
            await self._stabilize_after_navigation()
            form_count, has_hs = await self._detect_initial_form_and_hubspot()
            if form_count == 0 or has_hs:
                await self._wait_for_dynamic_content()

            # ページソース（+必要に応じてiframe）保存 + ターゲットフレーム決定
            page_source, target_frame = await self._extract_page_source_with_iframes(form_count)
            source_file = await self._save_page_source(page_source)

            # 実行系と同じ検出器を使う（target_frameがあればそちらを優先）
            detector_ctx = target_frame if target_frame else self.page
            sp = await SalesProhibitionDetector(detector_ctx).detect_prohibition_text()

            should_abort, summary = self._evaluate_prohibition_detection(sp or {})

            # 結果をファイル保存
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(self.temp_dir, f"prohibition_result_{ts}.json")
            # dataclass(ProhibitionMatch) を含む可能性があるためJSON化
            serializable_sp = self._make_json_serializable(sp)
            payload = {
                "company_id": company["id"],
                "form_url": url,
                "timestamp": ts,
                "sales_prohibition": serializable_sp,
                "evaluation": {"should_abort": should_abort, **summary},
                "source_file": source_file,
            }
            # URL等はログでサニタイズされるが、ファイルには実値を保持
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Detection result saved: {out_path}", extra={"summary": True})

            # 依頼: 営業禁止判定（true/false）をログに明示
            logger.info(
                f"営業禁止判定(should_abort): {'true' if should_abort else 'false'}",
                extra={"summary": True},
            )

            if should_abort:
                logger.warning(
                    f"判定: 営業禁止（早期中断基準に合致） level={summary.get('level')}, matches={summary.get('matches_count')}, conf={summary.get('confidence_level')}/{summary.get('confidence_score')}"
                )
            else:
                logger.info(
                    f"判定: 営業禁止なし（早期中断基準未満） level={summary.get('level')}, matches={summary.get('matches_count')}, conf={summary.get('confidence_level')}/{summary.get('confidence_score')}"
                )
            return True
        except Exception as e:
            logger.error(f"Detection test failed: {e}")
            return False

    def _make_json_serializable(self, obj: Any, depth: int = 0, max_depth: Optional[int] = None) -> Any:
        """検出結果をJSONシリアライズ可能な形へ再帰的に変換（深さ制限付き）"""
        if max_depth is None:
            try:
                max_depth = int((get_worker_config() or {}).get("storage", {}).get("sanitize_max_depth", 6))
            except Exception:
                max_depth = 6
        if depth >= (max_depth or 6):
            return "<max_depth_reached>"
        try:
            if obj is None or isinstance(obj, (str, int, float, bool)):
                return obj
            if isinstance(obj, dict):
                return {str(k): self._make_json_serializable(v, depth + 1, max_depth) for k, v in obj.items()}
            if isinstance(obj, (list, tuple, set)):
                return [self._make_json_serializable(v, depth + 1, max_depth) for v in obj]
            if is_dataclass(obj):
                return self._make_json_serializable(asdict(obj), depth + 1, max_depth)
            # 最後の手段: 文字列化
            return str(obj)
        except Exception:
            try:
                return str(obj)
            except Exception:
                return None


# デフォルト実行タイムアウト（秒）: 環境変数で上書き可
try:
    DEFAULT_TEST_TIMEOUT_SECONDS = int(os.getenv("PROHIBITION_TEST_TIMEOUT_SECONDS", "60"))
except Exception:
    DEFAULT_TEST_TIMEOUT_SECONDS = 60


async def main():
    parser = argparse.ArgumentParser(
        description="Sales Prohibition Detection Test (single-run)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tests/test_prohibition_detector.py                    # Single random test
  python tests/test_prohibition_detector.py --company-id 12345 # Test specific company
""",
    )
    parser.add_argument("--company-id", type=int, help="Specific company ID to test")
    parser.add_argument("--verbose", action="store_true", help="Show normal logs (summary filter off)")
    parser.add_argument("--debug", action="store_true", help="Show debug logs")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose, debug=args.debug)

    try:
        async with ProhibitionDetectionTester() as tester:
            try:
                ok = await asyncio.wait_for(tester.run_single_test(args.company_id), timeout=DEFAULT_TEST_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(f"⏱️ Test timed out after {DEFAULT_TEST_TIMEOUT_SECONDS} seconds")
                ok = False
            if ok:
                logger.info("🎯 Prohibition detection test completed successfully!", extra={"summary": True})
            else:
                logger.error("❌ Prohibition detection test failed")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
