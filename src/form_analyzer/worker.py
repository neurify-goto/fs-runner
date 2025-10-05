#!/usr/bin/env python3
"""
Form Analyzer Worker

企業フォームページを解析し、営業禁止検出とプロンプト生成を行うワーカークラス。
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

from playwright.async_api import async_playwright

from .form_extractor import FormExtractor
from .groq_client import GroqClient
from .prohibition_detector import ProhibitionDetector
from .prompt_generator import PromptGenerator
from utils.env import is_github_actions

logger = logging.getLogger(__name__)


class FormAnalyzerWorker:
    """フォーム解析ワーカー（form-sales-fuma準拠版）"""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None

        # ヘルパーモジュールのインスタンス化
        self.prohibition_detector = ProhibitionDetector()
        self.form_extractor = FormExtractor()
        self.prompt_generator = PromptGenerator()
        self.groq_client = GroqClient()

        self._initialize_constants()

    def _initialize_constants(self):
        """定数・設定の初期化"""
        self.PAGE_LOAD_TIMEOUT = 30000
        self.ELEMENT_WAIT_TIMEOUT = 5000
        # スクロール関連定数
        self.SCROLL_STEPS = 5  # スクロールステップ数
        self.SCROLL_WAIT_TIME = 0.8  # 各ステップでの待機時間（秒）
        self.FINAL_WAIT_TIME = 2.0  # スクロール完了後の最終待機時間（秒）
        logger.debug("定数・設定を初期化しました")

    async def _initialize_browser(self):
        """Playwrightブラウザを初期化"""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
            context = await self.browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            self.page = await context.new_page()
            logger.info("Playwrightブラウザを初期化しました")

    async def _fetch_form_page(self, form_url: str) -> str:
        """フォームページ取得"""
        try:
            await self.page.goto(form_url, wait_until='networkidle', timeout=self.PAGE_LOAD_TIMEOUT)
            
            # ページを段階的にスクロールしてJavaScript動的コンテンツを読み込ませる
            await self._scroll_page_gradually()
            
            return await self.page.content()
        except Exception as e:
            # URLはログに出さない（CIポリシー準拠）
            logger.warning(f"フォームページ取得エラー: ***URL_REDACTED*** - {e}")
            try:
                await self.page.goto('about:blank', timeout=self.ELEMENT_WAIT_TIMEOUT)
                await asyncio.sleep(0.5)
            except: # noqa
                pass
            raise

    async def _scroll_page_gradually(self):
        """ページを段階的にスクロールしてコンテンツを読み込ませる"""
        try:
            # ページの高さを取得
            page_height = await self.page.evaluate("document.body.scrollHeight")
            
            # 段階的にスクロール
            for i in range(self.SCROLL_STEPS):
                scroll_position = (page_height * (i + 1)) // self.SCROLL_STEPS
                await self.page.evaluate(f"window.scrollTo(0, {scroll_position})")
                await asyncio.sleep(self.SCROLL_WAIT_TIME)
            
            # 最後に一番下までスクロール
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(self.SCROLL_WAIT_TIME)
            
            # 一番上に戻る
            await self.page.evaluate("window.scrollTo(0, 0)")
            
            # 最終的な安定化待機
            await asyncio.sleep(self.FINAL_WAIT_TIME)
            
            logger.debug("ページスクロール処理完了")
            
        except Exception as e:
            logger.warning(f"ページスクロール処理でエラーが発生（継続）: {e}")
            # スクロールエラーが発生しても処理は継続

    async def __aenter__(self):
        await self._initialize_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    def get_github_event_data(self) -> Dict[str, Any]:
        """GitHub Actions イベントデータを取得"""
        event_path = os.environ.get('GITHUB_EVENT_PATH')
        if not event_path or not os.path.exists(event_path):
            raise ValueError("GitHub Actions イベントファイルが見つかりません")
        with open(event_path, 'r', encoding='utf-8') as f:
            event_data = json.load(f)
        client_payload = event_data.get('client_payload', {})
        if not client_payload:
            raise ValueError("client_payload が見つかりません")
        return client_payload

    async def analyze_company_form(self, company_data: Dict[str, Any]) -> Dict[str, Any]:
        """企業フォーム解析処理"""
        start_time = time.time()
        record_id = company_data.get('record_id')
        company_name = company_data.get('company_name', '')
        form_url = company_data.get('form_url', '')

        logger.info(f"企業フォーム解析開始: Record ID {record_id}")

        validation_result = self._validate_input_data(company_data)
        if not validation_result['valid']:
            return self._create_error_result(company_data, start_time, f"入力データ検証エラー: {validation_result['error']}", 'validation_error')

        html_content = None
        try:
            logger.debug(f"企業ID {record_id}: HTMLページ取得開始")
            fetch_result = await self._fetch_form_page_with_validation(form_url)
            if not fetch_result['success']:
                return self._create_error_result(company_data, start_time, f"ページ取得失敗: {fetch_result['error']}", 'network_error')

            html_content = fetch_result['content']
            logger.debug(f"企業ID {record_id}: HTMLページ取得完了 ({len(html_content)} バイト)")

            logger.debug(f"企業ID {record_id}: 営業禁止文言検出開始")
            detected, phrases = self.prohibition_detector.detect(html_content)
            if detected:
                logger.info(f"営業禁止文言を検出: Record ID {record_id}")
                return self._create_success_result(company_data, start_time, prohibition_detected=True, prohibition_phrases=phrases)

            logger.debug(f"企業ID {record_id}: フォーム要素抽出開始")
            form_elements = self.form_extractor.extract(html_content)
            if not form_elements:
                logger.warning(f"フォーム要素が見つかりません: Record ID {record_id}")
                return self._create_error_result(company_data, start_time, "フォーム要素が見つかりません", 'no_form_elements')

            logger.debug(f"企業ID {record_id}: プロンプト生成開始")
            system_prompt, user_prompt = self.prompt_generator.generate_prompts(company_data, form_elements)
            if not system_prompt or not user_prompt:
                 return self._create_error_result(company_data, start_time, "プロンプト生成エラー", 'prompt_generation_error')

            logger.info(f"プロンプト生成完了: Record ID {record_id}")
            return self._create_success_result(
                company_data, start_time,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                form_elements_count=len(form_elements)
            )

        except asyncio.TimeoutError as e:
            logger.error(f"企業ID {record_id}: タイムアウトエラー - {e}")
            return self._create_error_result(company_data, start_time, f"処理タイムアウト: {str(e)}", 'timeout_error')
        except Exception as e:
            logger.error(f"企業ID {record_id}: 予期しないエラー - {e}", exc_info=True)
            return self._create_error_result(company_data, start_time, f"予期しないエラー: {str(e)}", 'unexpected_error')

    def _validate_input_data(self, company_data: Dict[str, Any]) -> Dict[str, Any]:
        """入力データの検証"""
        required_fields = ['record_id', 'form_url']
        for field in required_fields:
            if not company_data.get(field):
                return {'valid': False, 'error': f"必須フィールド '{field}' が不足しています"}
        form_url = company_data['form_url']
        if not (form_url.startswith('http://') or form_url.startswith('https://')):
            return {'valid': False, 'error': "不正なURL形式です"}
        return {'valid': True}

    async def _fetch_form_page_with_validation(self, form_url: str) -> Dict[str, Any]:
        """検証付きページ取得処理"""
        try:
            html_content = await self._fetch_form_page(form_url)
            if not html_content:
                return {'success': False, 'error': "HTMLコンテンツが空です"}
            if len(html_content) < 100:
                return {'success': False, 'error': "HTMLコンテンツが短すぎます（100文字未満）"}
            if '<html' not in html_content.lower() and '<body' not in html_content.lower():
                logger.warning("HTMLタグが見つかりません - プレーンテキストの可能性があります")
            return {'success': True, 'content': html_content}
        except Exception as e:
            return {'success': False, 'error': f"ページ取得エラー: {str(e)}"}

    def _create_error_result(self, company_data: Dict[str, Any], start_time: float, error_msg: str, error_type: str) -> Dict[str, Any]:
        """エラー結果の統一作成"""
        return {
            'record_id': company_data.get('record_id'),
            'company_name': company_data.get('company_name', ''),
            'form_url': company_data.get('form_url', ''),
            'status': 'failed',
            'error': error_msg,
            'error_type': error_type,
            'prohibition_detected': False,
            'execution_time': time.time() - start_time,
            'timestamp': datetime.now().isoformat()
        }

    def _create_success_result(self, company_data: Dict[str, Any], start_time: float, **kwargs) -> Dict[str, Any]:
        """成功結果の統一作成"""
        return {
            'record_id': company_data.get('record_id'),
            'company_name': company_data.get('company_name', ''),
            'form_url': company_data.get('form_url', ''),
            'status': 'success',
            'prohibition_detected': kwargs.get('prohibition_detected', False),
            'prohibition_phrases': kwargs.get('prohibition_phrases', []),
            'system_prompt': kwargs.get('system_prompt'),
            'user_prompt': kwargs.get('user_prompt'),
            'form_elements_count': kwargs.get('form_elements_count', 0),
            'execution_time': time.time() - start_time,
            'timestamp': datetime.now().isoformat()
        }

    async def process_batch(self, batch_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """バッチ処理実行"""
        # GitHub Actions環境では統計情報を制限
        github_actions_env = is_github_actions()
        if github_actions_env:
            logger.info("フォーム解析バッチ処理開始")
        else:
            logger.info(f"フォーム解析バッチ処理開始: {len(batch_data)}件")
        results = []
        prohibited_count = 0

        for i, company_data in enumerate(batch_data):
            record_id = company_data.get('record_id')
            company_name = company_data.get('company_name', '')
            # GitHub Actions環境ではrecord_idログを制限
            github_actions_env = is_github_actions()
            if github_actions_env:
                logger.info(f"企業処理 {i+1}/{len(batch_data)}")
            else:
                logger.info(f"企業処理 {i+1}/{len(batch_data)} - Record ID {record_id}")
            try:
                result = await self.analyze_company_form(company_data)
                results.append(result)
                if result.get('prohibition_detected'):
                    prohibited_count += 1
                if github_actions_env:
                    logger.info(f"企業処理完了 {i+1}/{len(batch_data)} - {result['status']}")
                else:
                    logger.info(f"企業処理完了 {i+1}/{len(batch_data)} - Record ID {record_id} - {result['status']}")
                if self.page:
                    try:
                        await self.page.goto('about:blank', timeout=self.ELEMENT_WAIT_TIMEOUT)
                        await asyncio.sleep(0.1)
                    except Exception as cleanup_error:
                        logger.debug(f"ページクリーンアップエラー（無視可能）: {cleanup_error}")
            except Exception as e:
                logger.error(f"企業処理エラー {i+1}/{len(batch_data)} - Record ID {record_id}: {e}")
                error_result = self._create_error_result(company_data, time.time(), f'処理エラー: {e}', 'batch_processing_error')
                results.append(error_result)

        successful = [r for r in results if r['status'] == 'success']
        failed = [r for r in results if r['status'] == 'failed']
        prompt_ready_results = [r for r in successful if not r.get('prohibition_detected', False) and r.get('system_prompt')]

        groq_batch_result = None
        if prompt_ready_results:
            logger.info(f"プロンプト生成済み企業 {len(prompt_ready_results)}件のGroq Batch API処理を開始")
            groq_batch_result = self.groq_client.create_groq_batch_request(prompt_ready_results)

        summary = {
            'total_processed': len(results),
            'total_successful': len(successful),
            'total_failed': len(failed),
            'total_prohibited': prohibited_count,
            'results': results,
            'execution_time': sum(r.get('execution_time', 0) for r in results),
            'timestamp': datetime.now().isoformat(),
            'groq_batch_result': groq_batch_result,
            'prompt_ready_count': len(prompt_ready_results)
        }

        logger.info(f"フォーム解析バッチ処理完了: 成功={len(successful)}, 失敗={len(failed)}, 営業禁止={prohibited_count}")
        if groq_batch_result:
            if groq_batch_result.get('success'):
                logger.info(f"Groq Batch API送信成功: batch_id={groq_batch_result.get('batch_id')}")
            else:
                logger.error(f"Groq Batch API送信失敗: {groq_batch_result.get('error')}")
        return summary

    async def run(self):
        """メイン処理実行"""
        try:
            logger.info("=== Form Analyzer GitHub Actions Worker 開始 ===")
            logger.info(f"GITHUB_EVENT_PATH: {os.getenv('GITHUB_EVENT_PATH', 'NOT_SET')}")
            logger.info(f"Working directory: {os.getcwd()}")

            event_data = self.get_github_event_data()
            batch_data = event_data.get('batch_data', [])
            # GitHub Actions環境では統計情報を制限
            github_actions_env = is_github_actions()
            if github_actions_env:
                logger.info("フォーム解析タスクデータ取得完了")
            else:
                logger.info(f"フォーム解析タスク数: {len(batch_data)}")

            if not batch_data:
                raise ValueError("処理対象タスクが空です")

            artifacts_dir = Path("../artifacts")
            artifacts_dir.mkdir(exist_ok=True)

            async with self as worker:
                results = await worker.process_batch(batch_data)

            results_file = artifacts_dir / "processing_results.json"
            with open(results_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

            logger.info(f"結果を {results_file} に保存しました")
            logger.info("=== Form Analyzer GitHub Actions Worker 正常終了 ===")

        except Exception as e:
            logger.critical(f"メイン処理で致命的なエラーが発生: {e}", exc_info=True)
            logger.critical("=== Form Analyzer GitHub Actions Worker 異常終了 ===")
            # 異常終了時もファイルにエラー情報を書き出す試み
            try:
                artifacts_dir = Path("../artifacts")
                artifacts_dir.mkdir(exist_ok=True)
                error_file = artifacts_dir / "error_log.json"
                error_info = {
                    "error": str(e),
                    "timestamp": datetime.now().isoformat()
                }
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(error_info, f, ensure_ascii=False, indent=2)
            except Exception as write_e:
                logger.error(f"エラーログの書き込みに失敗: {write_e}")
