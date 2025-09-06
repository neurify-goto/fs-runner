#!/usr/bin/env python3
"""
Offline snapshot runner for field mapping.

Loads saved page_source_*.html under test_results and runs RuleBasedAnalyzer
against file:// snapshots to produce fresh mapping results without Supabase.

Usage:
  python tests/run_mapping_on_snapshot.py                       # auto-pick 4 snapshots
  python tests/run_mapping_on_snapshot.py --sources a.html b.html  # specify sources

Outputs mapping JSON next to each HTML as analysis_result_rerun_*.json
and prints both paths for external evaluation (e.g., Claude Code prompt in fmi.md).
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List

# Import src modules
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from playwright.async_api import async_playwright
from form_sender.analyzer.rule_based_analyzer import RuleBasedAnalyzer
from tests.data.test_client_data import create_test_client_config


def find_default_sources(limit: int = 4) -> List[Path]:
    base = PROJECT_ROOT / "test_results"
    dirs = sorted([p for p in base.glob('field_mapping_*') if p.is_dir()])
    htmls: List[Path] = []
    for d in dirs:
        htmls.extend(sorted(d.glob('page_source_*.html')))
        if len(htmls) >= limit:
            break
    return htmls[:limit]


async def run_once(html_path: Path) -> Path:
    html_url = html_path.resolve().as_uri()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = html_path.parent / f"analysis_result_rerun_{timestamp}.json"

    def _make_json_serializable(obj):
        if isinstance(obj, dict):
            return {k: _make_json_serializable(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_make_json_serializable(x) for x in obj]
        # Playwright Locator やページ要素などは文字列にフォールバック
        try:
            from playwright.async_api import Locator
            if isinstance(obj, Locator):
                return str(obj)
        except Exception:
            pass
        # 任意オブジェクトは安全側で文字列化
        if hasattr(obj, "__dict__"):
            return str(obj)
        return obj

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        page = await context.new_page()
        await page.goto(html_url)

        analyzer = RuleBasedAnalyzer(page)
        client_cfg = create_test_client_config()
        result = await analyzer.analyze_form(client_cfg)

        serializable = _make_json_serializable(result)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)

        await context.close()
        await browser.close()

    return out_path


async def main():
    parser = argparse.ArgumentParser(description='Run RuleBasedAnalyzer on saved page_source snapshots (file://)')
    parser.add_argument('--sources', nargs='*', help='Paths to page_source_*.html files')
    parser.add_argument('--limit', type=int, default=4, help='Number of snapshots to auto-pick')
    args = parser.parse_args()

    if args.sources:
        sources = [Path(s) for s in args.sources]
    else:
        sources = find_default_sources(args.limit)

    if not sources:
        print('No snapshot HTML found under test_results/.')
        sys.exit(1)

    for i, src in enumerate(sources, 1):
        if not src.exists():
            print(f"[SKIP] Missing source: {src}")
            continue
        out = await run_once(src)
        print(f"[{i}] Mapping Result: {out}")
        print(f"[{i}] Page Source:   {src}")


if __name__ == '__main__':
    asyncio.run(main())
