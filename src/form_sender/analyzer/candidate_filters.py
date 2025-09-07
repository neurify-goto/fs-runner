from __future__ import annotations

"""候補要素フィルタ（FieldMapper._score_element_in_detail での早期除外を分離）。"""

from typing import Any, Dict
from playwright.async_api import Locator
from config.manager import get_prefectures


async def allow_candidate(field_name: str, element: Locator, element_info: Dict[str, Any]) -> bool:
    """フィールド固有の早期不採用判定。

    - 住所×select の誤検出抑止: 都道府県名が十分含まれない select は除外
    - 性別×select の誤検出抑止: 男女表現が両方なければ除外
    """
    try:
        # 都道府県: select の場合は option に都道府県名が一定数含まれることを要求
        if field_name == "都道府県":
            tag = (element_info.get("tag_name") or "").lower()
            if tag == "select":
                try:
                    options = await element.evaluate(
                        "el => Array.from(el.options).map(o => (o.textContent||'').trim())"
                    )
                except Exception:
                    options = []
                names = (get_prefectures() or {}).get("names", [])
                hits = 0
                low_opts = [str(o).lower() for o in options]
                for n in names or []:
                    if str(n).lower() in low_opts:
                        hits += 1
                        if hits >= 5:
                            break
                if hits < 5 and not any("都道府県" in (o or "") for o in options):
                    return False

        if field_name == "住所":
            tag = (element_info.get("tag_name") or "").lower()
            if tag == "select":
                try:
                    options = await element.evaluate(
                        "el => Array.from(el.options).map(o => (o.textContent||'').trim())"
                    )
                except Exception:
                    options = []
                pref_cfg = get_prefectures() or {}
                names = pref_cfg.get("names", []) if isinstance(pref_cfg, dict) else []
                hits = 0
                if options and names:
                    low_opts = [str(o).lower() for o in options]
                    for n in names:
                        if str(n).lower() in low_opts:
                            hits += 1
                            if hits >= 5:
                                break
                if hits < 5 and not any("都道府県" in (o or "") for o in options):
                    return False
            # 入力欄の場合、建物名・部屋番号のみを示唆するプレースホルダ/属性は除外
            ph = str(element_info.get("placeholder") or "").lower()
            name_id_cls = " ".join([
                str(element_info.get("name") or ""),
                str(element_info.get("id") or ""),
                str(element_info.get("class") or ""),
            ]).lower()
            neg = ["建物名", "建物", "マンション", "アパート", "部屋番号", "号室", "room", "apartment", "building"]
            if any(t in ph for t in neg) or any(t in name_id_cls for t in neg):
                return False

        if field_name == "性別":
            tag = (element_info.get("tag_name") or "").lower()
            if tag == "select":
                try:
                    opt_texts = await element.evaluate(
                        "el => Array.from(el.options).map(o => (o.textContent||'').trim().toLowerCase())"
                    )
                except Exception:
                    opt_texts = []
                male = any(k in (t or "") for t in opt_texts for k in ["男", "男性", "male"])
                female = any(k in (t or "") for t in opt_texts for k in ["女", "女性", "female"])
                if not (male and female):
                    return False
    except Exception:
        pass

    return True
