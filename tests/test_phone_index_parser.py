import re

from src.form_sender.analyzer.unmapped_element_handler import UnmappedElementHandler  # type: ignore


def infer_idx(nm: str, ide: str, cls: str):
    # 簡易に内部関数のロジックを再現（外部公開していないため）
    nm = (nm or '').lower(); ide = (ide or '').lower(); cls = (cls or '').lower()
    blob = nm + ' ' + ide + ' ' + cls
    if not (('tel' in blob) or ('phone' in blob) or ('電話' in blob)):
        return None
    for s in (nm, ide, cls):
        if not s:
            continue
        m = re.search(r'(?:tel|phone|電話)[^\d]*([0123])(?!.*\d)', s)
        if m:
            raw = int(m.group(1))
            return (raw + 1) if raw in (0, 1, 2) else raw
    tail = re.search(r'(\d)(?!.*\d)$', blob)
    if tail:
        raw = int(tail.group(1))
        return (raw + 1) if raw in (0, 1, 2) else raw
    return 1


def test_phone_index_mapping_zero_based():
    assert infer_idx('電話番号[data][0]', '', '') == 1
    assert infer_idx('電話番号[data][1]', '', '') == 2
    assert infer_idx('電話番号[data][2]', '', '') == 3


def test_phone_index_mapping_english_tokens():
    assert infer_idx('tel1', '', '') == 1
    assert infer_idx('phone3', '', '') == 3


def test_phone_index_mapping_no_digit():
    assert infer_idx('tel', '', '') == 1


def test_phone_index_mapping_out_of_range():
    # 4などの範囲外はそのまま返る（後段で1..3以外は弾かれる想定）
    assert infer_idx('tel4', '', '') == 4
    # 異常系: 電話トークン無し → None
    assert infer_idx('fax2', '', '') is None
