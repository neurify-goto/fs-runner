#!/usr/bin/env python3
"""
フィールドマッピング精度検証・改善専用ツール

実際のform_urlを使用してフィールドマッピングアルゴリズムの精度を検証し、
段階的な改善を行うための反復テストツール
"""

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

try:
    from bs4 import BeautifulSoup

    HAS_BEAUTIFULSOUP = True
except ImportError:
    HAS_BEAUTIFULSOUP = False
    print("⚠️ BeautifulSoup4 not installed. Install with: pip install beautifulsoup4")
    print("   Form content extraction will use basic fallback method.")

# 環境設定
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root))

from playwright.async_api import async_playwright, Browser, Page
from supabase import create_client

# フォーム解析関連
from form_sender.analyzer.rule_based_analyzer import RuleBasedAnalyzer
from form_sender.utils.cookie_handler import CookieConsentHandler
# マッピング検証では公開可能な擬似データを使用する
from tests.fixtures.sample_client_data import (
    CLIENT_DATA,
    TARGETING_DATA,
    create_test_client_config,
)

# ログ設定（quietデフォルト + サニタイズ + サマリフィルタ）
logger = logging.getLogger(__name__)


class SanitizingFormatter(logging.Formatter):
    """LogSanitizer を用いて出力直前にメッセージをサニタイズするフォーマッタ"""

    def __init__(
        self,
        fmt: str = "%(asctime)s - %(levelname)s - %(message)s",
        datefmt: Optional[str] = None,
    ):
        super().__init__(fmt=fmt, datefmt=datefmt)
        try:
            from form_sender.security.log_sanitizer import LogSanitizer

            self._sanitizer = LogSanitizer()
        except Exception:
            self._sanitizer = None

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if self._sanitizer:
            try:
                return self._sanitizer.sanitize_string(rendered)
            except Exception:
                return rendered
        return rendered


class SummaryOnlyFilter(logging.Filter):
    """quietモードで出力をサマリ中心に制御するフィルタ。

    仕様（quiet=True のとき）:
    - ERROR以上: 常に通す
    - summaryタグ付き: 通す
    - 上記以外のWARNING: 原則通すが、
      フィールドマッピング内部の詳細警告（duplicate_prevention 等）はノイズのため抑制
    - INFO/DEBUG: 抑制
    """

    def __init__(self, quiet: bool = True):
        super().__init__()
        self.quiet = quiet

    def _is_internal_mapping_warning(self, record: logging.LogRecord) -> bool:
        """マッピング内部の詳細警告か判定（quietでは抑制対象）。"""
        name = getattr(record, "name", "")
        if name.startswith("form_sender.analyzer.duplicate_prevention"):
            return True
        # 既知の詳細警告文言でも抑制（将来の名称変更に耐性）
        msg = str(getattr(record, "msg", "")).lower()
        if any(
            keyword in msg
            for keyword in [
                "duplicate value detected",
                "field group conflict detected",
            ]
        ):
            return True
        return False

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if not self.quiet:
            return True

        # エラー以上は常に通す
        if record.levelno >= logging.ERROR:
            return True

        # サマリ指定は通す
        if bool(getattr(record, "summary", False)):
            return True

        # WARNING は原則通すが、内部詳細警告は抑制
        if record.levelno == logging.WARNING:
            return not self._is_internal_mapping_warning(record)

        # INFO/DEBUG はquietでは表示しない
        return False


def configure_logging(verbose: bool = False, debug: bool = False) -> None:
    """ルートロガーを再構成。quiet(既定)/verbose/debug を切替。"""
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.INFO)
    quiet = not (verbose or debug)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SanitizingFormatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    if quiet:
        handler.addFilter(SummaryOnlyFilter(quiet=True))

    root.addHandler(handler)
    root.setLevel(level)


class FieldMappingAnalyzer:
    """フィールドマッピング精度検証ツール"""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.supabase_client = None
        self._initialized = False
        # プロジェクト配下にテスト結果ディレクトリを作成
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        project_root = Path(__file__).parent.parent
        test_results_dir = project_root / "test_results" / f"field_mapping_{timestamp}"
        test_results_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = str(test_results_dir)

    async def __aenter__(self):
        """コンテキストマネージャー開始"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """コンテキストマネージャー終了 - 確実なリソース解放"""
        await self.cleanup()
        return False

    async def initialize(self):
        """システム初期化"""
        if self._initialized:
            logger.warning("Already initialized, skipping...")
            return

        try:
            # Supabase接続初期化
            await self._initialize_supabase()

            # Playwright初期化（メモリ最適化）
            await self._initialize_browser()

            self._initialized = True
            logger.info("✅ FieldMappingAnalyzer initialized successfully")

        except Exception as e:
            logger.error(f"❌ Initialization failed: {e}")
            await self.cleanup()  # 部分的初期化でもクリーンアップ
            raise

    async def _initialize_supabase(self):
        """Supabase接続初期化"""
        try:
            from dotenv import load_dotenv

            # OS 環境に同名変数が存在しても .env の値を優先
            load_dotenv(override=True)

            supabase_url = os.getenv("SUPABASE_URL")
            supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

            if not supabase_url or not supabase_key:
                raise ValueError("Supabase credentials not found in environment")

            self.supabase_client = create_client(supabase_url, supabase_key)

        except Exception as e:
            logger.error(f"❌ Supabase initialization failed: {e}")
            raise

    async def _initialize_browser(self):
        """Playwright初期化（安定化＋メモリ最適化）"""
        try:
            self.playwright = await async_playwright().start()
            # このテストは常にヘッドレスで実行する（運用ポリシー）
            # 以前は環境変数で切替可能だったが、誤ってGUI実行されるのを防ぐため固定化
            headless = True

            # ブラウザエンジン選択（デフォルト: chromium）。問題発生時に切替可能。
            engine = os.getenv("PLAYWRIGHT_ENGINE", "chromium").lower()
            engine_map = {
                "chromium": self.playwright.chromium,
                "webkit": self.playwright.webkit,
                "firefox": self.playwright.firefox,
            }
            launcher = engine_map.get(engine, self.playwright.chromium)
            if engine not in engine_map:
                logger.warning(
                    f"Unknown PLAYWRIGHT_ENGINE='{engine}', falling back to chromium"
                )

            # Chromium でクラッシュしやすいフラグを整理し、最小限の安定構成にする
            # - macOS では sandbox 系フラグは不要（Linux CI のみに限定）
            # - 一部の disable-* フラグは描画/IPC 周りの不整合でクラッシュを誘発するため除去
            extra_args = []
            if engine == "chromium":
                import sys

                is_linux = sys.platform.startswith("linux")
                # 最小・安全寄りのフラグのみ適用
                extra_args = [
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ]
                # Linux CI のみ sandbox 無効化
                if is_linux:
                    extra_args += ["--no-sandbox", "--disable-setuid-sandbox"]
                # ヘッドレス時のみ GPU を抑制（描画周りの安定化）
                if headless:
                    extra_args += ["--disable-gpu"]
            else:
                # Firefox/WebKit は既存の安定挙動に委ねる（追加フラグなし）
                extra_args = []

            # Chromium が環境依存でクラッシュするケースに備え、
            # まずシステム Chrome チャンネルでの起動を試み、失敗したら同バイナリで再試行
            launch_kwargs = dict(headless=headless, args=extra_args)
            if engine == "chromium":
                try:
                    self.browser = await launcher.launch(
                        channel="chrome", **launch_kwargs
                    )
                except Exception:
                    self.browser = await launcher.launch(**launch_kwargs)
            else:
                self.browser = await launcher.launch(**launch_kwargs)
            # コンテキスト＋ページ作成（ページクローズ時の復旧を容易にする）
            self.context = await self.browser.new_context(
                ignore_https_errors=True,
                java_script_enabled=True,
                bypass_csp=True,
                viewport={"width": 1366, "height": 900},
            )

            # いくつかのサイトで発生する self-closing/popup リダイレクト対策
            # - window.close を無効化
            # - window.open は同一タブ遷移にフォールバック
            await self.context.add_init_script(
                """
                (() => {
                  try {
                    const noop = () => false;
                    Object.defineProperty(window, 'close', { value: noop, configurable: true });
                    const _open = window.open;
                    Object.defineProperty(window, 'open', { value: (url, target, features) => {
                      try { window.location.href = url; } catch {}
                      return window;
                    }, configurable: true });
                  } catch {}
                })();
                """
            )

            # ページクローズを検知して自動で新規ページを補充
            self.context.set_default_navigation_timeout(30000)
            self.page = await self.context.new_page()
            self.page.on("close", lambda: logger.debug("Active page closed (event)"))

            # 不要リソースのブロッキング（速度最適化）。
            # 以前は script を厳しくブロックしていたが、
            # 動的生成フォーム（例: 外部プラットフォーム）で要素が生成されない問題が発生。
            # 汎用精度を優先し script は許可し、明らかなトラッキング系のみ抑制する。
            async def handle_route(route):
                resource_type = route.request.resource_type
                url = route.request.url.lower()

                # 画像・フォント・メディアは引き続き遮断（DOM解析に不要）
                if resource_type in ["image", "media", "font", "manifest", "other"]:
                    await route.abort()
                    return

                # CSS は基本ブロック（表示崩れよりパフォーマンス優先）。
                # ただしフォーム生成に CSS は不要なため許容リスクは低い。
                if resource_type == "stylesheet":
                    await route.abort()
                    return

                # Script は許可（フォーム生成のため）。
                # ただし明確なトラッキング・広告系のみブロックしてノイズを低減。
                if resource_type == "script":
                    tracker_keywords = [
                        "googletagmanager.com",
                        "google-analytics.com",
                        "www.google-analytics.com",
                        "doubleclick.net",
                        "googlesyndication.com",
                        "facebook.net",
                        "connect.facebook.net",
                        "hotjar.com",
                        "mixpanel.com",
                        "amplitude.com",
                    ]
                    if any(k in url for k in tracker_keywords):
                        await route.abort()
                        return

                # その他は許可
                await route.continue_()

            # 以降に生成されるページにも適用されるよう Context に適用
            await self.context.route("**/*", handle_route)

            # User Agent設定（Context単位で適用）
            await self.context.set_extra_http_headers(
                {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }
            )

            logger.info("✅ Browser initialized with memory optimization (headless)")

        except Exception as e:
            logger.error(f"❌ Browser initialization failed: {e}")
            # 部分的初期化でもクリーンアップ
            if self.page:
                try:
                    await self.page.close()
                except:
                    pass
            if self.context:
                try:
                    await self.context.close()
                except:
                    pass
            if self.browser:
                try:
                    await self.browser.close()
                except:
                    pass
            if self.playwright:
                try:
                    await self.playwright.stop()
                except:
                    pass
            raise

    async def _recreate_page(self, max_retries: int = 2) -> None:
        """ページ/コンテキスト閉鎖時の簡易リカバリ（最大 max_retries 回）。"""
        import asyncio

        last_err = None
        for attempt in range(max_retries):
            try:
                if self.page:
                    try:
                        await self.page.close()
                    except Exception:
                        pass
                if self.context is None:
                    if self.browser is None:
                        # ブラウザも無い場合は再初期化
                        await self._initialize_browser()
                        return
                    # 初期化時と同等のオプションを再適用
                    self.context = await self.browser.new_context(
                        ignore_https_errors=True,
                        java_script_enabled=True,
                        bypass_csp=True,
                        viewport={"width": 1366, "height": 900},
                    )

                    # self-closing / popup 抑止 init_script を最優先で再適用
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

                    # User-Agent / タイムアウト再設定
                    await self.context.set_extra_http_headers(
                        {
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                        }
                    )
                    self.context.set_default_navigation_timeout(30000)

                    # 再ルーティング（初期化時と同様のブロッキング方針）
                    async def handle_route(route):
                        resource_type = route.request.resource_type
                        url = route.request.url.lower()
                        if resource_type in [
                            "image",
                            "media",
                            "font",
                            "manifest",
                            "other",
                        ]:
                            await route.abort()
                            return
                        if resource_type == "stylesheet":
                            await route.abort()
                            return
                        if resource_type == "script":
                            tracker_keywords = [
                                "googletagmanager.com",
                                "google-analytics.com",
                                "www.google-analytics.com",
                                "doubleclick.net",
                                "googlesyndication.com",
                                "facebook.net",
                                "connect.facebook.net",
                                "hotjar.com",
                                "mixpanel.com",
                                "amplitude.com",
                            ]
                            if any(k in url for k in tracker_keywords):
                                await route.abort()
                                return
                        await route.continue_()

                    await self.context.route("**/*", handle_route)

                # 新規ページ生成とイベント再設定
                self.page = await self.context.new_page()
                self.page.on(
                    "close", lambda: logger.debug("Active page closed (event)")
                )
                return
            except Exception as e:
                last_err = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        # 失敗時は最後の手段として完全再初期化
        await self.cleanup()
        await self.initialize()

    async def fetch_test_form_url(
        self, company_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """テスト用form_urlを1件取得（指定IDまたはランダム選択）"""
        try:
            # 設定読み込み（存在しない場合はデフォルト）
            def _load_threshold_config() -> Dict[str, Any]:
                cfg_path = Path(__file__).parent.parent / "config" / "test_field_mapping.json"
                defaults = {
                    "min_company_id": 1,
                    "max_company_id": 536156,
                    "max_retries": 10,
                    "form_url_scheme": "http%",
                }
                try:
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                    return {**defaults, **{k: v for k, v in data.items() if k in defaults}}
                except FileNotFoundError:
                    logger.info(
                        "Config not found: config/test_field_mapping.json (using defaults)"
                    )
                    return defaults
                except Exception as e:
                    logger.warning(
                        f"Failed to load config/test_field_mapping.json: {e} (using defaults)"
                    )
                    return defaults

            cfg = _load_threshold_config()
            if company_id:
                logger.info(
                    f"Fetching specific company (ID: {company_id}) from database...",
                    extra={"summary": True},
                )

                # 指定されたIDの企業を取得
                # フォームURLは http(s) のみを対象にする（mailto等でのブラウザ終了回避）
                response = (
                    self.supabase_client.table("companies")
                    .select("id, company_name, form_url, instruction_json, company_url")
                    .eq("id", company_id)
                    .neq("form_url", None)
                    .ilike("form_url", "http%")
                    .limit(1)
                    .execute()
                )

                if not response.data:
                    logger.error(
                        f"Company with ID {company_id} not found or has no form_url"
                    )
                    return None

                company = response.data[0]
                logger.info("✅ Specific company selected:")

            else:
                logger.info(
                    "Fetching test form URL via threshold search (no SQL RANDOM)..."
                )

                min_id = int(cfg.get("min_company_id", 1))
                max_id = int(cfg.get("max_company_id", 536156))
                max_retries = int(cfg.get("max_retries", 10))
                scheme = str(cfg.get("form_url_scheme", "http%"))

                # 安全ガード
                if min_id < 1:
                    min_id = 1
                if max_id < min_id:
                    max_id = min_id

                company = None
                for attempt in range(1, max_retries + 1):
                    threshold = random.randint(min_id, max_id)
                    logger.info(
                        f"Attempt {attempt}/{max_retries} with threshold >= {threshold}"
                    )

                    # 条件: form_url が指定スキーマ始まり、id >= threshold の中で最小の id を 1 件
                    response = (
                        self.supabase_client.table("companies")
                        .select(
                            "id, company_name, form_url, instruction_json, company_url"
                        )
                        .neq("form_url", None)
                        .ilike("form_url", scheme)
                        .gte("id", threshold)
                        .order("id", desc=False)
                        .limit(1)
                        .execute()
                    )

                    if response.data:
                        company = response.data[0]
                        break

                    logger.info(
                        "No match found for this threshold, retrying with a new threshold..."
                    )

                if not company:
                    logger.error(
                        "Failed to find any company with http form_url after retries"
                    )
                    return None

                logger.info(
                    "✅ Test company selected via threshold search",
                    extra={"summary": True},
                )

            logger.info(
                f"🎯 Target company_id: {company['id']}", extra={"summary": True}
            )
            # 会社名・URLなどは quiet では非表示（必要なら --verbose/--debug）
            logger.info(f"   Company: ***COMPANY_REDACTED***")
            logger.info(f"   Form URL: ***URL_REDACTED***")
            logger.info(
                f"   Has instruction: {'Yes' if company.get('instruction_json') else 'No'}"
            )

            return company

        except Exception as e:
            logger.error(f"❌ Failed to fetch test form URL: {e}")
            return None

    async def _analyze_form_mapping_once(
        self, form_url: str
    ) -> Tuple[Dict[str, Any], str]:
        """フォームマッピング解析実行（単回実行）"""
        logger.info(f"Starting form mapping analysis...", extra={"summary": True})
        logger.info(f"Target URL: ***URL_REDACTED***")

        try:
            # Step 1: ナビゲーション（ポップアップ/セルフクローズに強い実装）
            await self._goto_with_popup_recovery(form_url)

            # DOM安定化とCookie同意処理
            await self._stabilize_after_navigation()

            # Step 2: 初期検出（form数/HubSpot検出）
            form_count, has_hubspot_script = await self._detect_initial_form_and_hubspot()

            # Step 3: 必要に応じて動的待機の実施
            form_count = await self._maybe_wait_dynamic_and_log(
                form_count, has_hubspot_script
            )

            # Step 3.5: エラー/確認ページからの簡易リカバリ（入力欄が無い場合）
            try:
                recovered = await self._recover_from_error_like_page()
                if recovered:
                    # リカバリ後のフォーム数を再評価
                    form_count = await self.page.evaluate("document.querySelectorAll('form').length")
                    logger.info(f"📋 Form elements after recovery: {form_count}")
            except Exception as e:
                logger.debug(f"error-like recovery skipped: {e}")

            # Step 4: フォームHTML抽出＋iframe検査
            form_content, target_frame = await self._extract_form_content_with_iframes(
                form_count
            )

            # Step 5: ページソース保存
            source_file = await self._save_form_content(form_content)

            # Step 6: RuleBasedAnalyzerでフィールドマッピング実行
            if target_frame:
                analyzer = RuleBasedAnalyzer(target_frame)  # iframe内を解析
                logger.info("📋 Analyzing iframe content for field mapping")
            else:
                analyzer = RuleBasedAnalyzer(self.page)  # 通常のページを解析
                logger.info("📋 Analyzing main page content for field mapping")

            analysis_result = await analyzer.analyze_form(
                client_data=create_test_client_config()
            )

            # HTMLから必須フィールド情報を抽出
            # 必須フィールドを抽出（厳密化）
            required_fields_info = self._extract_required_fields(form_content)
            analysis_result["required_fields_info"] = required_fields_info

            return analysis_result, source_file

        except Exception as e:
            logger.error(f"❌ Form mapping analysis failed: {e}")
            raise

    async def analyze_form_mapping(self, form_url: str) -> Tuple[Dict[str, Any], str]:
        """フォームマッピング解析実行（ページ/ブラウザが閉じられた場合の自動リトライ対応）"""
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                return await self._analyze_form_mapping_once(form_url)
            except Exception as e:
                last_error = e
                if "has been closed" in str(e):
                    logger.info(
                        "Detected unexpected page/context/browser close. Recreating page and retrying once..."
                    )
                    await self._recreate_page()
                    continue
                raise
        # 再試行しても失敗した場合
        raise last_error if last_error else RuntimeError("Form mapping analysis failed")

    def analyze_mapping_results(
        self, analysis_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """マッピング結果の詳細分析"""
        logger.info("\n" + "=" * 80)
        logger.info("FIELD MAPPING ANALYSIS RESULTS")
        logger.info("=" * 80)

        field_mappings = analysis_result.get("field_mapping", {})
        total_fields = len(field_mappings)

        logger.info(f"📊 Total mapped fields: {total_fields}")

        if total_fields == 0:
            logger.warning("⚠️  No fields were mapped!")
            return {"total_fields": 0, "issues": ["no_fields_mapped"]}

        # 詳細なマッピング結果を表示
        issues = []
        field_analysis = {}

        for field_name, field_info in field_mappings.items():
            analysis_entry, field_issues = self._log_field_details_and_collect_issues(
                field_name, field_info
            )
            if field_issues:
                issues.extend(field_issues)
            field_analysis[field_name] = analysis_entry

        # form_sender_name使用チェック
        if "form_sender_name" in field_mappings or any(
            "form_sender_name" in str(info) for info in field_mappings.values()
        ):
            issues.append("deprecated_form_sender_name_used")
            logger.warning("⚠️  Deprecated form_sender_name detected!")

        # 重複値チェック
        value_counts = {}
        for field_name, field_info in field_mappings.items():
            value = field_info.get("value", "")
            if value and value.strip():
                value_counts[value] = value_counts.get(value, 0) + 1

        duplicates = {v: count for v, count in value_counts.items() if count > 1}
        if duplicates:
            # メールアドレス確認フィールドを除く重複をチェック
            non_email_duplicates = {
                v: count
                for v, count in duplicates.items()
                if not self._is_email_confirmation_value(v)
            }
            if non_email_duplicates:
                issues.append("duplicate_values_found")
                # 値はログに出さない（個人情報保護）。件数のみ通知。
                logger.warning(
                    f"⚠️  Non-email duplicate values found (count={len(non_email_duplicates)})",
                    extra={"summary": True},
                )

        # 必須フィールドカバレッジは動的検出された情報を使用
        # （詳細な評価はfield-mapping-evaluatorエージェントが実行）
        required_info = analysis_result.get("required_fields_info", {})
        if required_info and not required_info.get("error"):
            required_count = required_info.get("required_fields_count", 0)
            logger.info(f"📋 Required fields detected: {required_count} fields")

            # 必須フィールドの簡単な一覧表示（詳細評価はエージェントに委譲）
            for req_element in required_info.get("required_elements", []):
                label = req_element.get(
                    "label_text",
                    req_element.get("placeholder", req_element.get("name", "N/A")),
                )
                logger.info(f"   - Required: {label}")

        logger.info(f"\n📋 Basic Analysis Summary:")
        logger.info(f"   Total mapped fields: {total_fields}")
        logger.info(f"   Basic issues found: {len(issues)}")
        if issues:
            logger.info(f"   Issue types: {', '.join(set(issues))}")
        logger.info(
            "   ℹ️  Detailed evaluation will be performed by field-mapping-evaluator agent"
        )

        return {
            "total_fields": total_fields,
            "issues": issues,
            "field_analysis": field_analysis,
            "duplicates": duplicates,
            "required_fields_info": required_info,
        }

    # --- Helper methods (extracted; no behavior change) ---

    async def _goto_with_popup_recovery(self, form_url: str) -> None:
        """`page.goto` 実行時のセルフクローズ/ポップアップ遷移を安全に吸収する。"""
        popup_captured: List[Page] = []

        def _on_popup(p):
            try:
                popup_captured.append(p)
                logger.debug("Popup captured during navigation")
            except Exception:
                pass

        self.page.once("popup", _on_popup)

        try:
            await self.page.goto(form_url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:
            if "has been closed" in str(e) and popup_captured:
                try:
                    candidate = popup_captured[-1]
                    is_closed = False
                    try:
                        if hasattr(candidate, "is_closed"):
                            is_closed = bool(candidate.is_closed())
                    except Exception:
                        is_closed = False

                    if not is_closed:
                        self.page = candidate
                        logger.info("Detected self-close -> switched to popup page")
                    else:
                        logger.info(
                            "Captured popup already closed. Recreating page and retrying..."
                        )
                        await self._recreate_page()
                        await self.page.goto(
                            form_url, wait_until="domcontentloaded", timeout=25000
                        )
                finally:
                    popup_captured.clear()
            elif "has been closed" in str(e):
                logger.info(
                    "Detected unexpected page close. Recreating page and retrying once..."
                )
                await self._recreate_page()
                await self.page.goto(
                    form_url, wait_until="domcontentloaded", timeout=25000
                )
            else:
                raise

    async def _stabilize_after_navigation(self) -> None:
        """DOM安定化待機とCookie同意処理。"""
        await asyncio.sleep(0.5)  # 500msの最小待機でDOM安定化
        await CookieConsentHandler.handle(self.page)

    async def _detect_initial_form_and_hubspot(self) -> Tuple[int, bool]:
        """初期フォーム数とHubSpotスクリプト検出。ログ出力含む。"""
        form_count = await self.page.evaluate("document.querySelectorAll('form').length")
        logger.info(f"📋 Initial form elements found: {form_count}")

        has_hubspot_script = await self.page.evaluate(
            """
            () => {
                const scripts = Array.from(document.querySelectorAll('script'));
                return scripts.some(script => 
                    script.src && (script.src.includes('hsforms.net') || script.src.includes('hubspot'))
                );
            }
            """
        )

        if has_hubspot_script:
            logger.info("🔍 HubSpot forms script detected - applying specialized handling")

        return form_count, bool(has_hubspot_script)

    async def _maybe_wait_dynamic_and_log(
        self, form_count: int, has_hubspot_script: bool
    ) -> int:
        """必要時のみ動的待機。待機後のform数およびHubSpot要素をログ。"""
        if form_count == 0 or has_hubspot_script:
            if form_count == 0:
                logger.info(
                    "No form elements found with domcontentloaded, trying additional strategies..."
                )
            else:
                logger.info("HubSpot detected - ensuring complete form loading...")

            success = await self._wait_for_dynamic_content()
            if success:
                form_count = await self.page.evaluate(
                    "document.querySelectorAll('form').length"
                )
                logger.info(
                    f"📋 Form elements found after dynamic waiting: {form_count}"
                )

                if has_hubspot_script:
                    hubspot_info = await self.page.evaluate(
                        """
                        () => {
                            const hbsptForms = document.querySelectorAll('.hbspt-form').length;
                            const hsInputs = document.querySelectorAll('.hs-input').length;
                            const hsFieldsets = document.querySelectorAll('fieldset.form-columns-1, fieldset.form-columns-2').length;
                            return {hbsptForms, hsInputs, hsFieldsets};
                        }
                        """
                    )
                    logger.info(
                        f"📋 HubSpot elements: containers={hubspot_info['hbsptForms']}, inputs={hubspot_info['hsInputs']}, fieldsets={hubspot_info['hsFieldsets']}"
                    )
        else:
            # 追加戦略: form は存在するが input/textarea/select が 0 の場合、
            # 動的生成を考慮して待機を試行する（例: 外部プラットフォーム埋め込み等）。
            try:
                inputs_total = await self.page.evaluate(
                    "document.querySelectorAll('input, textarea, select').length"
                )
            except Exception:
                inputs_total = 0
            if int(inputs_total or 0) == 0:
                logger.info(
                    "Forms present but no inputs detected; waiting for dynamic content..."
                )
                success = await self._wait_for_dynamic_content()
                if success:
                    form_count = await self.page.evaluate(
                        "document.querySelectorAll('form').length"
                    )
                    logger.info(
                        f"📋 Elements found after dynamic waiting: forms={form_count}"
                    )
        return form_count

    async def _recover_from_error_like_page(self) -> bool:
        """入力欄が見当たらない『エラー/確認/完了』風ページからの簡易リカバリ（汎用）。"""
        try:
            inputs_visible = await self.page.evaluate(
                """
                () => Array.from(document.querySelectorAll('input, textarea, select')).filter(el => {
                  const type = (el.getAttribute('type')||'').toLowerCase();
                  if (['hidden','submit','button','image'].includes(type)) return false;
                  const rect = el.getBoundingClientRect();
                  const visible = rect && rect.width > 0 && rect.height > 0;
                  const style = window.getComputedStyle(el);
                  return visible && style.visibility !== 'hidden';
                }).length
                """
            )
        except Exception:
            inputs_visible = 0

        if int(inputs_visible or 0) > 0:
            return False

        # 本文中の典型語で判定を補強
        try:
            body_text = await self.page.evaluate(
                "() => (document.body.innerText||'').toLowerCase()"
            )
        except Exception:
            body_text = ""
        tokens = ["未入力", "エラー", "戻る", "前画面", "確認画面", "error", "back"]
        if sum(1 for t in tokens if t in (body_text or "")) < 2:
            return False

        # 戻る/前画面系のUIを試行
        selectors = [
            "text=戻る",
            "text=前画面",
            "text=前の画面",
            "text=Back",
            "a:has-text('戻る')",
            "a:has-text('前画面')",
            "button:has-text('戻る')",
            "input[type=button][value*='戻る']",
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel)
                if await el.count():
                    await el.first.click(timeout=2000)
                    try:
                        await self.page.wait_for_load_state('domcontentloaded', timeout=5000)
                    except Exception:
                        pass
                    logger.info("🔁 Recovered from error-like page via UI", extra={"summary": True})
                    return True
            except Exception:
                continue

        # 最後の手段: history.back()
        try:
            await self.page.evaluate("history.back()")
            try:
                await self.page.wait_for_load_state('domcontentloaded', timeout=5000)
            except Exception:
                pass
            logger.info("🔁 Recovered from error-like page via history.back()", extra={"summary": True})
            # まだ入力欄が無い場合は2ステップ戻る/リファラ遷移も試みる
            try:
                inputs_visible2 = await self.page.evaluate(
                    "() => Array.from(document.querySelectorAll('input, textarea, select')).filter(el => (el.getAttribute('type')||'').toLowerCase()!=='hidden').length"
                )
            except Exception:
                inputs_visible2 = 0
            if int(inputs_visible2 or 0) > 0:
                return True
            # 追加の戻る
            try:
                await self.page.evaluate("history.go(-1)")
                await self.page.wait_for_load_state('domcontentloaded', timeout=5000)
            except Exception:
                pass
            try:
                inputs_visible3 = await self.page.evaluate(
                    "() => Array.from(document.querySelectorAll('input, textarea, select')).filter(el => (el.getAttribute('type')||'').toLowerCase()!=='hidden').length"
                )
            except Exception:
                inputs_visible3 = 0
            if int(inputs_visible3 or 0) > 0:
                return True
            # リファラへ遷移
            try:
                await self.page.evaluate("if (document.referrer) location.href = document.referrer;")
                await self.page.wait_for_load_state('domcontentloaded', timeout=5000)
                return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    async def _extract_form_content_with_iframes(
        self, form_count: int
    ) -> Tuple[str, Optional[Any]]:
        """フォームHTML抽出＋必要ならiframeも解析し結合。"""
        page_source = await self.page.content()
        form_content = self._extract_form_content(page_source)

        target_frame = None
        # フォームが無い、またはフォームはあるが入力欄が0の場合は iframe も確認
        need_iframe_check = False
        if form_count == 0:
            need_iframe_check = True
        else:
            try:
                inputs_total = await self.page.evaluate(
                    "document.querySelectorAll('input, textarea, select').length"
                )
            except Exception:
                inputs_total = 0
            if int(inputs_total or 0) == 0:
                need_iframe_check = True

        if need_iframe_check:
            if form_count == 0:
                logger.info("🔍 No forms found in main page, checking iframes...")
            else:
                logger.info("🔍 Forms exist but no inputs found; checking iframes...")
            iframe_content, target_frame = await self._analyze_iframes()
            if iframe_content:
                form_content += "\n\n" + iframe_content

        return form_content, target_frame

    async def _save_form_content(self, form_content: str) -> str:
        """抽出したフォームHTMLをテスト用一時ディレクトリに保存。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        source_file = os.path.join(self.temp_dir, f"page_source_{timestamp}.html")
        with open(source_file, "w", encoding="utf-8") as f:
            f.write(form_content)
        logger.info(f"📄 Form content saved: {source_file}", extra={"summary": True})
        return source_file

    def _log_field_details_and_collect_issues(
        self, field_name: str, field_info: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[str]]:
        """フィールド詳細ログ＋問題収集。元実装と同一出力・同一判定。"""
        logger.info(f"\n🎯 Field: {field_name}")

        input_value = field_info.get("input_value", "N/A")
        score = field_info.get("score", 0)
        element = field_info.get("element", {})

        element_type_name = str(type(element))
        element_str = str(element)
        logger.debug(
            f"Element type: {element_type_name}, Element str: {element_str[:100]}..."
        )

        if "Locator" in element_type_name or "Locator" in element_str:
            element_name = element_str
            element_id = "Locator-Object"
            element_type = "Locator"
            selector = element_str
        elif isinstance(element, dict):
            element_name = element.get("name", "N/A")
            element_id = element.get("id", "N/A")
            element_type = element.get("type", "N/A")
            selector = element.get("selector", "N/A")
        else:
            element_name = element_str
            element_id = "Unknown"
            element_type = (
                element_type_name.split("'")[1] if "'" in element_type_name else "Unknown"
            )
            selector = element_str

        logger.info(f"   Input Value: '{input_value}'")
        logger.info(f"   Score: {score}")
        logger.info(f"   Element Type: {element_type}")
        logger.info(f"   Selector: {selector}")

        if "Locator" in element_type:
            if "selector=" in element_str:
                try:
                    selector_start = element_str.find("selector='") + len("selector='")
                    selector_end = element_str.find("'>", selector_start)
                    if selector_end > selector_start:
                        extracted_selector = element_str[selector_start:selector_end]
                        logger.info(f"   Extracted Selector: {extracted_selector}")
                except Exception:
                    pass
            logger.info(f"   Full Locator: {element_str}")
        else:
            logger.info(f"   Target Element: name='{element_name}', id='{element_id}'")

        field_issues = self._check_field_issues(field_name, field_info)
        analysis_entry = {
            "value": field_info.get("value", ""),
            "score": field_info.get("score", 0),
            "issues": field_issues,
        }
        return analysis_entry, field_issues

    def _check_field_issues(
        self, field_name: str, field_info: Dict[str, Any]
    ) -> List[str]:
        """個別フィールドの問題チェック"""
        issues = []
        value = field_info.get("value", "")
        score = field_info.get("score", 0)

        # 低スコアチェック
        if score < 15:
            issues.append("low_confidence_score")

        # 不適切な値の例（五十嵐問題等）
        if field_name in ["会社名", "company_name"] and any(
            name in value for name in ["五十嵐", "駿", "いがらし", "しゅん"]
        ):
            issues.append("name_in_company_field")

        if (
            field_name in ["姓", "名", "last_name", "first_name"]
            and "株式会社" in value
        ):
            issues.append("company_in_name_field")

        return issues

    def _is_email_confirmation_value(self, value: str) -> bool:
        """メールアドレス確認値かどうかチェック"""
        return "@" in value and "neurify.jp" in value.lower()

    def _make_json_serializable(self, obj: Any) -> Any:
        """JSONシリアライゼーション可能なオブジェクトに変換"""
        if isinstance(obj, dict):
            return {k: self._make_json_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._make_json_serializable(item) for item in obj]
        elif hasattr(obj, "__dict__"):
            # Locatorなどのオブジェクトは文字列表現に変換
            return str(obj)
        else:
            return obj

    # フォーム要素表示メソッドは削除（page_sourceファイルで代替）

    def _log_memory_usage(self, phase: str):
        """メモリ使用量ログ"""
        try:
            import psutil

            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            logger.info(f"💾 Memory usage ({phase}): {memory_mb:.1f} MB")
        except ImportError:
            logger.debug("psutil not available for memory monitoring")
        except Exception:
            pass

    async def run_single_test(self, company_id: Optional[int] = None) -> bool:
        """単一テスト実行"""
        self._log_memory_usage("start")

        try:
            # テスト対象企業を取得
            test_company = await self.fetch_test_form_url(company_id)
            if not test_company:
                logger.error("No test company available")
                return False

            form_url = test_company["form_url"]

            # フォームマッピング解析実行
            self._log_memory_usage("before_analysis")
            analysis_result, source_file = await self.analyze_form_mapping(form_url)
            self._log_memory_usage("after_analysis")

            # 結果分析
            analysis_summary = self.analyze_mapping_results(analysis_result)

            # フォーム要素表示はスキップ（page_sourceファイルで十分）

            # 結果をJSONファイルに保存
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            result_file = os.path.join(
                self.temp_dir, f"analysis_result_{timestamp}.json"
            )

            # JSONシリアライゼーション用の結果を準備（Locatorオブジェクトを除外）
            serializable_result = self._make_json_serializable(analysis_result)

            result_data = {
                "company_id": test_company["id"],
                "form_url": form_url,
                "timestamp": timestamp,
                "analysis_result": serializable_result,
                "analysis_summary": analysis_summary,
                "source_file": source_file,
                "test_metadata": {
                    "company_id": test_company["id"],
                    "is_specific_id_test": test_company["id"] is not None,
                    "test_timestamp": timestamp,
                },
            }

            with open(result_file, "w", encoding="utf-8") as f:
                json.dump(result_data, f, ensure_ascii=False, indent=2)

            logger.info(
                f"\n💾 Analysis result saved: {result_file}", extra={"summary": True}
            )
            logger.info(f"📄 Page source saved: {source_file}", extra={"summary": True})

            self._log_memory_usage("end")

            # 改善提案は自動化エージェントに委譲

            return True

        except Exception as e:
            logger.error(f"❌ Single test execution failed: {e}")
            return False

    def _extract_required_fields(self, form_content: str) -> Dict[str, Any]:
        """HTMLから必須フィールド情報を動的に抽出"""
        try:
            if not HAS_BEAUTIFULSOUP:
                return {
                    "error": "BeautifulSoup not available for required field detection"
                }

            soup = BeautifulSoup(form_content, "html.parser")
            required_elements = []

            # 0. hiddenフィールドに含まれるバリデーションヒントを検出
            # 例: <input type="hidden" name="F2M_CHECK_01" value="NAME,お名前は必須です">
            hinted_names_upper = []
            try:
                for h in soup.find_all("input", {"type": "hidden"}):
                    name_attr = (h.get("name") or "").upper()
                    value_attr = (h.get("value") or "")
                    if any(k in name_attr for k in ["F2M_CHECK", "REQ_CHECK", "REQUIRED_CHECK", "VALIDATE_"]):
                        if "," in value_attr:
                            candidate = value_attr.split(",", 1)[0].strip()
                            if 0 < len(candidate) <= 64:
                                hinted_names_upper.append(candidate.upper())
            except Exception:
                pass
            # 重複排除
            hinted_names_upper = list(dict.fromkeys(hinted_names_upper))

            # required属性、aria-required="true"、クラス名、隣接要素をチェック
            for element in soup.find_all(["input", "textarea", "select"]):
                is_required = False

                # 1. required属性のチェック
                if element.get("required") is not None:
                    is_required = True

                # 2. aria-required属性のチェック
                elif element.get("aria-required") == "true":
                    is_required = True

                # 3. クラス名による必須判定
                elif self._check_required_by_class(element):
                    is_required = True

                # 4. 隣接要素の必須マーカーチェック
                elif self._check_required_by_adjacent_text(element):
                    is_required = True

                # 5. hiddenヒント（名前一致）
                elif hinted_names_upper:
                    try:
                        elem_name = (element.get("name") or "").strip()
                        if elem_name and elem_name.upper() in hinted_names_upper:
                            is_required = True
                    except Exception:
                        pass

                if is_required:
                    element_info = {
                        "tag": element.name,
                        "name": element.get("name", ""),
                        "id": element.get("id", ""),
                        "type": element.get("type", ""),
                        "placeholder": element.get("placeholder", ""),
                        "class": " ".join(element.get("class", [])),
                    }
                    required_elements.append(element_info)

            # ラベルテキストも抽出
            for req_element in required_elements:
                element_id = req_element.get("id")
                element_name = req_element.get("name")

                # label要素からテキストを取得
                label_text = ""
                if element_id:
                    label = soup.find("label", {"for": element_id})
                    if label:
                        label_text = label.get_text(strip=True)

                # placeholder がある場合はそれも含める
                if req_element.get("placeholder"):
                    if label_text:
                        label_text += f" ({req_element['placeholder']})"
                    else:
                        label_text = req_element["placeholder"]

                req_element["label_text"] = label_text

            return {
                "required_fields_count": len(required_elements),
                "required_elements": required_elements,
                "detection_method": "comprehensive_required_detection",
            }

        except Exception as e:
            return {"error": f"Required field extraction failed: {e}"}

    def _check_required_by_class(self, element) -> bool:
        """クラス名による必須判定"""
        class_attr = element.get("class", [])
        if isinstance(class_attr, list):
            class_names = " ".join(class_attr).lower()
        else:
            class_names = str(class_attr).lower()

        # 必須を示すクラス名パターン
        required_class_patterns = [
            "fldrequired",  # CFormsプラグイン
            "wpcf7-validates-as-required",  # Contact Form 7
            "required",
            "mandatory",
            "must",
        ]

        return any(pattern in class_names for pattern in required_class_patterns)

    def _check_required_by_adjacent_text(self, element) -> bool:
        """隣接要素の必須マーカーチェック"""
        try:
            # 次の兄弟要素をチェック
            next_sibling = element.next_sibling
            while next_sibling:
                # <img alt="必須"> に対応
                try:
                    if getattr(next_sibling, 'name', '') == 'img':
                        alt = (next_sibling.get('alt') or '').strip()
                        if any(m in alt for m in ["必須", "Required", "Mandatory"]):
                            return True
                except Exception:
                    pass
                if hasattr(next_sibling, "get_text"):
                    text = next_sibling.get_text().strip()
                    # ラベル近傍では『※』が必須記号として使われることが多い。
                    # ただし注記との混同を避けるため、短いテキストに限定して許可する。
                    if any(marker in text for marker in ["必須", "Required", "Mandatory", "*", "＊"]):
                        return True
                    if "※" in text and len(text) <= 10:
                        return True
                next_sibling = next_sibling.next_sibling

            # 親要素内の他の子要素もチェック
            if element.parent:
                for sibling in element.parent.find_all(["span", "label", "div", "img"]):
                    if sibling != element:
                        if getattr(sibling, 'name', '') == 'img':
                            try:
                                alt = (sibling.get('alt') or '').strip()
                                if any(m in alt for m in ["必須", "Required", "Mandatory"]):
                                    return True
                            except Exception:
                                pass
                        else:
                            text = sibling.get_text().strip()
                            if (any(marker in text for marker in ["必須", "Required", "Mandatory"]) or "※" in text) and len(text) <= 10:
                                return True

            # テーブルレイアウト対応: td内のinputに対して直前のthを確認
            try:
                td = element
                # 祖先方向に辿って td を探す
                while td and getattr(td, "name", "") != "td":
                    td = td.parent
                if td and td.parent:
                    # 同じ tr 内の直前 th を探す
                    prev = td.find_previous_sibling("th")
                    if prev:
                        th_text = prev.get_text().strip()
                        if any(marker in th_text for marker in ["必須", "Required", "Mandatory", "＊", "*", "※"]):
                            return True
                        # th 内の画像 alt でも必須を検出
                        try:
                            for img in prev.find_all("img"):
                                alt = (img.get('alt') or '').strip()
                                if any(m in alt for m in ["必須", "Required", "Mandatory"]):
                                    return True
                        except Exception:
                            pass
            except Exception:
                pass

            return False

        except Exception:
            return False

    async def _wait_for_dynamic_content(self, max_wait: int = 15) -> bool:
        """動的コンテンツを段階的に待機（HubSpot対応強化）"""
        logger.info("🔄 Waiting for dynamic content...")

        # 戦略1: HubSpotフォーム特有のセレクタチェック
        try:
            logger.info("Strategy 1: HubSpot form detection...")
            hubspot_selectors = [
                ".hbspt-form form",
                'form[id^="hsForm_"]',
                "div[data-hs-forms-root] form",
                ".hs-form",
                'div[id^="hbspt-form-"] form',
            ]

            for selector in hubspot_selectors:
                try:
                    count = await self.page.evaluate(
                        f'document.querySelectorAll("{selector}").length'
                    )
                    if count > 0:
                        logger.info(
                            f"✅ HubSpot form detected with selector '{selector}': {count} forms"
                        )
                        return True
                except Exception:
                    continue

            # HubSpotスクリプトの存在チェック
            has_hubspot_script = await self.page.evaluate("""
                () => {
                    const scripts = Array.from(document.querySelectorAll('script'));
                    return scripts.some(script => 
                        script.src && (script.src.includes('hsforms.net') || script.src.includes('hubspot'))
                    );
                }
            """)

            if has_hubspot_script:
                logger.info("HubSpot script detected, applying extended wait...")

        except Exception as e:
            logger.debug(f"HubSpot detection failed: {e}")

        # 戦略2: 拡張networkidle待機（HubSpot対応）
        try:
            logger.info("Strategy 2: Extended networkidle wait...")
            await self.page.wait_for_load_state("networkidle", timeout=15000)

            # フォーム要素をチェック
            form_count = await self.page.evaluate(
                "document.querySelectorAll('form').length"
            )
            if form_count > 0:
                logger.info(
                    f"✅ Form elements found after extended networkidle: {form_count}"
                )
                return True

            # HubSpot特有の要素をチェック
            hubspot_elements = await self.page.evaluate("""
                () => {
                    const hbsptForms = document.querySelectorAll('.hbspt-form').length;
                    const hsInputs = document.querySelectorAll('.hs-input').length;
                    const hsFieldsets = document.querySelectorAll('fieldset.form-columns-1, fieldset.form-columns-2').length;
                    return {hbsptForms, hsInputs, hsFieldsets};
                }
            """)

            total_hubspot = (
                hubspot_elements["hbsptForms"]
                + hubspot_elements["hsInputs"]
                + hubspot_elements["hsFieldsets"]
            )
            if total_hubspot > 0:
                logger.info(
                    f"🔍 HubSpot elements found: containers={hubspot_elements['hbsptForms']}, inputs={hubspot_elements['hsInputs']}, fieldsets={hubspot_elements['hsFieldsets']}"
                )
                # HubSpotコンテナがあっても、実際のinput要素がない場合は継続して待機
                if (
                    hubspot_elements["hsInputs"] > 0
                    or hubspot_elements["hsFieldsets"] > 0
                ):
                    logger.info("✅ HubSpot input elements detected, form is ready")
                    return True
                else:
                    logger.info(
                        "HubSpot containers found but no input elements yet - continuing to Strategy 3..."
                    )

            # input要素があるかチェック（formタグ無しのフォーム対応）
            input_count = await self.page.evaluate(
                "document.querySelectorAll('input[type=text], input[type=email], textarea, input[type=radio], input[type=checkbox]').length"
            )
            if input_count > 2:  # 最低3つのinput要素が必要
                logger.info(
                    f"✅ Multiple input elements found without form tag: {input_count}"
                )
                return True

        except Exception as e:
            logger.debug(f"Extended networkidle wait failed: {e}")

        # 戦略3: JavaScript実行待機（条件付き強化）
        try:
            logger.info("Strategy 3: JavaScript execution wait...")

            # HubSpotが検出されている場合のみ、より厳密な待機を行う
            has_hubspot = await self.page.evaluate("""
                () => {
                    const scripts = Array.from(document.querySelectorAll('script'));
                    return scripts.some(script => 
                        script.src && script.src.includes('hsforms.net')
                    );
                }
            """)

            if has_hubspot:
                logger.info(
                    "HubSpot detected - applying enhanced form generation wait..."
                )

                # HubSpot専用の段階的待機
                for attempt in range(4):  # 最大4回試行（5秒x4 = 20秒）
                    await asyncio.sleep(5)  # 5秒待機

                    # メインページのフォーム要素チェック
                    form_status = await self.page.evaluate("""
                        () => {
                            const forms = document.querySelectorAll('form').length;
                            const hsFormSpecific = document.querySelectorAll('form[id^="hsForm_"]').length;
                            const hsInputs = document.querySelectorAll('.hs-input').length;
                            const hsFieldsets = document.querySelectorAll('fieldset.form-columns-1, fieldset.form-columns-2').length;
                            const allInputs = document.querySelectorAll('input').length;
                            
                            // HubSpotコンテナ内のform要素もチェック
                            let hbsptInnerForms = 0;
                            document.querySelectorAll('.hbspt-form').forEach(container => {
                                hbsptInnerForms += container.querySelectorAll('form').length;
                            });
                            
                            return {forms, hsFormSpecific, hsInputs, hsFieldsets, allInputs, hbsptInnerForms};
                        }
                    """)

                    # iframe内のフォーム要素チェック
                    iframe_form_count = 0
                    iframe_input_count = 0
                    try:
                        frames = self.page.frames
                        for frame in frames:
                            if (
                                frame != self.page.main_frame
                            ):  # メインフレーム以外をチェック
                                frame_forms = await frame.evaluate(
                                    "document.querySelectorAll('form').length"
                                )
                                frame_inputs = await frame.evaluate(
                                    "document.querySelectorAll('input').length"
                                )
                                iframe_form_count += frame_forms
                                iframe_input_count += frame_inputs
                                if frame_forms > 0:
                                    logger.info(
                                        f"iframe detected forms: {frame_forms} in frame: {frame.url}"
                                    )
                    except Exception as e:
                        logger.debug(f"iframe access error: {e}")

                    total_forms = form_status["forms"] + iframe_form_count
                    total_inputs = form_status["allInputs"] + iframe_input_count

                    logger.info(
                        f"HubSpot attempt {attempt+1}: main_forms={form_status['forms']}, iframe_forms={iframe_form_count}, hsInputs={form_status['hsInputs']}, hsFieldsets={form_status['hsFieldsets']}, total_inputs={total_inputs}"
                    )

                    # 成功条件：実際のform要素またはinput要素が十分数存在
                    if (
                        total_forms > 0
                        or form_status["hsInputs"] > 3
                        or form_status["hsFieldsets"] > 0
                        or total_inputs > 5
                    ):
                        logger.info(
                            f"✅ HubSpot form elements fully loaded on attempt {attempt+1} (forms: {total_forms}, inputs: {total_inputs})"
                        )
                        return True

                # HubSpot用のiframe処理
                iframe_count = await self.page.evaluate(
                    "document.querySelectorAll('iframe.hs-form-iframe').length"
                )
                if iframe_count > 0:
                    logger.info(
                        f"HubSpot iframe detected: {iframe_count}, applying additional wait..."
                    )
                    await asyncio.sleep(3)  # iframe読み込み用の追加待機

            else:
                # 通常のフォーム用の軽量な待機（既存処理を維持）
                await self.page.wait_for_function(
                    """() => {
                        const forms = document.querySelectorAll('form');
                        const inputs = document.querySelectorAll('input[type="text"], input[type="email"], textarea');
                        return forms.length > 0 || inputs.length > 3;
                    }""",
                    timeout=8000,
                )

            # 最終的な要素チェック
            final_check = await self.page.evaluate("""
                () => {
                    const forms = document.querySelectorAll('form').length;
                    const hbsptForms = document.querySelectorAll('.hbspt-form').length;
                    const inputs = document.querySelectorAll('input').length;
                    const textareas = document.querySelectorAll('textarea').length;
                    const selects = document.querySelectorAll('select').length;
                    const hsInputs = document.querySelectorAll('.hs-input').length;
                    return {forms, hbsptForms, inputs, textareas, selects, hsInputs};
                }
            """)

            total_elements = (
                final_check["forms"]
                + final_check["inputs"]
                + final_check["textareas"]
                + final_check["selects"]
            )
            logger.info(
                f"JavaScript wait results: forms={final_check['forms']}, hbspt={final_check['hbsptForms']}, inputs={final_check['inputs']}, hsInputs={final_check['hsInputs']}, textareas={final_check['textareas']}, selects={final_check['selects']}"
            )

            if total_elements > 0 or final_check["hsInputs"] > 0:
                logger.info(
                    f"✅ Form elements found via JavaScript wait: {total_elements} total (hsInputs: {final_check['hsInputs']})"
                )
                return True

        except Exception as e:
            logger.debug(f"JavaScript execution wait failed: {e}")

        # 戦略4: スクロールトリガー（最後の手段）
        try:
            logger.info("Strategy 4: Scroll trigger...")

            # ページをスクロールして遅延読み込みをトリガー
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            await self.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(2)

            # 最終チェック
            form_count = await self.page.evaluate(
                "document.querySelectorAll('form').length"
            )
            input_count = await self.page.evaluate(
                "document.querySelectorAll('input').length"
            )

            if form_count > 0 or input_count > 0:
                logger.info(
                    f"✅ Elements found after scroll: forms={form_count}, inputs={input_count}"
                )
                return True

        except Exception as e:
            logger.debug(f"Scroll trigger failed: {e}")

        logger.warning(
            "⚠️ No form elements found after all dynamic strategies (including HubSpot)"
        )
        return False

    async def _analyze_iframes(self) -> Tuple[str, Optional[Any]]:
        """iframe内のフォーム要素を抽出し、RuleBasedAnalyzer用のtarget_frameも決定（統合版）"""
        iframe_contents = []
        iframe_count = 0
        target_frame = None

        try:
            frames = self.page.frames
            for frame in frames:
                if frame != self.page.main_frame:  # メインフレーム以外をチェック
                    try:
                        # iframe内のフォーム要素を取得
                        frame_content = await frame.content()

                        # 実際のフォーム要素存在をチェック
                        frame_forms = await frame.query_selector_all("form")
                        has_forms = len(frame_forms) > 0

                        if not HAS_BEAUTIFULSOUP:
                            # BeautifulSoupがない場合の基本抽出
                            if "<form" in frame_content.lower():
                                iframe_count += 1
                                iframe_contents.append(
                                    f"<!-- iframe {iframe_count} Content from {frame.url} -->\n{frame_content}\n"
                                )
                                if not target_frame and has_forms:
                                    target_frame = frame
                        else:
                            # BeautifulSoupを使用した抽出
                            soup = BeautifulSoup(frame_content, "html.parser")
                            forms = soup.find_all("form")

                            if forms:
                                iframe_count += 1
                                iframe_contents.append(
                                    f"<!-- iframe {iframe_count} Forms from {frame.url} -->\n"
                                )
                                for i, form in enumerate(forms):
                                    iframe_contents.append(
                                        f"<!-- iframe {iframe_count} Form {i+1} -->\n{str(form)}\n"
                                    )

                                logger.info(
                                    f"Extracted {len(forms)} form(s) from iframe: {frame.url}"
                                )

                                # 最初に見つかったフォーム付きiframeをtarget_frameに設定
                                if not target_frame and has_forms:
                                    target_frame = frame

                            # iframe内のHubSpot要素も抽出
                            hubspot_elements = soup.find_all(
                                ["input", "textarea", "fieldset"],
                                class_=lambda x: x
                                and ("hs-" in str(x) if x else False),
                            )
                            if hubspot_elements and not forms:
                                # フォームタグがないがHubSpot要素がある場合
                                iframe_count += 1
                                body = soup.find("body") or soup
                                iframe_contents.append(
                                    f"<!-- iframe {iframe_count} HubSpot Elements from {frame.url} -->\n{str(body)}\n"
                                )
                                logger.info(
                                    f"Extracted HubSpot elements from iframe: {frame.url}"
                                )

                                # HubSpot要素があってフォームが実際に存在する場合はtarget_frameに設定
                                if not target_frame and has_forms:
                                    target_frame = frame

                    except Exception as e:
                        logger.debug(f"Failed to extract from iframe {frame.url}: {e}")
                        continue

        except Exception as e:
            logger.debug(f"iframe extraction failed: {e}")

        iframe_content_str = ""
        if iframe_contents:
            logger.info(f"Successfully extracted content from {iframe_count} iframe(s)")
            iframe_content_str = "\n".join(iframe_contents)

        if target_frame:
            logger.info(
                f"📋 Target iframe found for analysis: {len(await target_frame.query_selector_all('form'))} forms found"
            )

        return iframe_content_str, target_frame

    # 改善提案メソッドは削除（field-mapping-coderが自動判断）

    def _extract_form_content(self, page_html: str) -> str:
        """
        HTMLページソースから<form>要素の内容のみを抽出（HubSpot対応強化）

        Args:
            page_html: 完全なページHTML

        Returns:
            form要素のみを含むHTML文字列
        """
        if not HAS_BEAUTIFULSOUP:
            # BeautifulSoupがない場合のフォールバック - 基本的な<form>抽出
            logger.warning("BeautifulSoup not available, using basic form extraction")
            return self._extract_form_basic(page_html)

        try:
            soup = BeautifulSoup(page_html, "html.parser")
            form_elements = soup.find_all("form")

            # HubSpotフォームコンテナもチェック
            hubspot_containers = soup.find_all(
                ["div"], class_=["hbspt-form", "hs-form"]
            )
            hubspot_forms_by_id = soup.find_all(
                "div", id=lambda x: x and x.startswith("hbspt-form-")
            )

            # 抽出されるコンテンツ
            form_contents = []

            # 通常のform要素: 最も妥当な1件のみを選択して抽出（複数フォーム混在ページ対策）
            if form_elements:
                def _score_form(f) -> float:
                    # 要素内の統計
                    email = len(f.select('input[type="email"], input[type="mail"]'))
                    text = len(f.select('input[type="text"], input:not([type])'))
                    textarea = len(f.select('textarea'))
                    select = len(f.select('select'))
                    search = len(f.select('input[type="search"]'))
                    hidden = len(f.select('input[type="hidden"]'))
                    submit = len(f.select('input[type="submit"], button[type="submit"], button'))
                    # メタ/ボタン文言
                    action = (f.get('action') or '').lower()
                    klass = (f.get('class') or [])
                    klass_txt = ' '.join(klass).lower() if isinstance(klass, list) else str(klass).lower()
                    fid = (f.get('id') or '').lower()
                    role = (f.get('role') or '').lower()
                    btn_texts = ' '.join([
                        (b.get_text(strip=True) or '') for b in f.select('button')
                    ] + [
                        (b.get('value') or '') for b in f.select('input[type="submit"]')
                    ]).lower()

                    attr_text = f"{action} {klass_txt} {fid} {role} {btn_texts}"
                    # 連絡/問い合わせ/subscribe のキーワード
                    contact_keywords = ['contact', 'inquiry', 'お問い合わせ', '問い合わせ', 'toiawase', 'お問合せ', '問合せ']
                    negative_action_keywords = ['search', 'order', 'checkout', 'cart', 'unsubscribe', '解除', '配信停止', '退会', '削除']

                    score = 0.0
                    score += email * 3.0
                    score += textarea * 3.5
                    score += text * 1.5
                    score += select * 1.0
                    score += min(submit, 3) * 0.2
                    score -= search * 2.0
                    score -= min(hidden, 10) * 0.05
                    if any(k in attr_text for k in contact_keywords):
                        score += 5.0
                    # subscribe は微加点、unsubscribe/解除は強い減点
                    if 'subscribe' in attr_text or '登録' in attr_text:
                        score += 2.0
                    if any(k in attr_text for k in negative_action_keywords):
                        score -= 6.0
                    # 必須項目数（簡易）
                    try:
                        req = len(f.select('[required], [aria-required="true"], .wpcf7-validates-as-required'))
                        score += min(5.0, req * 0.5)
                    except Exception:
                        pass
                    return score

                # ベストフォームを選択
                best = None
                best_score = -1e9
                for form in form_elements:
                    try:
                        s = _score_form(form)
                    except Exception:
                        s = 0.0
                    if s > best_score:
                        best = form
                        best_score = s

                if best is not None:
                    form_contents.append(f"<!-- Selected Form (score={best_score:.2f}) -->\n{str(best)}\n")
                    logger.info(
                        f"Extracted 1 selected form element (score={best_score:.2f}) from page source"
                    )

            # HubSpotフォームコンテナを抽出
            hubspot_count = 0
            for container in hubspot_containers + hubspot_forms_by_id:
                if container not in [
                    elem.find_parent(["div"], class_=["hbspt-form", "hs-form"])
                    for elem in form_elements
                    if elem.find_parent(["div"], class_=["hbspt-form", "hs-form"])
                ]:
                    hubspot_count += 1
                    form_contents.append(
                        f"<!-- HubSpot Container {hubspot_count} -->\n{str(container)}\n"
                    )

            if hubspot_count > 0:
                logger.info(
                    f"Extracted {hubspot_count} HubSpot form container(s) from page source"
                )

            # 何も見つからない場合の処理
            if not form_contents:
                # input要素が多数ある場合、body全体を抽出（HubSpot未検出ケース対応）
                inputs = soup.find_all(["input", "textarea", "select"])
                if len(inputs) > 3:
                    logger.warning(
                        f"No form containers found, but {len(inputs)} input elements detected - extracting body"
                    )
                    body = soup.find("body")
                    if body:
                        return f"<!-- Full body content (multiple inputs detected) -->\n{str(body)}\n"

                logger.warning("No <form> elements or HubSpot containers found in page")
                return "<!-- No form elements found -->\n"

            extracted_html = "\n".join(form_contents)
            return extracted_html

        except Exception as e:
            logger.error(f"Form extraction failed: {e}")
            return self._extract_form_basic(page_html)

    def _extract_form_basic(self, page_html: str) -> str:
        """
        BeautifulSoup未使用時のフォールバック: 基本的なform抽出

        Args:
            page_html: 完全なページHTML

        Returns:
            form要素のみを含むHTML文字列（基本抽出版）
        """
        import re

        # 基本的な正規表現でform要素を抽出
        form_pattern = re.compile(r"<form[^>]*>.*?</form>", re.DOTALL | re.IGNORECASE)
        forms = form_pattern.findall(page_html)

        if not forms:
            logger.warning("No forms found with basic extraction")
            return "<!-- No form elements found with basic extraction -->\n"

        logger.info(f"Basic extraction found {len(forms)} form(s)")
        return "\n\n".join(
            f"<!-- Form {i+1} (basic extraction) -->\n{form}"
            for i, form in enumerate(forms)
        )

    async def cleanup(self):
        """リソースクリーンアップ - 強化版"""
        cleanup_errors = []

        # ページクリーンアップ
        if self.page:
            try:
                logger.debug("Closing page...")
                await asyncio.wait_for(self.page.close(), timeout=5.0)
                self.page = None
            except Exception as e:
                cleanup_errors.append(f"Page cleanup error: {e}")
                self.page = None  # 強制的にクリア

        # ブラウザクリーンアップ
        if self.browser:
            try:
                logger.debug("Closing browser...")
                await asyncio.wait_for(self.browser.close(), timeout=10.0)
                self.browser = None
            except Exception as e:
                cleanup_errors.append(f"Browser cleanup error: {e}")
                self.browser = None  # 強制的にクリア

        # Playwrightクリーンアップ
        if self.playwright:
            try:
                logger.debug("Stopping playwright...")
                await asyncio.wait_for(self.playwright.stop(), timeout=5.0)
                self.playwright = None
            except Exception as e:
                cleanup_errors.append(f"Playwright cleanup error: {e}")
                self.playwright = None  # 強制的にクリア

        # 初期化フラグリセット
        self._initialized = False

        # メモリ強制ガベージコレクション
        try:
            import gc

            gc.collect()
            logger.debug("Forced garbage collection completed")
        except Exception:
            pass

        # クリーンアップ結果レポート
        if cleanup_errors:
            logger.warning(f"⚠️ Cleanup completed with {len(cleanup_errors)} errors:")
            for error in cleanup_errors:
                logger.warning(f"   - {error}")
        else:
            logger.info("✅ All resources cleaned up successfully")

        logger.info(f"🗂️ Test files: {self.temp_dir}")

        # メモリ使用量ログ（可能であれば）
        try:
            import psutil

            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            logger.info(f"💾 Current memory usage: {memory_mb:.1f} MB")
        except ImportError:
            pass
        except Exception:
            pass


"""連続実行(--count)は廃止。単一実行のみをサポート。"""


# 長めの動的生成フォームにも対応するため延長（単発テストのみ実行のため許容）。
# デフォルトの単一実行タイムアウト（4分）
# 環境変数 `FM_TEST_TIMEOUT_SECONDS` で上書き可能（例: 300）
import os as _os
try:
    DEFAULT_TEST_TIMEOUT_SECONDS = int(_os.getenv("FM_TEST_TIMEOUT_SECONDS", "60"))
except Exception:
    DEFAULT_TEST_TIMEOUT_SECONDS = 60


async def main():
    """メイン実行関数"""
    parser = argparse.ArgumentParser(
        description="Field Mapping Algorithm Testing Tool (single-run only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_field_mapping_analyzer.py                    # Single random test
  python test_field_mapping_analyzer.py --company-id 12345 # Test specific company
""",
    )

    parser.add_argument("--company-id", type=int, help="Specific company ID to test")
    # --count オプションは廃止
    parser.add_argument(
        "--verbose", action="store_true", help="Show normal logs (summary filter off)"
    )
    parser.add_argument("--debug", action="store_true", help="Show debug logs")

    args = parser.parse_args()

    # ログ構成（quietデフォルト）
    configure_logging(verbose=args.verbose, debug=args.debug)

    # 単一テスト実行のみ対応
    try:
        async with FieldMappingAnalyzer() as analyzer:
            try:
                # デフォルト動作（引数なし）は全体で2分のタイムアウトを設定
                success = await asyncio.wait_for(
                    analyzer.run_single_test(args.company_id),
                    timeout=DEFAULT_TEST_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"⏱️ Test timed out after {DEFAULT_TEST_TIMEOUT_SECONDS} seconds"
                )
                success = False

            if success:
                logger.info("🎯 Field mapping analysis completed successfully!")
            else:
                logger.error("❌ Field mapping analysis failed")

    except Exception as e:
        logger.error(f"❌ Analysis failed: {e}")
        import gc

        gc.collect()  # 例外時も確実にメモリクリーンアップ


if __name__ == "__main__":
    asyncio.run(main())
