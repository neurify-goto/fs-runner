#!/usr/bin/env python3
"""
オフライン・フォームマッピング検証スクリプト

指定したHTMLファイル（保存済みのpage_source）をPlaywrightのページに読み込み、
RuleBasedAnalyzerでマッピングを実行して結果を表示する。

使い方:
  python tests/run_offline_mapping.py test_results/field_mapping_YYYYMMDD_xxxxxx/page_source_*.html
"""
import sys
import json
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from form_sender.analyzer.rule_based_analyzer import RuleBasedAnalyzer
from tests.data.test_client_data import create_test_client_config


async def run_offline(html_path: str) -> int:
    html_file = Path(html_path)
    if not html_file.exists():
        print(f"❌ HTMLファイルが見つかりません: {html_file}")
        return 2

    html = html_file.read_text(encoding='utf-8', errors='ignore')

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(html, wait_until='domcontentloaded')

        analyzer = RuleBasedAnalyzer(page)
        result = await analyzer.analyze_form(client_data=create_test_client_config())

        # 最低限の表示
        fm = result.get('field_mapping', {})
        print('\n=== Field Mapping (offline) ===')
        for k, v in fm.items():
            sel = v.get('selector') or v.get('element') or ''
            print(f"- {k}: selector={sel}")

        # 解析結果を隣接フォルダに保存
        out_dir = PROJECT_ROOT / 'test_results' / 'offline'
        out_dir.mkdir(parents=True, exist_ok=True)
        # JSONシリアライズ可能に変換
        def _to_jsonable(obj):
            if isinstance(obj, dict):
                return {k: _to_jsonable(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_to_jsonable(v) for v in obj]
            try:
                json.dumps(obj)
                return obj
            except Exception:
                return str(obj)

        out_json = out_dir / f"offline_mapping_{html_file.stem}.json"
        out_json.write_text(json.dumps(_to_jsonable(result), ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\n💾 解析結果を保存しました: {out_json}")

        # 重要フィールドの存在確認
        required = ['姓', '名', '姓ひらがな', '名ひらがな', 'メールアドレス', 'お問い合わせ本文']
        missing = [r for r in required if r not in fm]
        if missing:
            print(f"⚠️ 必須想定フィールドが未検出: {', '.join(missing)}")
            return 1
        return 0
    finally:
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        await pw.stop()


def main() -> int:
    if len(sys.argv) < 2:
        print("使い方: python tests/run_offline_mapping.py path/to/page_source.html")
        return 2
    return asyncio.run(run_offline(sys.argv[1]))


if __name__ == '__main__':
    raise SystemExit(main())
