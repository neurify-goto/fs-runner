#!/usr/bin/env python3
"""
オフライン/DB不要の再テスト用スクリプト

既存の解析結果JSON、またはURLを入力に、Playwrightで再解析を実行して
`test_results/field_mapping_YYYYmmdd_HHMMSS/` 配下に結果を保存します。

使い方:
  - 既存結果から再テスト:  python scripts/retest_field_mapping.py --from-result path/to/analysis_result_xxx.json
  - URLを直接指定:        python scripts/retest_field_mapping.py --url https://example.com/contact

注意:
  - Supabase接続は行いません（DB不要）。
  - Playwrightのブラウザは headless=1（環境変数 PLAYWRIGHT_HEADLESS=0 でGUI可）。
"""

import argparse
import asyncio
import json
from pathlib import Path
from datetime import datetime

import os
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 既存テスト用クラスを流用
from tests.test_field_mapping_analyzer import FieldMappingAnalyzer


async def run_by_url(url: str) -> Path:
    """URLを直接解析して結果JSONのパスを返す"""
    analyzer = FieldMappingAnalyzer()
    try:
        # Supabaseは使わないのでブラウザのみ初期化
        await analyzer._initialize_browser()  # type: ignore[attr-defined]
        analysis_result, source_file = await analyzer._analyze_form_mapping_once(url)  # type: ignore[attr-defined]
        summary = analyzer.analyze_mapping_results(analysis_result)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(analyzer.temp_dir) / f"analysis_result_{ts}.json"
        payload = {
            'company_id': None,
            'form_url': url,
            'timestamp': ts,
            'analysis_result': analyzer._make_json_serializable(analysis_result),  # type: ignore[attr-defined]
            'analysis_summary': summary,
            'source_file': source_file,
            'test_metadata': {
                'company_id': None,
                'is_specific_id_test': False,
                'test_timestamp': ts,
                'retest_mode': 'url'
            }
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"Saved: {out}")
        print(f"Page source: {source_file}")
        return out
    finally:
        await analyzer.cleanup()


async def run_by_result(json_path: Path) -> Path:
    """既存の結果JSONからURLを取得して再解析"""
    data = json.loads(json_path.read_text(encoding='utf-8'))
    url = data.get('form_url')
    if not url:
        raise ValueError('form_url not found in the result JSON')
    return await run_by_url(url)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--from-result', type=str, help='既存 analysis_result_*.json のパス')
    p.add_argument('--url', type=str, help='直接解析するフォームURL')
    args = p.parse_args()

    if not (args.from_result or args.url):
        p.error(' --from-result か --url のどちらかを指定してください')

    if args.from_result:
        path = Path(args.from_result).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        asyncio.run(run_by_result(path))
    else:
        asyncio.run(run_by_url(args.url))


if __name__ == '__main__':
    main()

