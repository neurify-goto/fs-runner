#!/usr/bin/env python3
"""
Lightweight evaluator for mapping outputs.

For each analysis_result_*.json file, checks whether required fields inferred
from `required_fields_info.required_elements` have corresponding entries in
`analysis_result.input_assignments` (by heuristic name mapping).

Usage:
  python tests/evaluate_mappings.py <result_json1> [<result_json2> ...]
"""

import json
import sys
import re
from pathlib import Path
from typing import Dict, Any, List


def infer_required_category(elem: Dict[str, Any]) -> str:
    name = (elem.get('name') or '').lower()
    ph = (elem.get('placeholder') or '').lower()
    lbl = (elem.get('label_text') or '').lower()
    blob = ' '.join([name, ph, lbl])

    def has(*tokens: str) -> bool:
        return any(t in blob for t in tokens)

    if has('email', 'mail', 'メール'):
        return 'メールアドレス'
    if has('tel', 'phone', 'telephone', '電話'):
        return '電話番号'
    if has('zip', 'postal', 'postcode', '〒'):
        return '郵便番号'
    if has('subject', '件名', 'topic'):
        return '件名'
    if has('message', '本文', '問い合わせ', 'お問い合わせ'):
        return 'お問い合わせ本文'
    if has('kana', 'furigana', 'フリガナ', 'カナ'):
        return '統合氏名カナ'
    if has('addr', 'address', '所在地', '市', '区', '町', '村', '丁目', '番地'):
        return '住所'
    if has('name', '氏名', 'お名前', 'fullname', 'full_name'):
        return '統合氏名'
    return ''


def evaluate(result_path: Path) -> Dict[str, Any]:
    data = json.loads(result_path.read_text(encoding='utf-8'))
    ar = data.get('analysis_result', {})
    req = ar.get('required_fields_info', {})
    assigns: Dict[str, Any] = ar.get('input_assignments', {})
    required_elems: List[Dict[str, Any]] = req.get('required_elements', []) or []

    missing: List[Dict[str, str]] = []
    for elem in required_elems:
        cat = infer_required_category(elem)
        if not cat:
            # Unknown category: skip (cannot auto-judge)
            continue
        # Accept both exact key and known variants for kana/name
        ok = False
        candidates = [cat]
        if cat == '統合氏名カナ':
            candidates += ['姓カナ', '名カナ']
        if cat == '統合氏名':
            candidates += ['姓', '名']
        if cat == '住所':
            candidates += [k for k in assigns.keys() if k.startswith('住所')]

        for key in candidates:
            if key in assigns and str(assigns[key].get('value', '')).strip() != '':
                ok = True
                break
        if not ok:
            missing.append({'required_hint': elem.get('name') or elem.get('label_text') or '', 'expected_category': cat})

    return {
        'file': str(result_path),
        'company_id': data.get('company_id'),
        'form_url': data.get('form_url'),
        'required_count': len(required_elems),
        'missing_count': len(missing),
        'missing': missing,
    }


def main():
    if len(sys.argv) < 2:
        print('Usage: python tests/evaluate_mappings.py <result_json1> [<result_json2> ...]')
        sys.exit(1)

    for p in sys.argv[1:]:
        res = evaluate(Path(p))
        status = 'OK' if res['missing_count'] == 0 else 'NG'
        print(f"[{status}] {res['file']}  required={res['required_count']}  missing={res['missing_count']}")
        if res['missing_count']:
            for m in res['missing']:
                print(f"  - missing: expected={m['expected_category']}  hint={m['required_hint']}")


if __name__ == '__main__':
    main()

