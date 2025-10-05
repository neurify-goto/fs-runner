"""Client configuration validation library.

GAS が生成する2シート構造の client_config を検証・正規化する。
dispatcher / Cloud Run Job / GitHub Actions で共通利用することを想定している。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union

logger = logging.getLogger(__name__)


class ClientConfigValidationError(ValueError):
    """client_config の検証に失敗した場合の例外。"""


class TargetingConfig(TypedDict, total=False):
    """GAS 側 targeting セクションの型定義。"""

    id: int
    subject: str
    message: str
    targeting_sql: str
    ng_companies: str
    max_daily_sends: int
    send_start_time: str
    send_end_time: str
    send_days_of_week: List[int]


class ClientInfo(TypedDict, total=False):
    """GAS 側 client セクションの型定義。"""

    company_name: str
    company_name_kana: str
    form_sender_name: str
    last_name: str
    first_name: str
    last_name_kana: str
    first_name_kana: str
    last_name_hiragana: str
    first_name_hiragana: str
    position: str
    gender: str
    email_1: str
    email_2: str
    postal_code_1: str
    postal_code_2: str
    address_1: str
    address_2: str
    address_3: str
    address_4: str
    phone_1: str
    phone_2: str
    phone_3: str
    department: str
    website_url: str
    address_5: str


class Gas2SheetConfig(TypedDict):
    """GAS 2シート構造で渡されるデータの型定義。"""

    targeting_id: int
    client_id: int
    active: bool
    client: ClientInfo
    targeting: TargetingConfig


_ConfigCache = Dict[str, Tuple[Dict[str, Any], float]]
_config_cache: _ConfigCache = {}
_CACHE_TTL_SECONDS = 300


def _get_cache_key(raw_config: Dict[str, Any]) -> str:
    try:
        config_str = json.dumps(raw_config, sort_keys=True)
    except TypeError as exc:
        raise ClientConfigValidationError(f"config is not JSON serializable: {exc}") from exc
    return hashlib.sha256(config_str.encode()).hexdigest()


def _is_cache_valid(timestamp: float) -> bool:
    return (time.time() - timestamp) < _CACHE_TTL_SECONDS


def _ensure_dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ClientConfigValidationError(f"'{name}' セクションが辞書型ではありません")
    return value


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _validate_2sheet_config(config: Dict[str, Any]) -> None:
    if 'client' not in config:
        raise ClientConfigValidationError("2シート構造の 'client' セクションが見つかりません")
    if 'targeting' not in config:
        raise ClientConfigValidationError("2シート構造の 'targeting' セクションが見つかりません")

    _ensure_dict(config['client'], 'client')
    _ensure_dict(config['targeting'], 'targeting')

    if 'targeting_id' not in config:
        raise ClientConfigValidationError("必須フィールド 'targeting_id' が見つかりません")
    if 'client_id' not in config:
        raise ClientConfigValidationError("必須フィールド 'client_id' が見つかりません")

    client_required_fields = [
        'company_name', 'company_name_kana', 'form_sender_name',
        'last_name', 'first_name', 'last_name_kana', 'first_name_kana',
        'last_name_hiragana', 'first_name_hiragana', 'position',
        'gender', 'email_1',
        'postal_code_1', 'address_1', 'address_2', 'address_3',
        'phone_1'
    ]
    client_optional_not_empty = ['postal_code_2', 'address_4', 'phone_2', 'phone_3', 'email_2']
    client = config['client']
    missing_client_fields = [
        f for f in client_required_fields
        if f not in client or _is_blank(client.get(f))
    ]
    if missing_client_fields:
        raise ClientConfigValidationError(
            f"client セクションの必須フィールドが不足: {missing_client_fields}"
        )

    optional_empty = [
        f for f in client_optional_not_empty
        if f in client and _is_blank(client.get(f))
    ]
    if optional_empty:
        logger.debug("client optional fields left blank: %s", optional_empty)

    targeting_required_fields = [
        'subject', 'message', 'max_daily_sends', 'send_start_time', 'send_end_time', 'send_days_of_week'
    ]
    targeting = config['targeting']
    missing_targeting_fields = [f for f in targeting_required_fields if targeting.get(f) is None]
    if missing_targeting_fields:
        raise ClientConfigValidationError(
            f"targeting セクションの必須フィールドが不足: {missing_targeting_fields}"
        )

    if not isinstance(targeting['send_start_time'], str):
        raise ClientConfigValidationError("targeting.send_start_time は文字列である必要があります")
    if not isinstance(targeting['send_end_time'], str):
        raise ClientConfigValidationError("targeting.send_end_time は文字列である必要があります")
    if not isinstance(targeting['send_days_of_week'], list):
        raise ClientConfigValidationError("targeting.send_days_of_week はリストである必要があります")
    if not isinstance(targeting['max_daily_sends'], int):
        raise ClientConfigValidationError("targeting.max_daily_sends は整数である必要があります")

    if not all(isinstance(day, int) and 0 <= day <= 6 for day in targeting['send_days_of_week']):
        raise ClientConfigValidationError(
            "targeting.send_days_of_week は 0-6 の整数リストである必要があります"
        )

    time_pattern = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
    if not time_pattern.match(targeting['send_start_time']):
        raise ClientConfigValidationError(
            "targeting.send_start_time は 'HH:MM' 形式である必要があります"
        )
    if not time_pattern.match(targeting['send_end_time']):
        raise ClientConfigValidationError(
            "targeting.send_end_time は 'HH:MM' 形式である必要があります"
        )


def _normalize_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {'true', '1', 'yes', 'on'}:
            return True
        if lowered in {'false', '0', 'no', 'off', ''}:
            return False
    raise ClientConfigValidationError(f"ブール値として解釈できません: {value}")


def _get_config_value(config: Dict[str, Any], key: str, fallback_key: Optional[str] = None) -> Any:
    if key in config:
        return config[key]
    if 'targeting' in config and isinstance(config['targeting'], dict) and key in config['targeting']:
        return config['targeting'][key]
    if fallback_key and fallback_key in config:
        return config[fallback_key]
    return None


def transform_client_config(raw_config: Union[Gas2SheetConfig, Dict[str, Any]]) -> Dict[str, Any]:
    copy_config = dict(raw_config)
    cache_key = _get_cache_key(copy_config)
    cached = _config_cache.get(cache_key)
    if cached and _is_cache_valid(cached[1]):
        logger.debug("client_config cache hit: %s", cache_key[:8])
        return cached[0]

    _validate_2sheet_config(copy_config)
    logger.info("client_config validation succeeded for targeting_id=%s", copy_config.get('targeting_id'))

    # active フィールド統一（bool化）
    active_value = copy_config.get('active', True)
    try:
        copy_config['active'] = _normalize_boolean(active_value)
    except ClientConfigValidationError as exc:
        raise ClientConfigValidationError(f"active フィールドの値が不正です: {active_value}") from exc

    logger.debug("client_config normalization complete (active=%s)", copy_config['active'])

    _config_cache[cache_key] = (copy_config, time.time())
    return copy_config


def clear_config_cache() -> None:
    _config_cache.clear()


def get_validator() -> "ClientConfigValidator":
    return ClientConfigValidator()


class ClientConfigValidator:
    """オブジェクト指向のバリデータラッパー。"""

    def transform(self, raw_config: Union[Gas2SheetConfig, Dict[str, Any]]) -> Dict[str, Any]:
        return transform_client_config(raw_config)

    def clear_cache(self) -> None:
        clear_config_cache()
